"""HTTP-surface tests for POST /api/graph/verify/sweep, agent mocked out.

Fixture style deliberately copies test_routes_fanout.py's bare-app setup, so
this route's contract is pinned by a self-contained file.

The route's job: pick the largest source, read ONE payload from Neo4j, hand it
to the executor's sweep WITH an injected broadcast sink, label the payload as
replicated, and pass the result straight back. It computes nothing itself.
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

PAYLOAD = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}]}

SWEEP_RESULT = {
    "ok": True,
    "replicated": True,
    "results": [
        {"n": 1, "ok": True, "sandbox_count": 1, "short_by": 0, "wall_ms": 90,
         "serial_ms": 80, "speedup": 0.89, "boot_ms": {"count": 1, "min": 7, "max": 7, "avg": 7},
         "method": "daytona"},
        {"n": 3, "ok": True, "sandbox_count": 3, "short_by": 0, "wall_ms": 100,
         "serial_ms": 240, "speedup": 2.4, "boot_ms": {"count": 3, "min": 5, "max": 9, "avg": 7},
         "method": "daytona"},
    ],
    "sizes": [1, 3],
    "total_wall_ms": 200,
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


def _wire(app, *, result=SWEEP_RESULT) -> AsyncMock:
    app.state.neo4j.get_sources = AsyncMock(return_value=SOURCES)
    app.state.neo4j.get_graph_data_by_source = AsyncMock(return_value=PAYLOAD)
    app.state.agent.daytona.verify_graph_sweep = AsyncMock(return_value=result)
    return app.state.agent.daytona.verify_graph_sweep


@pytest.mark.asyncio
async def test_reads_one_payload_and_hands_it_to_the_sweep(app, client) -> None:
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [1, 3]})

    assert resp.status_code == 200
    call = sweep.await_args
    assert call is not None
    assert call.args[0] is PAYLOAD
    assert call.kwargs["sizes"] == [1, 3]
    # Exactly one source is read: the sweep replicates one sub-graph.
    assert app.state.neo4j.get_graph_data_by_source.await_count == 1
    assert app.state.neo4j.get_graph_data_by_source.await_args.args[0] == "big"


@pytest.mark.asyncio
async def test_defaults_to_the_rehearsed_sizes(app, client) -> None:
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep")

    assert resp.status_code == 200
    call = sweep.await_args
    assert call is not None
    assert call.kwargs["sizes"] == [1, 3, 6, 10]


@pytest.mark.asyncio
async def test_response_labels_the_payload_as_replicated(app, client) -> None:
    """A judge reading the JSON must not conclude N documents were crawled."""
    _wire(app)

    body = (await client.post("/api/graph/verify/sweep", json={"sizes": [1]})).json()

    assert body["source_doc"] == "big"
    assert body["distinct_sources"] == 1
    assert body["payload_nodes"] == 2
    assert body["payload_edges"] == 1
    assert body["replicated"] is True
    assert body["results"] == SWEEP_RESULT["results"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sizes", [[0], [17], [-3], []])
async def test_sizes_out_of_range_are_rejected(app, client, sizes) -> None:
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": sizes})

    assert resp.status_code == 422
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_sizes_are_deduped_and_sorted(app, client) -> None:
    """The chart's x-axis must be monotonic whatever order the caller sent."""
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [6, 1, 6, 3]})

    assert resp.status_code == 200
    call = sweep.await_args
    assert call is not None
    assert call.kwargs["sizes"] == [1, 3, 6]


@pytest.mark.asyncio
async def test_too_many_sizes_is_rejected(app, client) -> None:
    sweep = _wire(app)

    resp = await client.post(
        "/api/graph/verify/sweep", json={"sizes": [1, 2, 3, 4, 5, 6, 7]}
    )

    assert resp.status_code == 422
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_sweep_never_gains_a_results_key(app, client) -> None:
    """The route forwards the failed envelope; it must not invent results."""
    _wire(app, result={
        "ok": False,
        "error": "sweep unavailable",
        "method": "daytona",
        "wall_ms": 12,
    })

    body = (await client.post("/api/graph/verify/sweep", json={"sizes": [1]})).json()

    assert body["ok"] is False
    assert body["error"] == "sweep unavailable"
    assert "results" not in body


@pytest.mark.asyncio
async def test_no_sources_is_a_failed_sweep_that_never_calls_the_executor(app, client) -> None:
    sweep = _wire(app)
    app.state.neo4j.get_sources = AsyncMock(return_value=[])

    body = (await client.post("/api/graph/verify/sweep", json={"sizes": [1]})).json()

    assert body["ok"] is False
    assert body["error"] == "sweep unavailable"
    assert body["distinct_sources"] == 0
    sweep.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcasts_each_size_as_sweep_update(app, client, ws_manager) -> None:
    """The route injects the sink: the executor never imports a transport."""
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [1]})
    assert resp.status_code == 200

    call = sweep.await_args
    assert call is not None
    sink = call.kwargs["on_event"]
    await sink({"phase": "size_started", "n": 1})
    await sink({"phase": "size_done", "n": 1, "result": {"n": 1, "ok": True}})

    assert [msg["type"] for msg in ws_manager.sent] == ["sweep_update", "sweep_update"]
    assert ws_manager.sent[0]["phase"] == "size_started"
    assert ws_manager.sent[0]["n"] == 1
    assert ws_manager.sent[1]["result"] == {"n": 1, "ok": True}


@pytest.mark.asyncio
async def test_a_missing_ws_manager_still_serves_the_sweep(app, client) -> None:
    """A bare app with no lifespan hook has no ws_manager — that's not a 500."""
    del app.state.ws_manager
    sweep = _wire(app)

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [1]})

    assert resp.status_code == 200
    call = sweep.await_args
    assert call is not None
    await call.kwargs["on_event"]({"phase": "size_started", "n": 1})


@pytest.mark.asyncio
async def test_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [1]})

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_500_body_is_scrubbed(app, client) -> None:
    app.state.neo4j.get_sources = AsyncMock(
        side_effect=RuntimeError("bolt://neo4j:hunter2@db:7687 exploded")
    )

    resp = await client.post("/api/graph/verify/sweep", json={"sizes": [1]})

    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"
    assert "hunter2" not in resp.text
    assert "bolt://" not in resp.text
