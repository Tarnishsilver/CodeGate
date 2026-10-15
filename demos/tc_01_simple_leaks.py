"""
Test Case 01: Simple Resource Leaks
Description: Resources acquired and never released on the normal execution path.
Expected result: leaks flagged for each function below.
"""
import socket


def leak_file_handle():
    """LEAK: no close() anywhere on this path."""
    f = open("audit.log", "a")
    f.write("System event triggered\n")
    return f


def leak_socket():
    """LEAK: socket never closed, not even on the normal path."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(("127.0.0.1", 8080))
    s.sendall(b"PING")
    return s.recv(1024)


def leak_on_both_paths():
    """LEAK: every exit path skips cleanup."""
    f = open("config.ini")
    data = f.read()
    if data:
        return data.strip()
    return ""