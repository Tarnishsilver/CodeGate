# Demos

Runnable examples, one file per leak shape. Each file pairs a **leaking** function
with a **safe** equivalent, so you can see both what CodeGate flags and what it
correctly leaves alone.

| File | Pattern demonstrated |
|---|---|
| `vuln.py` | the core case — `close()` exists but an early `return` skips it |
| `socket_leak.py` | `socket.socket()` leak via early return |
| `tc_01_simple_leaks.py` | acquired and never released on any path |
| `tc_02_early_returns.py` | multiple early returns jumping over cleanup |
| `tc_03_exception_paths.py` | leaks on exception paths |
| `tc_04_safe_patterns.py` | compliant code — `with`, `try/finally` (expect **no** leaks) |
| `tc_05_multiple_resources.py` | several live resources in one function |
| `tc_06_conditional_branches.py` | closed in one `if` branch, missing in the `else` |
| `tc_07_false_positive_traps.py` | patterns that *look* like leaks but are not |
| `tc_08_loop_reallocations.py` | leaks inside and around loops |
| `tc_09_resource_transfers.py` | ownership transfer vs. genuine leak |
| `tc_10_nested_try_raise.py` | nested `try` with a re-raise that escapes cleanup |
| `bigcode.py` | 12 labeled cases across many shapes, for scale |
| `leak_simulation.py` | live demo — watches RSS and open-fd count climb, then stay flat |

## Run them

```bash
codegate demos/vuln.py                  # human-readable report
codegate demos/vuln.py --cfg            # CFG of the leaking functions
codegate demos/vuln.py --fix-dry        # preview the autofix
codegate demos/ --json                  # scan the whole directory, machine-readable
codegate tests/fixtures --json          # the labeled fixtures behind the test suite

python3 demos/leak_simulation.py        # live RSS / file-descriptor demo
```

Exit code is `1` when leaks are found, so any of these work as a CI gate.