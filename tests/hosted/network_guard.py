"""Fail-closed Python socket boundary for the isolated H1 child process only.

This is an accidental-egress guard, not an operating-system security sandbox.
Only fixed counters are persisted; addresses, request data and exceptions are not.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path


def install_network_guard(*, allowed_ports: set[int], listen_port: int, report_path: Path):
    if not allowed_ports or any(type(p) is not int or not 1 <= p <= 65535 for p in allowed_ports):
        raise ValueError("invalid H1 network configuration")
    if type(listen_port) is not int or not 1 <= listen_port <= 65535:
        raise ValueError("invalid H1 network configuration")
    counters = {"connect_allowed": 0, "bind_allowed": 0, "dns_allowed": 0, "denied": 0}
    lock = threading.Lock()

    def record(key=None):
        with lock:
            if key:
                counters[key] = min(counters[key] + 1, 1000000)
            data = json.dumps({"version": 1, **counters}).encode()
            pending = report_path.with_suffix(".pending")
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(pending, report_path)

    def literal(host):
        return host in ("127.0.0.1", b"127.0.0.1")

    def deny():
        record("denied")
        raise PermissionError("H1 network policy denied operation")

    def audit(event, args):
        if event in {"socket.connect", "socket.bind", "socket.sendto"}:
            sock = args[0]
            if sock.family == socket.AF_UNIX:
                return  # asyncio uses a local socketpair for wakeups.
            address = args[-1]
            ports = {listen_port} if event == "socket.bind" else allowed_ports
            if (sock.family != socket.AF_INET or not isinstance(address, tuple)
                    or len(address) != 2 or not literal(address[0]) or address[1] not in ports):
                deny()
            record("bind_allowed" if event == "socket.bind" else "connect_allowed")
        elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
            if not literal(args[0]):
                deny()
            if event == "socket.getaddrinfo" and args[1] not in allowed_ports:
                deny()
            record("dns_allowed")
        elif event == "socket.getnameinfo":
            # Reverse resolution is unnecessary for this literal-only harness.
            deny()

    record()
    sys.addaudithook(audit)
