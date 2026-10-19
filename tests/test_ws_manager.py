"""Tests for src.main.ConnectionManager."""
from __future__ import annotations

import asyncio

import pytest

from src.main import ConnectionManager


class FakeWebSocket:
    """Minimal stand-in with the async surface ConnectionManager touches."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.accepted = False
        self.sent: list[str] = []
        self.closed = False

    async def close(self, code: int):
        self.closed = True

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
    assert bad.closed
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


@pytest.mark.asyncio
async def test_broadcast_drops_stalled_socket_without_blocking_others(monkeypatch) -> None:
    import src.main as main_module

    monkeypatch.setattr(main_module, "WS_SEND_TIMEOUT", 0.05)

    class StalledWebSocket(FakeWebSocket):
        async def send_text(self, message: str):
            await asyncio.sleep(10)

    manager = ConnectionManager()
    good, stalled = FakeWebSocket(), StalledWebSocket()
    await manager.connect(good)
    await manager.connect(stalled)

    await asyncio.wait_for(manager.broadcast({"type": "graph_update"}), timeout=1)

    assert stalled not in manager.active_connections
    assert stalled.closed
    assert len(good.sent) == 1


@pytest.mark.asyncio
async def test_concurrent_handshakes_reserve_capacity(monkeypatch) -> None:
    import src.main as main_module

    monkeypatch.setattr(main_module, "MAX_WS_CONNECTIONS", 1)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowAcceptWebSocket(FakeWebSocket):
        async def accept(self):
            started.set()
            await release.wait()
            await super().accept()

    manager = ConnectionManager()
    first = SlowAcceptWebSocket()
    connecting = asyncio.create_task(manager.connect(first))
    await started.wait()
    others = [FakeWebSocket() for _ in range(19)]
    assert await asyncio.gather(*(manager.connect(ws) for ws in others)) == [False] * 19
    assert not any(ws.accepted for ws in others)
    assert manager.is_full()
    assert manager.active_connections == []
    release.set()
    assert await connecting
    assert manager.active_connections == [first]
    assert manager.is_full()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_accept_releases_capacity(monkeypatch, cancel) -> None:
    import src.main as main_module

    monkeypatch.setattr(main_module, "MAX_WS_CONNECTIONS", 1)
    started, release = asyncio.Event(), asyncio.Event()

    class FailedAcceptWebSocket(FakeWebSocket):
        async def accept(self):
            started.set()
            await release.wait()
            raise RuntimeError("accept failed")

    manager = ConnectionManager()
    connecting = asyncio.create_task(manager.connect(FailedAcceptWebSocket()))
    await started.wait()
    assert manager.is_full()
    if cancel:
        connecting.cancel()
    else:
        release.set()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await connecting
    assert not manager.is_full()
    assert manager.active_connections == []
    assert await manager.connect(FakeWebSocket())


@pytest.mark.asyncio
async def test_stalled_close_is_bounded_and_releases_capacity(monkeypatch) -> None:
    import src.main as main_module

    monkeypatch.setattr(main_module, "WS_CLOSE_TIMEOUT", 0.01)

    class StalledCloseWebSocket(FakeWebSocket):
        async def close(self, code: int):
            self.closed = True
            await asyncio.Event().wait()

    manager = ConnectionManager()
    ws = StalledCloseWebSocket(fail=True)
    await manager.connect(ws)
    await asyncio.wait_for(manager.broadcast({"type": "graph_update"}), timeout=1)
    assert ws.closed
    assert ws not in manager.active_connections
