"""
Test Case 02: Leaks via Early Return
Description: The resource is closed at the end of the function, but early
returns jump over the cleanup statement.
Expected result: leaks flagged -- a naive "does close() exist?" check passes
this file, which is exactly why CodeGate reasons about control flow.
"""


def get_user_status(path, user_id):
    """LEAK: both early returns skip f.close()."""
    f = open(path)
    row = f.read()

    if not row:
        return "NOT_FOUND"       # <-- LEAK: f still open

    if not row[0]:
        return "INACTIVE"        # <-- LEAK: f still open

    f.close()
    return "ACTIVE"              # only this path is clean


def get_user_status_safe(path, user_id):
    """SAFE: a context manager covers every return path."""
    with open(path) as f:
        row = f.read()
        if not row:
            return "NOT_FOUND"
        if not row[0]:
            return "INACTIVE"
        return "ACTIVE"