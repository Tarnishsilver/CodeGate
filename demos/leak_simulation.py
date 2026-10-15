"""Live resource-leak simulation for judges/demos (stdlib only).

Runs a deliberately LEAKING workload (unclosed sqlite3 :memory: connections
+ unclosed sockets, kept alive like a real request handler dropping handles),
then the FIXED equivalent, while sampling process RSS and open-fd count.

    python3 demos/leak_simulation.py                 # both modes + CodeGate check
    python3 demos/leak_simulation.py --mode leak     # leaking version only
    python3 demos/leak_simulation.py --iters 400 --rows 1500
    python3 demos/leak_simulation.py --json-out /tmp/sim.json

What the judge sees: RSS and fd count climb steadily in leak mode and stay
flat in fixed mode, plus CodeGate flagging the exact leaking lines.
"""

import argparse
import gc
import json
import os
import resource
import socket
import sqlite3
import sys
import time

# Handles a careless request handler "forgets" — kept alive so the leak is real.
_LEAKED_CONNS: list = []
_LEAKED_SOCKETS: list = []

ROW = ("req-%d", "x" * 96)  # ~100 bytes per row


def rss_mb() -> float:
    """Current resident set size in MB (Linux /proc, no third-party deps)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def fd_count() -> int:
    try:
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return -1


def fd_limit() -> int:
    try:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return 1024


def do_work(conn: sqlite3.Connection, nrows: int, seed: int) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS events (id TEXT, payload TEXT)")
    conn.executemany(
        "INSERT INTO events VALUES (?, ?)",
        [(f"req-{seed}-{i}", "x" * 96) for i in range(nrows)],
    )


def leak_iteration(i: int, nrows: int) -> None:
    """Deliberately leaking handler: handles are dropped, never closed."""
    conn = sqlite3.connect(":memory:")
    do_work(conn, nrows, i)
    _LEAKED_CONNS.append(conn)  # leaked: connection (+its pages) stays alive
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _LEAKED_SOCKETS.append(s)  # leaked: fd stays open forever


def fixed_iteration(i: int, nrows: int) -> None:
    """Same work, handles always released."""
    conn = sqlite3.connect(":memory:")
    try:
        do_work(conn, nrows, i)
        conn.commit()
    finally:
        conn.close()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        pass  # ... use the socket ...
    finally:
        s.close()


def bar(growth_mb: float, per_block: float = 2.0, width: int = 40) -> str:
    n = min(width, int(max(0.0, growth_mb) / per_block))
    return "#" * n + "." * (width - n)


def run_mode(label: str, fn, iters: int, nrows: int, step: int) -> dict:
    print(f"\n--- {label} ---", flush=True)
    print(f"{'iter':>6} | {'RSS MB':>10} | {'+MB':>7} | {'FDs':>5} | {'+FD':>4} | growth", flush=True)
    r0, f0 = rss_mb(), fd_count()
    t0 = time.perf_counter()
    for i in range(1, iters + 1):
        fn(i, nrows)
        if i % step == 0 or i == iters:
            r, f = rss_mb(), fd_count()
            print(
                f"{i:>6} | {r:>10.1f} | {r - r0:>+7.1f} | {f:>5} | {f - f0:>+4} | "
                f"{bar(r - r0)}",
                flush=True,
            )
    dt = time.perf_counter() - t0
    r1, f1 = rss_mb(), fd_count()
    return {
        "rss_start_mb": round(r0, 1),
        "rss_end_mb": round(r1, 1),
        "rss_delta_mb": round(r1 - r0, 1),
        "fd_start": f0,
        "fd_end": f1,
        "fd_delta": f1 - f0,
        "seconds": round(dt, 1),
    }


LEAKY_SNIPPET = '''
import socket
import sqlite3


def handle_request(db_path, flag):
    conn = sqlite3.connect(db_path)
    s = socket.socket()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    if flag:
        return None  # LEAK: conn and s never closed on this path
    conn.close()
    s.close()
    return True
'''


def codegate_check() -> dict:
    print("\n--- CodeGate static analysis of the leaking handler above ---", flush=True)
    try:
        from codegate.analyzer import analyze_source
    except Exception as e:  # pragma: no cover - degraded demo mode
        print(f"CodeGate import failed ({e}); skipping static check.", flush=True)
        return {"available": False, "findings": []}
    leaks = analyze_source(LEAKY_SNIPPET, filename="leaky_handler.py")
    path = [lk for lk in leaks if lk.kind in ("path", "path+exception")]
    for lk in path:
        print(
            f"  [{lk.rule} | {lk.confidence}] {lk.acquire}(...) never released "
            f"(line {lk.acquire_line}, {lk.func}())",
            flush=True,
        )
    print(f"  -> {len(path)} definite leak(s) detected before running anything.", flush=True)
    return {
        "available": True,
        "findings": [
            {"rule": lk.rule, "confidence": lk.confidence,
             "acquire": lk.acquire, "line": lk.acquire_line}
            for lk in path
        ],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live resource-leak simulation.")
    ap.add_argument("--mode", choices=["leak", "fixed", "both"], default="both")
    ap.add_argument("--iters", type=int, default=300)
    ap.add_argument("--rows", type=int, default=1500)
    ap.add_argument("--step", type=int, default=0)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--no-codegate", action="store_true")
    args = ap.parse_args(argv)
    step = args.step or max(1, args.iters // 25)

    print("CodeGate leak simulation: each op opens an sqlite3 :memory: DB (+~rows) and a socket.",
          flush=True)
    print(f"Config: iters={args.iters} rows/op={args.rows} fd_limit={fd_limit()}", flush=True)

    results: dict = {}
    if args.mode in ("leak", "both"):
        results["leak"] = run_mode("LEAK MODE (handles dropped, never closed)",
                                   leak_iteration, args.iters, args.rows, step)
    if args.mode == "both":
        # Harness hygiene (not part of the story): release the leaked handles so
        # the fixed phase starts from a clean baseline and the contrast is exact.
        for c in _LEAKED_CONNS:
            try:
                c.close()
            except Exception:
                pass
        for s in _LEAKED_SOCKETS:
            try:
                s.close()
            except Exception:
                pass
        _LEAKED_CONNS.clear()
        _LEAKED_SOCKETS.clear()
        gc.collect()
        print("(harness: leak-phase handles dropped; the allocator may keep absolute "
              "RSS high - the +MB/+FD deltas are the signal)", flush=True)
    if args.mode in ("fixed", "both"):
        results["fixed"] = run_mode("FIXED MODE (every handle closed)", fixed_iteration,
                                    args.iters, args.rows, step)

    print("\n=== SUMMARY ===", flush=True)
    for mode, r in results.items():
        print(f"{mode:>6}: RSS {r['rss_start_mb']:.1f} -> {r['rss_end_mb']:.1f} MB "
              f"(+{r['rss_delta_mb']:.1f}) | FDs {r['fd_start']} -> {r['fd_end']} "
              f"(+{r['fd_delta']}) | {r['seconds']}s", flush=True)
    if "leak" in results and results["leak"]["fd_delta"] > 0:
        per_op = results["leak"]["fd_delta"] / max(1, args.iters)
        limit = fd_limit()
        print(f"Fd-exhaustion projection: ~{per_op:.2f} leaked fd(s)/op -> "
              f"'Too many open files' in ~{int((limit - results['leak']['fd_end']) / per_op)} "
              f"more ops at this box's limit ({limit}); "
              f"~{int((1024 - results['leak']['fd_end']) / per_op)} ops at the "
              f"typical service default of 1024 fds.", flush=True)
    if "leak" in results and "fixed" in results:
        print(f"Verdict: leaking code grew +{results['leak']['rss_delta_mb']:.1f} MB vs "
              f"fixed +{results['fixed']['rss_delta_mb']:.1f} MB over {args.iters} ops.",
              flush=True)

    results["codegate"] = {} if args.no_codegate else codegate_check()
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Results written to {args.json_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
