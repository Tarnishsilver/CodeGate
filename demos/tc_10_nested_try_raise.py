"""
Test Case 10: Nested Try / Except with Re-raised Exceptions
Description: Cleanup sits inside an inner try block that an outer handler can
bypass by re-raising, so the handle escapes on the exception path.
Expected result: leak flagged on the raise path.
"""


def complex_transaction_runner(path, mode):
    """LEAK: the except branch re-raises before the handle is released."""
    f = open(path)
    try:
        try:
            data = f.read()
            if not data:
                raise ValueError("empty transaction log")
            return data
        except ValueError:
            raise            # <-- LEAK: f still open when this escapes
    finally:
        f.close()


def safe_transaction_runner(path, mode):
    """SAFE: context manager releases the handle even when the raise escapes."""
    with open(path) as f:
        data = f.read()
        if not data:
            raise ValueError("empty transaction log")
        return data