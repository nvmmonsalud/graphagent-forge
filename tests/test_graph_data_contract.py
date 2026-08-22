"""Contract tests for GET /api/graph/data (the "A1" regression).

Response shape MUST always be:
    {nodes, edges, truncated: bool, total_nodes: int, total_edges: int, limit: int|None}

and the HARD INVARIANT is that every id referenced by `edges[].source` /
`edges[].target` also appears in `nodes[].id` — the frontend's D3
`forceLink().id(d => d.id)` hard-crashes (blanking the whole graph) on any
dangling endpoint.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.jobs import JobManager
from src.api import routes as routes_module
from src.api.routes import router

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

REQUIRED_KEYS = {"nodes", "edges", "truncated", "total_nodes", "total_edges", "limit"}


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
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
    application.state.jobs = JobManager(broadcast=None, job_timeout=5)
    return application


@pytest.fixture
async def client(app):
    app.state.jobs.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await app.state.jobs.shutdown()


def _shaped_response(nodes=None, edges=None, total_nodes=0, total_edges=0, limit=500):
    nodes = nodes if nodes is not None else []
    edges = edges if edges is not None else []
    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": total_nodes > len(nodes) or total_edges > len(edges),
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "limit": limit,
    }


# ------------------------------------------------------------------
# Mocked contract shape
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_graph_data_returns_all_six_keys(app, client) -> None:
    app.state.neo4j.get_all_graph_data.return_value = _shaped_response(
        nodes=[{"id": "a", "label": "Alice"}],
        edges=[],
        total_nodes=1,
        total_edges=0,
        limit=500,
    )
    resp = await client.get("/api/graph/data")
    assert resp.status_code == 200
    body = resp.json()
    assert REQUIRED_KEYS <= body.keys()
    assert isinstance(body["truncated"], bool)
    assert isinstance(body["total_nodes"], int)
    assert isinstance(body["total_edges"], int)


@pytest.mark.asyncio
async def test_graph_data_limit_zero_is_422(client) -> None:
    resp = await client.get("/api/graph/data", params={"limit": 0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_graph_data_limit_over_5000_is_422(client) -> None:
    resp = await client.get("/api/graph/data", params={"limit": 9999})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_graph_data_limit_is_honored_and_forwarded(app, client) -> None:
    app.state.neo4j.get_all_graph_data.return_value = _shaped_response(
        nodes=[{"id": f"n{i}", "label": f"N{i}"} for i in range(10)],
        edges=[],
        total_nodes=30,
        total_edges=0,
        limit=10,
    )
    resp = await client.get("/api/graph/data", params={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 10
    assert len(body["nodes"]) == 10
    app.state.neo4j.get_all_graph_data.assert_awaited_with(limit=10)


@pytest.mark.asyncio
async def test_graph_data_edge_endpoints_are_always_within_nodes_mocked(app, client) -> None:
    """Even at the route layer: a response with a dangling edge endpoint is a
    contract violation the test suite should catch, not just trust the mock."""
    nodes = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    edges = [{"source": "a", "target": "b", "type": "RELATES_TO"}]
    app.state.neo4j.get_all_graph_data.return_value = _shaped_response(
        nodes=nodes, edges=edges, total_nodes=2, total_edges=1, limit=500
    )
    resp = await client.get("/api/graph/data")
    body = resp.json()
    node_ids = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


# ------------------------------------------------------------------
# Integration: real Neo4j, truncation + invariant under a real cap
# ------------------------------------------------------------------
@pytest.fixture
async def neo4j():
    from src.graph.neo4j_client import Neo4jClient

    real_client = Neo4jClient(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    await real_client.connect()
    await real_client.init_schema()
    try:
        yield real_client
    finally:
        await real_client.close()


@pytest.mark.integration
@pytest.mark.skipif(
    not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD),
    reason="NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD not set — no live Neo4j to test against",
)
@pytest.mark.asyncio
async def test_graph_data_invariant_holds_under_truncation(neo4j) -> None:
    source_doc = f"test-graph-data-{uuid.uuid4().hex[:8]}"
    node_ids = [f"{source_doc}_n{i}" for i in range(30)]
    nodes = [
        {"id": nid, "label": f"Node {i}", "type": "Concept", "properties": {"summary": ""}}
        for i, nid in enumerate(node_ids)
    ]
    # 29 chain edges + 11 extra = 40 edges, every endpoint declared above.
    edges = [
        {"source": node_ids[i], "target": node_ids[i + 1], "relationship": "NEXT",
         "properties": {}}
        for i in range(29)
    ]
    edges += [
        {"source": node_ids[i], "target": node_ids[(i + 5) % 30], "relationship": "LINKS",
         "properties": {}}
        for i in range(11)
    ]
    assert len(nodes) == 30
    assert len(edges) == 40

    try:
        write_result = await neo4j.write_graph({"nodes": nodes, "edges": edges},
                                                 source_doc=source_doc)
        assert write_result["nodes_written"] == 30
        assert write_result["edges_written"] == 40

        data = await neo4j.get_all_graph_data(limit=5)

        returned_ids = {n["id"] for n in data["nodes"]}
        for edge in data["edges"]:
            assert edge["source"] in returned_ids
            assert edge["target"] in returned_ids

        assert data["truncated"] is True
        assert data["total_nodes"] == 30
        assert data["limit"] == 5
        assert len(data["nodes"]) == 5
    finally:
        await neo4j.delete_source(source_doc)
