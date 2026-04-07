"""Networking utilities for the regression framework."""

from __future__ import annotations

import socket


def allocate_loopback_port() -> int:
    """Reserve an ephemeral loopback port for one regression daemon."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])
