"""Interactive / one-shot AI remediation guidance on top of proven findings.

Two entry points, both wired through ``cli.py``:

* ``codegate ask <target>``  — one-shot guidance for each finding.
* ``codegate ai <target>``   — interactive session with follow-up questions.

Both run the deterministic analyzer first and hand *proven* ``Leak`` objects to
the Bedrock advisor. Neither can report a leak the analyzer did not produce.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .analyzer import analyze_file
from .config import CodeGateConfig


def _print_guidance(g, colors, indent: str = "") -> None:
    """Render Guidance as readable terminal output."""
    def w(text: str = "") -> None:
        print(f"{indent}{text}")

    if g.summary:
        w(f"{colors.BOLD}{g.summary}{colors.RESET}")
    if g.why_it_leaks:
        w()
        w(f"{colors.CYAN}Why it leaks{colors.RESET}")
        w(f"  {g.why_it_leaks}")
    if g.remediation:
        w()
        w(f"{colors.CYAN}How to fix{colors.RESET}")
        for line in str(g.remediation).splitlines():
            w(f"  {line}")
    if g.suggested_fix:
        w()
        w(f"{colors.CYAN}Suggested fix{colors.RESET}")
        for line in g.suggested_fix.splitlines():
            w(f"  {colors.GREEN}{line}{colors.RESET}")
    for title, items in (("Alternatives", g.alternatives),
                         ("Caveats", g.caveats),
                         ("Unverified suggestions", g.other_suggestions)):
        if items:
            w()
            w(f"{colors.YELLOW}{title}{colors.RESET}")
            for item in items:
                w(f"  · {item}")
    w()
    w(f"{colors.DIM}advisory from {g.model} · {g.total_tokens} tokens "
      f"(analysis itself used 0){colors.RESET}")


def _load_findings(target: str, config: CodeGateConfig):
    """Analyze a file and return (path, source, leaks)."""
    p = Path(target)
    if not p.is_file():
        raise SystemExit(f"codegate: not a file: {target}")
    source = p.read_text(encoding="utf-8")
    leaks = analyze_file(p, config)
    return p, source, leaks


def _advisor_or_exit(colors):
    """Build the advisor, or exit with an actionable message."""
    from .bedrock import RemediationAdvisor, bedrock_status

    status = bedrock_status()
    if not status["available"]:
        print(f"{colors.RED}AI advisor unavailable:{colors.RESET} {status['reason']}", file=sys.stderr)
        if not status["enabled"]:
            print(f"  Opt in per-run with --ai, or set CODEGATE_AI=1.", file=sys.stderr)
        print(f"  Status: {json.dumps(status)}", file=sys.stderr)
        raise SystemExit(2)
    return RemediationAdvisor()


def _ask_main(argv: list[str], colors) -> int:
    """`codegate ask <target>` — one-shot guidance, no network unless opted in."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="codegate ask",
        description="Explain proven leaks and propose fixes using AWS Bedrock (advisory only)",
    )
    parser.add_argument("target", help="Python file to analyze")
    parser.add_argument("--line", type=int, default=None,
                        help="Only the finding starting on this line")
    parser.add_argument("--limit", type=int, default=5, help="Max findings to explain (default: 5)")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--ai", action="store_true", help="Opt in to the AI advisor for this run")
    args = parser.parse_args(argv)

    if args.ai:
        import os
        os.environ["CODEGATE_AI"] = "1"

    advisor = _advisor_or_exit(colors)
    path, source, leaks = _load_findings(args.target, CodeGateConfig.default())

    if args.line is not None:
        leaks = [lk for lk in leaks if lk.line == args.line]
        if not leaks:
            print(f"{colors.YELLOW}No leak reported at {path}:{args.line}.{colors.RESET}",
                  file=sys.stderr)
            return 1
    if not leaks:
        print(f"{colors.GREEN}{path}: ✓ no leaks — nothing to explain.{colors.RESET}")
        return 0

    selected = leaks[: max(1, args.limit)]
    results = []
    for i, leak in enumerate(selected, 1):
        if not args.json:
            print()
            print(f"{colors.BOLD}[{i}/{len(selected)}] {path}:{leak.line} · "
                  f"{leak.func}() · {leak.rule}{colors.RESET}")
        guidance = advisor.explain(leak, source)
        if guidance is None:
            msg = "advisor returned nothing (model error or unavailable)"
            if not args.json:
                print(f"{colors.RED}  {msg}{colors.RESET}", file=sys.stderr)
            results.append({"finding": leak.to_dict(), "guidance": None, "error": msg})
            continue
        if not args.json:
            _print_guidance(guidance, colors, indent="  ")
        results.append({"finding": leak.to_dict(), "guidance": guidance.to_dict()})

    if args.json:
        print(json.dumps({"ai": True, "results": results}, indent=2))

    if len(leaks) > len(selected):
        print(f"\n{colors.DIM}{len(leaks) - len(selected)} more finding(s) not explained "
              f"(raise --limit).{colors.RESET}", file=sys.stderr)
    return 0


def _ai_main(argv: list[str], colors) -> int:
    """`codegate ai <target>` — interactive remediation session."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="codegate ai",
        description="Interactive remediation guidance over proven findings (AWS Bedrock)",
    )
    parser.add_argument("target", help="Python file to analyze")
    parser.add_argument("--ai", action="store_true", help="Opt in to the AI advisor for this run")
    args = parser.parse_args(argv)

    if args.ai:
        import os
        os.environ["CODEGATE_AI"] = "1"

    advisor = _advisor_or_exit(colors)
    path, source, leaks = _load_findings(args.target, CodeGateConfig.default())

    print(f"{colors.BOLD}CodeGate AI — remediation advisor{colors.RESET} "
          f"({colors.DIM}advisory only; the analyzer decides what is a leak{colors.RESET})")
    print(f"{colors.DIM}model: {advisor.cfg.model_label} · region: {advisor.cfg.region}{colors.RESET}")

    if not leaks:
        print(f"\n{colors.GREEN}{path}: ✓ no leaks found — nothing to fix.{colors.RESET}")
        return 0

    print(f"\n{colors.BOLD}{len(leaks)} finding(s) proven by the CFG engine:{colors.RESET}")
    for i, leak in enumerate(leaks, 1):
        print(f"  {colors.YELLOW}{i}{colors.RESET}. {path}:{leak.line} · "
              f"{leak.func}() · {leak.rule} · {leak.message.splitlines()[0][:60]}")

    print(f"\nType a number to get guidance, then ask follow-ups.")
    print(f"{colors.DIM}  /list  re-list findings · /new N  switch finding · /quit  exit{colors.RESET}\n")

    current_idx: int | None = None
    history: list[dict[str, str]] = []

    def explain(idx: int) -> None:
        nonlocal history
        leak = leaks[idx - 1]
        print(f"\n{colors.BOLD}─── Finding {idx}: {leak.func}() at {path}:{leak.line} ───{colors.RESET}")
        guidance = advisor.explain(leak, source)
        if guidance is None:
            print(f"{colors.RED}Advisor returned nothing.{colors.RESET}", file=sys.stderr)
            return
        _print_guidance(guidance, colors)
        history = [{"role": "user", "text": "Explain this leak and propose a fix."},
                   {"role": "assistant", "text": guidance.summary + " " + guidance.why_it_leaks}]

    while True:
        try:
            raw = input(f"{colors.CYAN}codegate-ai> {colors.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue

        low = raw.lower()
        if low in {"/quit", "/exit", "quit", "exit", ":q"}:
            print("bye.")
            return 0
        if low == "/list":
            for i, leak in enumerate(leaks, 1):
                marker = "→" if i == current_idx else " "
                print(f"  {colors.YELLOW}{marker}{i}{colors.RESET}. {leak.func}() "
                      f"line {leak.line} · {leak.rule}")
            continue
        if low.startswith("/new"):
            parts = raw.split()
            if len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= len(leaks):
                current_idx = int(parts[1])
                history = []
                explain(current_idx)
            else:
                print(f"usage: /new <1-{len(leaks)}>", file=sys.stderr)
            continue

        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(leaks):
                current_idx = idx
                history = []
                explain(idx)
            else:
                print(f"no finding {idx}", file=sys.stderr)
            continue

        if current_idx is None:
            print(f"{colors.YELLOW}Pick a finding number first.{colors.RESET}")
            continue

        leak = leaks[current_idx - 1]
        guidance = advisor.advise(leak, source, raw, history=history)
        if guidance is None:
            print(f"{colors.RED}Advisor returned nothing.{colors.RESET}", file=sys.stderr)
            continue
        _print_guidance(guidance, colors)
        history.append({"role": "user", "text": raw})
        history.append({"role": "assistant",
                        "text": guidance.summary + " " + guidance.why_it_leaks})


def main(argv: list[str], colors) -> int:
    """Dispatch for `codegate ask` / `codegate ai`."""
    if argv and argv[0] == "ask":
        return _ask_main(argv[1:], colors)
    if argv and argv[0] == "ai":
        return _ai_main(argv[1:], colors)
    print("usage: codegate ask <file> | codegate ai <file>", file=sys.stderr)
    return 2