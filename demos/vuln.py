"""
Demo: the leak CodeGate should flag, next to safe variants it must NOT flag.

Run:  python -m codegate.cli demos/vuln.py
Expect: exactly one leak, reported on read_file() at the early return.
"""


def read_file(path):
    """LEAK: cleanup happens only on the fall-through path."""
    f = open(path)
    data = f.read()
    if not data:
        return None      # <-- LEAK: f is still open when we leave here
    f.close()
    return data


def safe_with(path):
    """SAFE: context manager releases on every path."""
    with open(path) as f:
        return f.read()


def safe_try_finally(path):
    """SAFE: finally runs even when the try block returns or raises."""
    f = open(path)
    try:
        return f.read()
    finally:
        f.close()


def safe_alias(path):
    """SAFE: closing an alias closes the original handle."""
    f = open(path)
    g = f
    g.close()
    return 1