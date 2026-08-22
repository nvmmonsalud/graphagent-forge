"""HTTP-surface tests for GET /api/graph/analytics, with the agent mocked out.

Fixtures are a deliberate copy of test_routes.py's bare-app setup (minus the
job queue, which this route never touches) so the analytics contract is pinned
by a self-contained file rather than by shared fixtures another workstream may
reshape.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import routes as routes_module
from src.api.routes import router

# A representative payload in the frozen response shape.
ANALYTICS_PAYLOAD = {
    "totals": {"nodes": 3, "edges": 2, "density": 0.66667, "isolated_nodes": 0},
    "node_types": [{"type": "Person", "count": 2}, {"type": "Org", "count": 1}],
    "edge_types": [{"type": "WORKS_AT", "count": 2}],
    "top_degree": [{"id": "b", "label": "Acme", "type": "Org", "degree": 2}],
    "structure": {
        "ok": True,
        "method": "local",
        "duration_ms": 12,
        "components": {"count": 1, "largest": 3, "sizes": [3]},
        "top_pagerank": [{"id": "b", "label": "Acme", "type": "Org", "score": 0.4}],
        "top_betweenness": [{"id": "b", "label": "Acme", "type": "Org", "score": 1.0}],
        "avg_clustering": 0.0,
        "betweenness_reason": None,
    },
}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter's bucket dict is module-level global state."""
    routes_module._rate_buckets.clear()
    yield
    routes_module._rate_buckets.clear()


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.state.agent = AsyncMock()
    application.state.neo4j = AsyncMock()
    application.state.neo4j_available = True
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_graph_analytics_passthrough(app, client) -> None:
    app.state.agent.get_analytics = AsyncMock(return_value=ANALYTICS_PAYLOAD)

    resp = await client.get("/api/graph/analytics")

    assert resp.status_code == 200
    assert resp.json() == ANALYTICS_PAYLOAD
    app.state.agent.get_analytics.assert_awaited_once_with(top=10)


@pytest.mark.asyncio
async def test_graph_analytics_forwards_top(app, client) -> None:
    app.state.agent.get_analytics = AsyncMock(return_value=ANALYTICS_PAYLOAD)

    resp = await client.get("/api/graph/analytics", params={"top": 25})

    assert resp.status_code == 200
    app.state.agent.get_analytics.assert_awaited_once_with(top=25)


@pytest.mark.asyncio
async def test_graph_analytics_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False

    resp = await client.get("/api/graph/analytics")

    assert resp.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("top", [0, 51, -1])
async def test_graph_analytics_rejects_out_of_range_top(app, client, top) -> None:
    app.state.agent.get_analytics = AsyncMock(return_value=ANALYTICS_PAYLOAD)

    resp = await client.get("/api/graph/analytics", params={"top": top})

    assert resp.status_code == 422
    app.state.agent.get_analytics.assert_not_awaited()


@pytest.mark.asyncio
async def test_graph_analytics_500_body_is_scrubbed(app, client) -> None:
    app.state.agent.get_analytics = AsyncMock(
        side_effect=RuntimeError("bolt://neo4j:hunter2@db:7687 exploded")
    )

    resp = await client.get("/api/graph/analytics")

    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"
    assert "bolt" not in resp.text
    assert "hunter2" not in resp.text


@pytest.mark.asyncio
async def test_graph_analytics_surfaces_structural_failure_as_200(app, client) -> None:
    """A structural-tier failure degrades in place — never a 500.

    The Cypher tier can't be unavailable while /graph/* answers at all, so the
    always-present keys stay present and only `structure` reports the failure.
    """
    degraded = dict(ANALYTICS_PAYLOAD)
    degraded["structure"] = {
        "ok": False,
        "error": "analytics unavailable",
        "method": "local",
        "duration_ms": 7,
    }
    app.state.agent.get_analytics = AsyncMock(return_value=degraded)

    resp = await client.get("/api/graph/analytics")

    assert resp.status_code == 200
    body = resp.json()
    assert body["structure"]["ok"] is False
    assert "components" not in body["structure"]
    for key in ("totals", "node_types", "edge_types", "top_degree"):
        assert key in body
