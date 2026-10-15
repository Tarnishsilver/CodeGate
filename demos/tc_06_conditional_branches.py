"""
Test Case 06: Conditional Cleanup Paths
Description: The resource is closed in one branch of an if/else but missed in
the other, so the leak only fires on one branch.
Expected result: leak flagged -- the close() exists in the source text, but not
on every reachable path.
"""


def sync_data(path, mode):
    """LEAK: the else branch returns without closing the handle."""
    f = open(path, "r")
    if mode == "full":
        data = f.read()
        f.close()
        return data
    else:
        return None                # <-- LEAK: f still open


def sync_data_safe(path, mode):
    """SAFE: context manager closes on both branches."""
    with open(path, "r") as f:
        if mode == "full":
            return f.read()
        return None