# CodeGate — Static Resource-Leak Analyzer for Python

**After a resource is acquired, is cleanup guaranteed on _every_ reachable path?**

CodeGate is a path-sensitive static analyzer that answers that question for files,
sockets, database connections, and subprocess handles — and rewrites the unsafe code
into a `with` block that is correct by construction.

It is deterministic: no LLM call, no network access, no API key. Same input, same
verdict, every run. An [optional AWS Bedrock layer](#ai-remediation-advisor-optional-aws-bedrock)
can explain a leak the engine has already proven and suggest a fix, but it is off by
default and never participates in detection.

---

## The problem

A leak is rarely a missing `close()` in plain sight. It is cleanup that exists in the
source but is _skipped_ by the way control actually flows:

```python
def read_file(path):
    f = open(path)
    data = f.read()
    if not data:
        return None      # <-- LEAK: f is still open when we leave here
    f.close()
    return data
```

A grep-based check sees `open()` and `close()` and moves on. Ruff's `SIM115` sees the
`open()` and flags it, but not _why_ it leaks. CodeGate builds a control-flow graph,
walks it, and reports the **exact path** that escapes cleanup:

```
✗ leaking path:  f = open(path) → data = f.read() → if not data: → return None
                    └─ 'f' never closed here
✓ safe path:      f = open(path) → data = f.read() → if not data: → f.close() → …
⚠ exception risk: if 'f.read()' (line 12) raises, 'f' leaks — normal paths close it, errors don't.
```

## What it detects

Path-sensitive across the constructs where leaks actually hide:

| Path shape              | Example                                                             |
| ----------------------- | ------------------------------------------------------------------- |
| **Early returns**       | cleanup at the end of the function, skipped by `return` in a branch |
| **Loops**               | handle closed after the loop but not on the path that exits early   |
| **Exceptions**          | `raise` or a re-raise that bypasses the cleanup                     |
| **Conditional cleanup** | closed in the `if` branch, missing in the `else`                    |
| **Aliasing**            | `g = f; g.close()` is credited as closing `f`                       |
| **Reassignment**        | `f = open(); f = open()` — the first handle is lost                 |
| **Ownership escape**    | `return f` is treated as a transfer, not a leak (configurable)      |

Built-in rules cover `open()`, `socket.socket()`, `subprocess.Popen`, `sqlite3.connect`,
HTTP clients, and more — extensible via `codegate/resources.yaml`. Unknown APIs are
handled by duck-typed inference and reported at `potential` confidence rather than
guessed at.

## Quick start

```bash
git clone <your-fork-url> codegate && cd codegate
pip install -e .                 # or: pip install libcst networkx astor graphviz

codegate demos/vuln.py                       # scan a file
codegate . --json                             # scan a tree, machine-readable
codegate demos/vuln.py --cfg                  # print the CFG of leaking functions
codegate demos/vuln.py --fix-dry              # preview the autofix
codegate demos/vuln.py --fix                  # apply it in place
```

Sample output:

```
███ CODEGATE FOUND 1 LEAK ███

┌─ 🔴 DEFINITE RESOURCE LEAK  demos/vuln.py:11 · in read_file() · CODEGATE001 · file
│
│  'f' = open(...) — is not guaranteed to be close()'d
│  leak paths: early return
│  ✗ leaking path:  ... → f = open(path) → data = f.read() → if not data: → return None
│                      └─ 'f' never closed here
│
│  💡 FIX: use a context manager, or close on every branch:
│       with open(...) as f:   # closes automatically
└────────────────────────────────────────────────────────────
```

Exit code is `1` when leaks are found, so it drops straight into a pipeline.

### Autofix

Rewrites are applied with **LibCST**, a concrete-syntax-tree library, so comments and
formatting survive the edit instead of being reformatted away:

```python
# before                      # after
f = open(path)                with open(path) as f:
data = f.read()                    data = f.read()
```

Use `--fix-dry` to preview, `--fix-out DIR` to write to a mirror tree.

## Blocking leaks in CI/CD

CodeGate is built to fail a build, not just print a warning. Three integration points,
all in-repo:

**1. GitHub Actions** — scans every push and pull request, writes inline annotations
onto the diff, and fails the job on a leak.

```yaml
- uses: actions/checkout@v4
- run: pip install -e .
- run: python -m codegate.ci . --ensemble
```

On pull requests it can scan only what changed (`--changed-only --base origin/main`).
Full workflow: [`.github/workflows/codegate.yml`](.github/workflows/codegate.yml).

**2. Reusable GitHub Action** — so other repositories can use it without vendoring
anything. `action.yml` is a composite action exposing `targets`, `changed-only`,
`base`, and `ensemble`:

```yaml
- uses: your-org/codegate@main
  with:
    targets: src/
    changed-only: true
```

**3. Pre-commit hook** — blocks the leak locally, before it ever reaches CI:

```bash
codegate install-hook     # writes .git/hooks/pre-commit
```

The hook scans only staged `.py` files and chains safely if you already have one.

**Ensemble mode** (`--ensemble`) cross-checks findings against `ruff` before
reporting, trading a little speed for far fewer false positives.

## REST API

Analysis is exposed over HTTP so the web dashboard, CI, and editors share one engine.

| Endpoint                     | Purpose                                     |
| ---------------------------- | ------------------------------------------- |
| `POST /api/codegate/analyze` | analyze source, return findings + AST + CFG |
| `POST /analyze-batch`        | analyze many files in one call              |
| `GET /health`                | liveness + cache statistics                 |

```bash
codegate serve            # persistent server with an LRU + Redis cache
```

The persistent server is what makes the dashboard feel instant — a cold one-shot CLI
run costs ~310 ms, while a warm server answers in ~4–28 ms (~1–16 ms cached, ~3.5 ms
per file in batch). See [`docs/ROADMAP.md`](docs/ROADMAP.md) for the full latency table.

## Docker

Containerized and verified on a Linux environment. The image builds the React
frontend in one stage and ships the Python analyzer alongside it in the final stage,
so a single container serves both the UI and the analysis API.

```bash
docker build -t codegate .
docker run -p 3000:3000 codegate          # UI + API on :3000

docker compose up --build                 # app + Redis cache
```

[`setup.sh`](setup.sh) provisions a fresh Ubuntu host end-to-end (system packages,
Node, Scalpel, frontend build, Python venv) and smoke-tests the install.

## Web dashboard

A React + TypeScript front end for reviewing scans: leak findings with the leaking
control-flow path, interactive AST tree and CFG graph views, an ensemble-verification
panel, and an admin view for tracking leak trends across repositories and branches.
It talks to the same Python engine through the REST API.

## AI remediation advisor (optional, AWS Bedrock)

Detection stays deterministic. This is a separate, **opt-in** layer that explains a
leak the analyzer has *already proven* and proposes a fix — it never decides whether a
leak exists, and it never parses code to find one.

```bash
pip install -e ".[ai]"          # adds boto3
export CODEGATE_AI=1
export CODEGATE_BEDROCK_MODEL=anthropic.claude-3-5-sonnet-20241022-v2:0
aws configure                    # or rely on an instance/task role

codegate ask demos/vuln.py --ai      # one-shot guidance per finding
codegate ai   demos/vuln.py --ai      # interactive session
```

`codegate ai` opens a session over the proven findings — pick one by number, then ask
follow-ups:

```
1 finding(s) proven by the CFG engine:
  1. demos/vuln.py:12 · get_user_status() · CODEGATE001 · Resource 'f' not released…

codegate-ai> 1
  Why it leaks
    open(path) binds f; the early return exits the function while f is still open…

  How to fix
    1. Wrap the body in `with open(path) as f:`.
    2. Keep the early return inside the block.

codegate-ai> why not try/finally instead?
  …follow-up answer, with conversation history…
```

Also available over HTTP for the dashboard:

| Endpoint | Purpose |
|---|---|
| `GET /ai-status` | whether the advisor is enabled, plus model and region |
| `POST /advise` | guidance for findings the **server** just proved (`source`, optional `line` / `question`) |

### Why it is built this way

| Property | How it is enforced |
|---|---|
| **Detection is never AI** | `analyzer.py` does not import `bedrock`; the advisor only accepts an already-proven `Leak` object |
| **Off unless asked** | needs *both* `CODEGATE_AI=1` (or `--ai`) *and* a configured model — stray AWS credentials alone start nothing |
| **No finding injection** | `POST /advise` re-analyzes the posted source server-side; client-supplied "findings" are ignored |
| **Cannot overrule the analyzer** | the system prompt forbids disputing the verdict or asserting new leaks; any extra hunch is returned under `other_suggestions`, explicitly unverified |
| **Minimal disclosure** | only the *enclosing function* is sent, never the rest of the file or repo |
| **Fails closed** | every failure returns `None`/a structured status — a Bedrock outage can never break a scan |

Config lives in [`.env.example`](.env.example). See [`codegate/bedrock.py`](codegate/bedrock.py).

## Results

Deterministic analysis benchmarked against an LLM on 19 hand-labeled cases
([`BENCHMARK.md`](BENCHMARK.md)):

| Metric                           | CodeGate                  | LLM (`deepseek-v4-flash`) |
| -------------------------------- | ------------------------- | ------------------------- |
| Precision / Recall / F1          | 1.000 / 0.917 / **0.957** | 1.000 / 1.000 / **1.000** |
| True positives / false negatives | 11 / 1                    | 12 / 0                    |
| LLM tokens consumed              | **0**                     | 8,797                     |
| Cost per run                     | **$0**                    | ~$0.003                   |

The LLM scores marginally higher on this small corpus. CodeGate's case is not raw
accuracy but the other three columns: it is free, reproducible to the bit, needs no
API key or network egress, runs in a sandbox, and cannot hallucinate a path that does
not exist in the CFG. A 19-case corpus is also small enough that one flipped case
moves F1 by ~5% — treat the gap as noise, and the operational properties as the real
result.

## Project layout

```
codegate/            analyzer, CFG walk, autofix, CLI, CI mode, REST API, benchmark
codegate/bedrock.py  optional AI remediation advisor (never imported by the analyzer)
demos/               runnable leak and safe-pattern cases, one file per path shape
frontend/            React dashboard (leak reports, AST/CFG visualizer, admin)
docs/                roadmap, benchmark data, system-design interview notes
scripts/             setup helpers
Dockerfile           multi-stage build: frontend + Python analyzer
action.yml           reusable GitHub Action
```

## Tests

```bash
python -m codegate.cli demos/            # every demo asserts its own expected verdict
python -m codegate.cli demos/ --json
```

The `demos/` corpus doubles as the regression suite — each file pairs a leaking function
with a safe equivalent, and every one is checked against its documented outcome.

## Documentation

| Doc | What it covers |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Scalpel/LibCST division of responsibility, CFG walk design |
| [`BENCHMARK.md`](BENCHMARK.md) | deterministic vs LLM, per-case results |
| [`codegate/resources.yaml`](codegate/resources.yaml) | resource acquire/release rules |
| [`.env.example`](.env.example) | optional Bedrock advisor configuration |

## License

MIT — see [`LICENSE`](LICENSE).
