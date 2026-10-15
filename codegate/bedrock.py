"""AWS Bedrock remediation advisor — an opt-in, advisory-only AI layer.

Design contract (the reason this module exists in its current shape):

* The **deterministic CFG engine is the sole authority on whether a leak exists.**
  ``analyzer.py`` never imports this module, and nothing here re-runs detection.
  Findings are consumed as already-proven ``Leak`` objects.
* The model is **advisory**. It explains why a proven leak leaks and proposes a
  remediation. It is instructed never to assert new findings; anything it suggests
  beyond the supplied finding is returned as an explicitly unverified hint.
* It is **off unless explicitly enabled** (``--ai`` / ``CODEGATE_AI=1``) *and*
  pointed at a Bedrock model. No credentials, no flag, no network call.
* It **fails closed**. Every failure mode returns ``None`` or a structured
  ``unavailable`` status; it never raises into the analysis path.
* It sends only the **enclosing function snippet**, never the whole repository.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default is a Claude model on Bedrock; override with CODEGATE_BEDROCK_MODEL.
DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"
DEFAULT_REGION = "us-east-1"

_SYSTEM_PROMPT = """\
You are a remediation advisor for CodeGate, a static resource-leak analyzer.

A deterministic control-flow-graph engine has ALREADY analyzed the code and \
PROVEN the leak you are shown. Your job is to help a developer fix it.

Hard rules:
1. Treat the supplied finding as ground truth. Never re-run detection, never \
claim the leak is a false positive, and never weaken the analyzer's verdict.
2. Never assert a DIFFERENT or ADDITIONAL leak as fact. If you suspect another \
problem, put it under "other_suggestions" and phrase it as something for the \
developer to verify with the analyzer.
3. Ground every explanation in the control-flow path you are given. Explain why \
that specific path escapes cleanup.
4. Be concrete and minimal. Prefer the smallest correct change. Mention runtime \
consequences (descriptor exhaustion, connection-pool starvation) briefly.
5. Reply with a single JSON object and no markdown fences or commentary.
"""

_EXPLAIN_INSTRUCTIONS = """\
Explain this leak and propose a fix.

Respond with a single JSON object using exactly these keys:
{{
  "summary": "one sentence a developer reads first",
  "why_it_leaks": "why this specific control-flow path escapes cleanup",
  "remediation": "numbered or short prose steps to fix it",
  "suggested_fix": "corrected code for the enclosing function, as a string",
  "alternatives": ["other valid approaches, or empty list"],
  "caveats": ["things that could break, trade-offs, or runtime cost, or empty list"],
  "other_suggestions": ["unverified hunches, clearly optional, or empty list"]
}}
"""


@dataclass
class BedrockConfig:
    """Resolved Bedrock settings. ``enabled`` is the master opt-in switch."""

    enabled: bool = False
    model_id: str = DEFAULT_MODEL
    region: str = DEFAULT_REGION
    max_tokens: int = 1200
    temperature: float = 0.2
    profile: Optional[str] = None
    snippet_chars: int = 4000

    @property
    def model_label(self) -> str:
        """Short human label, e.g. 'claude-3-5-sonnet' from a full model id.

        Strips the vendor prefix, the ``:N`` inference-profile suffix, and
        trailing build tokens (a 20241022 date stamp or a ``v2`` revision),
        while keeping meaningful version numbers like ``3-5-sonnet``.
        """
        name = self.model_id.split(".")[-1].split(":")[0]
        tokens = name.split("-")
        while tokens and re.fullmatch(r"v\d+|\d{4,}", tokens[-1]):
            tokens.pop()
        return "-".join(tokens) or name


@dataclass
class Guidance:
    """Advisory output. ``None`` fields mean the model omitted them."""

    summary: str = ""
    why_it_leaks: str = ""
    remediation: str = ""
    suggested_fix: str = ""
    alternatives: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    other_suggestions: list[str] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "why_it_leaks": self.why_it_leaks,
            "remediation": self.remediation,
            "suggested_fix": self.suggested_fix,
            "alternatives": self.alternatives,
            "caveats": self.caveats,
            "other_suggestions": self.other_suggestions,
            "model": self.model,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            },
        }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_env_file() -> None:
    """Best-effort .env load; never raises."""
    try:
        from dotenv import load_dotenv

        for candidate in (Path.cwd() / ".env", Path.home() / ".env"):
            if candidate.exists():
                load_dotenv(candidate)
                return
    except Exception:
        pass


def get_bedrock_config() -> BedrockConfig:
    """Resolve Bedrock settings from the environment.

    Opt-in requires BOTH an explicit enable flag and a model, so a stray AWS
    credential in the environment can never silently start sending code.
    """
    load_env_file()
    try:
        max_tokens = int(os.environ.get("CODEGATE_BEDROCK_MAX_TOKENS", "1200"))
    except ValueError:
        max_tokens = 1200
    try:
        temperature = float(os.environ.get("CODEGATE_BEDROCK_TEMPERATURE", "0.2"))
    except ValueError:
        temperature = 0.2

    model_id = os.environ.get("CODEGATE_BEDROCK_MODEL", "").strip() or DEFAULT_MODEL
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION

    return BedrockConfig(
        enabled=_truthy(os.environ.get("CODEGATE_AI", "")),
        model_id=model_id,
        region=region,
        max_tokens=max_tokens,
        temperature=temperature,
        profile=os.environ.get("AWS_PROFILE") or None,
    )


def bedrock_status(config: BedrockConfig | None = None) -> dict[str, Any]:
    """Describe whether the advisor can run, and why not if it cannot.

    Safe to call anywhere; makes no network request.
    """
    cfg = config or get_bedrock_config()
    boto3_ok = True
    try:
        import boto3  # noqa: F401
    except Exception:
        boto3_ok = False

    if not cfg.enabled:
        reason = "AI advisor is off. Re-run with --ai, or set CODEGATE_AI=1."
    elif not boto3_ok:
        reason = "boto3 is not installed. Run: pip install 'codegate[ai]'"
    else:
        reason = ""

    return {
        "enabled": cfg.enabled,
        "available": bool(cfg.enabled and boto3_ok),
        "reason": reason,
        "model": cfg.model_id,
        "model_label": cfg.model_label,
        "region": cfg.region,
    }


def _function_snippet(source: str, func_name: str, line: int, limit: int) -> str:
    """Extract the enclosing function for ``func_name``.

    Falls back to a line window when the source cannot be parsed, so a syntax
    error never prevents guidance.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        lines = source.splitlines()
        start = max(0, line - 1)
        return "\n".join(lines[start : start + 40])[:limit]

    best: Optional[ast.AST] = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            if best is None or node.lineno <= line:
                best = node
    if best is None:
        lines = source.splitlines()
        start = max(0, line - 1)
        return "\n".join(lines[start : start + 40])[:limit]

    seg = ast.get_source_segment(source, best) or ""
    return seg[:limit]


def _parse_json_payload(text: str) -> Optional[dict[str, Any]]:
    """Tolerant JSON extraction (models sometimes wrap output in fences)."""
    text = text.strip()
    if "```" in text:
        match = re.search(r"```(?:json)?\s*({.*?})\s*```", text, re.DOTALL)
        text = match.group(1) if match else re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        match = re.search(r"({.*})", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


class RemediationAdvisor:
    """Explains proven leaks and proposes fixes via AWS Bedrock Converse.

    The Bedrock client is injectable so the module can be exercised without
    network access or credentials.
    """

    def __init__(self, config: BedrockConfig | None = None, client: Any = None) -> None:
        self.cfg = config or get_bedrock_config()
        self._client = client

    # ── plumbing ────────────────────────────────────────────────────────────
    @property
    def client(self) -> Any:
        """Lazily build a bedrock-runtime client. Raises if boto3 is absent."""
        if self._client is None:
            import boto3

            kwargs: dict[str, Any] = {"region_name": self.cfg.region}
            if self.cfg.profile:
                kwargs["profile_name"] = self.cfg.profile
            self._client = boto3.client("bedrock-runtime", **kwargs)
        return self._client

    def available(self) -> tuple[bool, str]:
        """(can_run, reason_if_not). Never raises."""
        status = bedrock_status(self.cfg)
        return bool(status["available"]), str(status["reason"])

    def _converse(self, messages: list[dict[str, Any]], system: str) -> Optional[dict[str, Any]]:
        """Call Bedrock Converse. Returns None on any failure (fails closed)."""
        try:
            resp = self.client.converse(
                modelId=self.cfg.model_id,
                messages=messages,
                system=[{"text": system}],
                inferenceConfig={
                    "maxTokens": self.cfg.max_tokens,
                    "temperature": self.cfg.temperature,
                },
            )
        except Exception as exc:  # noqa: BLE001 - advisory path must not raise
            logger.warning("Bedrock Converse failed (%s): %s", type(exc).__name__, exc)
            return None

        try:
            text = resp["output"]["message"]["content"][0]["text"]
        except Exception:
            logger.warning("Bedrock returned an unexpected response shape.")
            return None

        usage = resp.get("usage", {}) or {}
        return {
            "text": text,
            "input_tokens": int(usage.get("inputTokens", 0) or 0),
            "output_tokens": int(usage.get("outputTokens", 0) or 0),
        }

    def _to_guidance(self, raw: Optional[dict[str, Any]], usage: dict[str, Any]) -> Optional[Guidance]:
        if not raw:
            return None
        return Guidance(
            summary=str(raw.get("summary", "") or ""),
            why_it_leaks=str(raw.get("why_it_leaks", "") or ""),
            remediation=str(raw.get("remediation", "") or ""),
            suggested_fix=str(raw.get("suggested_fix", "") or ""),
            alternatives=_as_list(raw.get("alternatives")),
            caveats=_as_list(raw.get("caveats")),
            other_suggestions=_as_list(raw.get("other_suggestions")),
            model=self.cfg.model_label,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )

    # ── finding → prompt ────────────────────────────────────────────────────
    @staticmethod
    def describe_finding(leak: Any) -> str:
        """Render a deterministic Leak as a compact evidence block."""
        leaking = getattr(leak, "path_sources", None) or []
        safe = getattr(leak, "safe_path_sources", None) or []

        def render(paths: list[Any]) -> str:
            out = []
            for p in paths[:3]:
                steps = " → ".join(str(s).strip() for s in p if str(s).strip())
                out.append(f"    {steps}")
            return "\n".join(out) or "    (none recorded)"

        reasons = ", ".join(getattr(leak, "leak_reasons", []) or []) or "unspecified"
        note = getattr(leak, "exception_note", "") or ""

        parts = [
            f"file:      {getattr(leak, 'file', '?')}",
            f"function:  {getattr(leak, 'func', '?')}()",
            f"line:      {getattr(leak, 'line', '?')}",
            f"variable:  {getattr(leak, 'var', '?')}",
            f"acquired:  {getattr(leak, 'acquire', '?')}",
            f"released:  {getattr(leak, 'release', '?')}",
            f"type:      {getattr(leak, 'resource_type', '?')}",
            f"severity:  {getattr(leak, 'severity', '?')} "
            f"({getattr(leak, 'confidence', '?')}, rule {getattr(leak, 'rule', '?')})",
            f"reasons:   {reasons}",
            "",
            "Control-flow paths that LEAK:",
            render(leaking),
            "",
            "Control-flow paths that are SAFE:",
            render(safe),
        ]
        if note:
            parts += ["", f"Exception risk: {note}"]
        return "\n".join(parts)

    # ── public API ──────────────────────────────────────────────────────────
    def explain(self, leak: Any, source: str) -> Optional[Guidance]:
        """Explain a proven leak and propose a fix. ``None`` if unavailable."""
        ok, reason = self.available()
        if not ok:
            logger.info("Bedrock advisor unavailable: %s", reason)
            return None

        snippet = _function_snippet(
            source, getattr(leak, "func", ""), getattr(leak, "line", 1) or 1, self.cfg.snippet_chars
        )
        user = (
            f"{_EXPLAIN_INSTRUCTIONS}\n"
            f"─── ANALYZER FINDING (proven) ───\n{self.describe_finding(leak)}\n\n"
            f"─── ENCLOSING FUNCTION SOURCE ───\n{snippet}\n"
        )
        result = self._converse([{"role": "user", "content": [{"text": user}]}], _SYSTEM_PROMPT)
        if not result:
            return None
        return self._to_guidance(_parse_json_payload(result["text"]), result)

    def advise(
        self,
        leak: Any,
        source: str,
        question: str,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[Guidance]:
        """Answer a free-form follow-up about a proven leak.

        ``history`` is prior turn context as ``{"role", "text"}`` entries.
        """
        ok, reason = self.available()
        if not ok:
            logger.info("Bedrock advisor unavailable: %s", reason)
            return None

        snippet = _function_snippet(
            source, getattr(leak, "func", ""), getattr(leak, "line", 1) or 1, self.cfg.snippet_chars
        )
        messages: list[dict[str, Any]] = []
        for turn in history or []:
            role = "assistant" if turn.get("role") == "assistant" else "user"
            text = str(turn.get("text", "")).strip()
            if text:
                messages.append({"role": role, "content": [{"text": text}]})

        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            f"Follow-up question from the developer:\n{question}\n\n"
                            f"─── ANALYZER FINDING (proven) ───\n{self.describe_finding(leak)}\n\n"
                            f"─── ENCLOSING FUNCTION SOURCE ───\n{snippet}\n\n"
                            f"Answer using the same JSON keys as before "
                            f"(summary, why_it_leaks, remediation, suggested_fix, "
                            f"alternatives, caveats, other_suggestions)."
                        )
                    }
                ],
            }
        )

        result = self._converse(messages, _SYSTEM_PROMPT)
        if not result:
            return None
        return self._to_guidance(_parse_json_payload(result["text"]), result)