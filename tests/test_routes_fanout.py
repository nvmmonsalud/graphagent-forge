"""HTTP-surface tests for POST /api/graph/verify/fanout, agent mocked out.

Fixture style is a deliberate copy of test_routes_analytics.py's bare-app
setup, so this route's contract is pinned by a self-contained file.

The route's job: pick the sources, read one payload each from Neo4j, hand them
to the executor's fan-out WITH an injected broadcast sink, and pass the result
straight back. It does not compute anything itself.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import routes as routes_module
from src.api.routes import router

SOURCES = [
    {"source_doc": "big", "node_count": 40, "updated_at": "2026-09-01"},
    {"source_doc": "mid", "node_count": 30, "updated_at": "2026-09-01"},
    {"source_doc": "small", "node_count": 20, "updated_at": "2026-09-01"},
]

FANOUT_PAYLOAD = {
    "ok": True,
    "sources": [
        {"source": "big", "ok": True, "valid": True, "node_count": 40, "edge_count": 39,
         "duration_ms": 120, "boot_ms": 96, "sandbox_id": "sbx-1", "method": "daytona"},
    ],
    "source_count": 1,
    "sandbox_count": 1,
    "wall_ms": 180,
    "serial_ms": 120,
    "speedup": 0.67,
    "concurrency": 6,
    "boot_ms": {"count": 1, "min": 96, "max": 96, "avg": 96},
    "method": "daytona",
}


class _RecordingManager:
    """Stands in for ConnectionManager: records what the route broadcasts."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def broadcast(self, data: dict) -> None:
        self.sent.append(data)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter's bucket dict is module-level global state."""
    routes_module._rate_buckets.clear()
    yield
    routes_module._rate_buckets.clear()


@pytest.fixture
def ws_manager() -> _RecordingManager:
    return _RecordingManager()


@pytest.fixture
def app(ws_manager) -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.state.agent = AsyncMock()
    application.state.neo4j = AsyncMock()
    application.state.neo4j_available = True
    application.state.ws_manager = ws_manager
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _wire(app, *, payload=FANOUT_PAYLOAD) -> AsyncMock:
    app.state.neo4j.get_sources = AsyncMock(return_value=SOURCES)
    app.state.neo4j.get_graph_data_by_source = AsyncMock(
        side_effect=lambda source_doc: {"nodes": [{"id": source_doc}], "edges": []}
    )
    app.state.agent.daytona.verify_graph_fanout = AsyncMock(return_value=payload)
    return app.state.agent.daytona.verify_graph_fanout


@pytest.mark.asyncio
async def test_fans_out_one_payload_per_source(app, client) -> None:
    fan_out = _wire(app)

    resp = await client.post("/api/graph/verify/fanout")

    assert resp.status_code == 200
    assert resp.json() == FANOUT_PAYLOAD
    call = fan_out.await_args
    assert call is not None
    assert list(call.args[0]) == ["big", "mid", "small"]
    assert app.state.neo4j.get_graph_data_by_source.await_count == 3


@pytest.mark.asyncio
async def test_limit_caps_how_many_sources_are_read(app, client) -> None:
    fan_out = _wire(app)

    resp = await client.post("/api/graph/verify/fanout", params={"limit": 2})

    assert resp.status_code == 200
    call = fan_out.await_args
    assert call is not None
    assert list(call.args[0]) == ["big", "mid"]
    assert app.state.neo4j.get_graph_data_by_source.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 17, -3])
async def test_limit_out_of_range_is_rejected(app, client, limit) -> None:
    fan_out = _wire(app)

    resp = await client.post("/api/graph/verify/fanout", params={"limit": limit})

    assert resp.status_code == 422
    fan_out.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcasts_each_sandbox_event_as_fanout_update(app, client, ws_manager) -> None:
    """The route injects the sink: the executor never imports a transport."""
    fan_out = _wire(app)

    resp = await client.post("/api/graph/verify/fanout")
    assert resp.status_code == 200

    call = fan_out.await_args
    assert call is not None
    sink = call.kwargs["on_event"]
    await sink({"phase": "started", "source": "big"})
    await sink({"phase": "done", "source": "big", "result": {"ok": True}})

    assert [msg["type"] for msg in ws_manager.sent] == ["fanout_update", "fanout_update"]
    assert ws_manager.sent[0]["phase"] == "started"
    assert ws_manager.sent[0]["source"] == "big"
    assert ws_manager.sent[1]["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_a_missing_ws_manager_still_serves_the_audit(app, client) -> None:
    """A bare app with no lifespan hook has no ws_manager — that's not a 500."""
    del app.state.ws_manager
    fan_out = _wire(app)

    resp = await client.post("/api/graph/verify/fanout")

    assert resp.status_code == 200
    call = fan_out.await_args
    assert call is not None
    await call.kwargs["on_event"]({"phase": "started", "source": "big"})  # must not raise


@pytest.mark.asyncio
async def test_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False

    resp = await client.post("/api/graph/verify/fanout")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_500_body_is_scrubbed(app, client) -> None:
    app.state.neo4j.get_sources = AsyncMock(
        side_effect=RuntimeError("bolt://neo4j:hunter2@db:7687 exploded")
    )

    resp = await client.post("/api/graph/verify/fanout")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"
    assert "hunter2" not in resp.text
    assert "bolt://" not in resp.text
