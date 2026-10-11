"""Abuse guards on the routes that spend Daytona credits / local CPU, and /ask.

Pins three things: these routes honour API_KEY like the mutating routes, they
draw on their own tighter rate-limit bucket, and only one fan-out/sweep batch
may run per process at a time.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import routes as routes_module
from src.api.routes import router

SANDBOX_ROUTES = [
    ("GET", "/api/graph/verify"),
    ("POST", "/api/graph/verify/fanout"),
    ("POST", "/api/graph/verify/sweep"),
    ("GET", "/api/graph/analytics"),
]


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    routes_module._rate_buckets.clear()
    yield
    routes_module._rate_buckets.clear()


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.include_router(router, prefix="/api")
    agent = AsyncMock()
    agent.daytona.verify_graph.return_value = {"ok": True, "valid": True}
    agent.daytona.verify_graph_fanout.return_value = {"ok": True, "sources": []}
    agent.daytona.verify_graph_sweep.return_value = {"ok": True, "results": []}
    agent.get_analytics.return_value = {"totals": {}}
    agent.ask.return_value = {"answer": "a", "status": "answered"}
    neo4j = AsyncMock()
    neo4j.get_sources.return_value = [{"source_doc": "doc", "node_count": 1}]
    neo4j.get_graph_data_by_source.return_value = {"nodes": [], "edges": []}
    neo4j.get_all_graph_data.return_value = {"nodes": [], "edges": []}
    application.state.agent = agent
    application.state.neo4j = neo4j
    application.state.neo4j_available = True
    application.state.ws_manager = None
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), SANDBOX_ROUTES)
async def test_sandbox_routes_require_api_key_when_configured(
    client, monkeypatch, method, path
) -> None:
    monkeypatch.setenv("API_KEY", "s3cret")
    assert (await client.request(method, path)).status_code == 401
    ok = await client.request(method, path, headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), SANDBOX_ROUTES)
async def test_sandbox_routes_open_without_api_key(client, method, path) -> None:
    assert (await client.request(method, path)).status_code == 200


@pytest.mark.asyncio
async def test_ask_requires_api_key_when_configured(client, monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "s3cret")
    body = {"question": "who?"}
    assert (await client.post("/api/ask", json=body)).status_code == 401
    ok = await client.post("/api/ask", json=body, headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_sandbox_rate_limit_is_separate_and_enforced(client, monkeypatch) -> None:
    monkeypatch.setattr(routes_module, "SANDBOX_RATE_LIMIT_MAX", 2)
    assert (await client.get("/api/graph/verify")).status_code == 200
    assert (await client.get("/api/graph/analytics")).status_code == 200
    assert (await client.get("/api/graph/verify")).status_code == 429
    # The ingest/ask bucket is untouched by sandbox traffic.
    assert (await client.post("/api/ask", json={"question": "q"})).status_code == 200


@pytest.mark.asyncio
async def test_only_one_sandbox_batch_runs_at_a_time(app, client) -> None:
    release = asyncio.Event()

    async def slow_fanout(*args, **kwargs):
        await release.wait()
        return {"ok": True, "sources": []}

    app.state.agent.daytona.verify_graph_fanout.side_effect = slow_fanout
    first = asyncio.create_task(client.post("/api/graph/verify/fanout"))
    for _ in range(50):
        if routes_module._sandbox_batch_lock.locked():
            break
        await asyncio.sleep(0.01)
    assert routes_module._sandbox_batch_lock.locked()

    busy = await client.post("/api/graph/verify/sweep")
    assert busy.status_code == 429

    release.set()
    assert (await first).status_code == 200
    assert not routes_module._sandbox_batch_lock.locked()
    assert (await client.post("/api/graph/verify/sweep")).status_code == 200
