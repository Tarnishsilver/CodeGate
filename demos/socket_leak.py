"""
Demo: socket resource leak detected through an early return.

Run:  python -m codegate.cli demos/socket_leak.py
"""
import socket


def fetch(host, port):
    """LEAK: the empty-response return exits without closing the socket."""
    s = socket.socket()
    s.connect((host, port))
    data = s.recv(1024)
    if not data:
        return None               # <-- LEAK: s still open
    s.close()
    return data


def fetch_safe(host, port):
    """SAFE: context manager releases the socket on every path."""
    with socket.socket() as s:
        s.connect((host, port))
        data = s.recv(1024)
        if not data:
            return None
        return data