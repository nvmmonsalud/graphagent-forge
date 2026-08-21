"""Tests for the merge/duplicates/delete_source HTTP surface in src.api.routes.

Mirrors the fixture pattern in tests/test_routes.py: bare FastAPI + router at
/api, AsyncMock agent/neo4j on state, neo4j_available=True, ASGITransport
client, rate-limiter reset. These target the CONTRACT in the task brief, not
necessarily the current state of src/ — some assertions may fail until the
parallel src/ work lands (e.g. neo4j_client.py's new delete_source shape).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import routes as routes_module
from src.api.routes import router


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


# ------------------------------------------------------------------
# POST /api/graph/merge — validation
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_422_for_single_id(client) -> None:
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a"]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_merge_422_for_duplicate_ids_collapsing_below_two(client) -> None:
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "a"]})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_merge_422_for_canonical_id_not_in_node_ids(client) -> None:
    resp = await client.post(
        "/api/graph/merge",
        json={"node_ids": ["a", "b"], "canonical_id": "c"},
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------
# POST /api/graph/merge — not-found
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_404_when_agent_reports_not_found(app, client) -> None:
    app.state.agent.merge_entities.return_value = {
        "error": "not_found",
        "missing": ["ghost-1"],
    }
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "ghost-1"]})
    assert resp.status_code == 404
    assert "ghost-1" in resp.json()["detail"]


# ------------------------------------------------------------------
# POST /api/graph/merge — success passthrough
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_200_passthrough(app, client) -> None:
    success = {
        "merged": 2,
        "canonical_id": "a",
        "aliases": ["Acme Inc"],
        "removed_ids": ["b"],
        "canonical": {
            "id": "a",
            "label": "Acme",
            "type": "Org",
            "summary": "a company",
            "source_docs": ["doc1", "doc2"],
            "aliases": ["Acme Inc"],
        },
    }
    app.state.agent.merge_entities.return_value = success
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "b"]})
    assert resp.status_code == 200
    assert resp.json() == success


@pytest.mark.asyncio
async def test_merge_calls_agent_with_deduped_ids_and_canonical(app, client) -> None:
    app.state.agent.merge_entities.return_value = {
        "merged": 2,
        "canonical_id": "a",
        "aliases": [],
        "removed_ids": ["b"],
        "canonical": {"id": "a"},
    }
    resp = await client.post(
        "/api/graph/merge", json={"node_ids": ["a", "b"], "canonical_id": "a"}
    )
    assert resp.status_code == 200
    args, kwargs = app.state.agent.merge_entities.call_args
    # node_ids may arrive positionally or by keyword depending on route impl.
    called_ids = args[0] if args else kwargs["node_ids"]
    assert set(called_ids) == {"a", "b"}


# ------------------------------------------------------------------
# POST /api/graph/merge — 503 gate, auth
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "b"]})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_merge_401_when_api_key_configured_and_missing(monkeypatch, client) -> None:
    monkeypatch.setenv("API_KEY", "super-secret")
    resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "b"]})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_merge_200_with_correct_api_key(monkeypatch, app, client) -> None:
    monkeypatch.setenv("API_KEY", "super-secret")
    app.state.agent.merge_entities.return_value = {
        "merged": 2,
        "canonical_id": "a",
        "aliases": [],
        "removed_ids": ["b"],
        "canonical": {"id": "a"},
    }
    resp = await client.post(
        "/api/graph/merge",
        json={"node_ids": ["a", "b"]},
        headers={"X-API-Key": "super-secret"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_merge_not_rate_limited(app, client) -> None:
    """MUTATING_DEP has no rate_limit dependency — 11 merge calls should never 429."""
    app.state.agent.merge_entities.return_value = {
        "merged": 2,
        "canonical_id": "a",
        "aliases": [],
        "removed_ids": ["b"],
        "canonical": {"id": "a"},
    }
    statuses = []
    for _ in range(11):
        resp = await client.post("/api/graph/merge", json={"node_ids": ["a", "b"]})
        statuses.append(resp.status_code)
    assert 429 not in statuses


# ------------------------------------------------------------------
# GET /api/graph/duplicates
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_duplicates_200_passthrough(app, client) -> None:
    body = {
        "groups": [
            {"norm_label": "kimi ai", "tier": 1, "nodes": [{"id": "a"}, {"id": "b"}]},
            {"norm_label": None, "tier": 2, "similarity": 0.93,
             "nodes": [{"id": "c"}, {"id": "d"}]},
        ],
        "tier2_reason": None,
    }
    app.state.agent.find_duplicates.return_value = body
    resp = await client.get("/api/graph/duplicates")
    assert resp.status_code == 200
    assert resp.json() == body


@pytest.mark.asyncio
async def test_duplicates_422_for_limit_zero(client) -> None:
    resp = await client.get("/api/graph/duplicates", params={"limit": 0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicates_422_for_limit_over_100(client) -> None:
    resp = await client.get("/api/graph/duplicates", params={"limit": 101})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_duplicates_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.get("/api/graph/duplicates")
    assert resp.status_code == 503


# ------------------------------------------------------------------
# DELETE /api/sources — new membership-aware semantics
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_source_200_when_only_memberships_and_edges_touched(app, client) -> None:
    """A shared node: deleted_nodes==0 but memberships/edges were touched -> 200."""
    app.state.neo4j.delete_source.return_value = {
        "deleted_nodes": 0,
        "removed_memberships": 2,
        "deleted_edges": 1,
    }
    resp = await client.request(
        "DELETE", "/api/sources", params={"source_doc": "shared-doc"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "deleted_nodes": 0,
        "removed_memberships": 2,
        "deleted_edges": 1,
    }


@pytest.mark.asyncio
async def test_delete_source_404_when_all_zero(app, client) -> None:
    app.state.neo4j.delete_source.return_value = {
        "deleted_nodes": 0,
        "removed_memberships": 0,
        "deleted_edges": 0,
    }
    resp = await client.request(
        "DELETE", "/api/sources", params={"source_doc": "nope"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_source_404_when_keys_missing_default_zero(app, client) -> None:
    app.state.neo4j.delete_source.return_value = {}
    resp = await client.request(
        "DELETE", "/api/sources", params={"source_doc": "nope"}
    )
    assert resp.status_code == 404
