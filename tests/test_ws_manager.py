"""Tests for src.main.ConnectionManager."""
from __future__ import annotations

import pytest

from src.main import ConnectionManager


class FakeWebSocket:
    """Minimal stand-in with the async surface ConnectionManager touches."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.accepted = False
        self.sent: list[str] = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, message: str):
        if self.fail:
            raise RuntimeError("connection closed")
        self.sent.append(message)


@pytest.mark.asyncio
async def test_disconnect_is_idempotent() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()
    await manager.connect(ws)
    assert ws in manager.active_connections

    manager.disconnect(ws)
    assert ws not in manager.active_connections

    # Second call must not raise.
    manager.disconnect(ws)
    assert ws not in manager.active_connections


@pytest.mark.asyncio
async def test_disconnect_never_connected_is_safe() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()
    manager.disconnect(ws)  # never connected — still safe
    assert ws not in manager.active_connections


@pytest.mark.asyncio
async def test_broadcast_removes_failing_sockets() -> None:
    manager = ConnectionManager()
    good = FakeWebSocket()
    bad = FakeWebSocket(fail=True)
    await manager.connect(good)
    await manager.connect(bad)

    await manager.broadcast({"type": "graph_update", "nodes": [], "edges": []})

    assert bad not in manager.active_connections
    assert good in manager.active_connections
    assert len(good.sent) == 1


@pytest.mark.asyncio
async def test_broadcast_sends_json_serialized_message() -> None:
    manager = ConnectionManager()
    ws = FakeWebSocket()
    await manager.connect(ws)

    await manager.broadcast({"type": "graph_update", "nodes": [{"id": "n1"}], "edges": []})

    assert len(ws.sent) == 1
    assert '"type": "graph_update"' in ws.sent[0] or '"type":"graph_update"' in ws.sent[0]
