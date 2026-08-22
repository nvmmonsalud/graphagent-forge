"""Tests for the /api/history HTTP surface, with agent/neo4j mocked out.

Its own bare app (rather than the fixture in tests/test_routes.py) so the
history store is wired explicitly and this file owns its state: the routes
must be exercisable without a lifespan hook, a Neo4j, or a job queue.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.history import QueryHistory
from src.api import routes as routes_module
from src.api.routes import router
from tests.test_history import PUBLIC_KEYS

ANSWER = {
    "answer": "It builds knowledge graphs.",
    "status": "answered",
    "context_nodes": ["GraphAgent Forge"],
    "sources": [{"id": "n1", "label": "GraphAgent Forge", "score": 0.9}],
    "source_docs": ["README"],
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
    application.state.agent.ask.return_value = dict(ANSWER)
    application.state.neo4j = AsyncMock()
    application.state.neo4j_available = True
    # No JobManager here on purpose — nothing under /api/history touches it.
    application.state.history = QueryHistory()
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(client, question: str = "What is it?") -> dict:
    resp = await client.post("/api/ask", json={"question": question})
    assert resp.status_code == 200
    entries = (await client.get("/api/history")).json()["entries"]
    return entries[0]


# ------------------------------------------------------------------
# Recording via /ask
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ask_records_exactly_one_entry(app, client) -> None:
    resp = await client.post("/api/ask", json={"question": "What is GraphAgent Forge?"})
    assert resp.status_code == 200
    assert resp.json() == ANSWER  # the answer body is untouched by recording

    body = (await client.get("/api/history")).json()
    assert body["total"] == 1
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert set(entry) == PUBLIC_KEYS
    assert entry["question"] == "What is GraphAgent Forge?"
    assert entry["answer"] == ANSWER["answer"]
    assert entry["status"] == "answered"
    assert entry["context_nodes"] == ["GraphAgent Forge"]
    assert entry["source_docs"] == ["README"]
    assert entry["saved"] is False
    assert isinstance(entry["duration_ms"], int) and entry["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_ask_failure_records_nothing(app, client) -> None:
    app.state.agent.ask.side_effect = RuntimeError("boom")
    resp = await client.post("/api/ask", json={"question": "explode"})
    assert resp.status_code == 500
    assert (await client.get("/api/history")).json()["total"] == 0


@pytest.mark.asyncio
async def test_history_failure_never_fails_the_answer(app, client) -> None:
    class Exploding:
        def record(self, **kwargs):
            raise RuntimeError("store is broken")

    app.state.history = Exploding()
    resp = await client.post("/api/ask", json={"question": "still works?"})
    assert resp.status_code == 200
    assert resp.json() == ANSWER


# ------------------------------------------------------------------
# GET /api/history
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_history_shape_and_newest_first_ordering(client) -> None:
    for i in range(3):
        assert (await client.post("/api/ask", json={"question": f"q{i}"})).status_code == 200

    body = (await client.get("/api/history")).json()
    assert set(body) == {"entries", "total", "saved_count"}
    assert body["total"] == 3
    assert body["saved_count"] == 0
    assert [e["question"] for e in body["entries"]] == ["q2", "q1", "q0"]


@pytest.mark.asyncio
async def test_history_limit_param(client) -> None:
    for i in range(4):
        await client.post("/api/ask", json={"question": f"q{i}"})

    body = (await client.get("/api/history?limit=2")).json()
    assert [e["question"] for e in body["entries"]] == ["q3", "q2"]
    assert body["total"] == 4  # total counts the store, not the page

    assert (await client.get("/api/history?limit=0")).status_code == 422
    assert (await client.get("/api/history?limit=101")).status_code == 422


@pytest.mark.asyncio
async def test_history_empty_store(client) -> None:
    body = (await client.get("/api/history")).json()
    assert body == {"entries": [], "total": 0, "saved_count": 0}


@pytest.mark.asyncio
async def test_history_get_is_outside_the_rate_bucket(client) -> None:
    """11 consecutive reads: /ingest/* and /ask share a 10/min per-IP bucket,
    and a polling history panel must never be the thing that exhausts it."""
    await client.post("/api/ask", json={"question": "seed"})
    for _ in range(11):
        assert (await client.get("/api/history")).status_code == 200


# ------------------------------------------------------------------
# Save / unsave / delete
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_then_unsave(client) -> None:
    entry = await _seed(client)

    saved = await client.post(f"/api/history/{entry['id']}/save")
    assert saved.status_code == 200
    assert saved.json()["saved"] is True
    assert saved.json()["id"] == entry["id"]
    assert (await client.get("/api/history")).json()["saved_count"] == 1

    unsaved = await client.delete(f"/api/history/{entry['id']}/save")
    assert unsaved.status_code == 200
    assert unsaved.json()["saved"] is False
    assert (await client.get("/api/history")).json()["saved_count"] == 0


@pytest.mark.asyncio
async def test_save_is_idempotent(client) -> None:
    entry = await _seed(client)
    for _ in range(3):
        resp = await client.post(f"/api/history/{entry['id']}/save")
        assert resp.status_code == 200
        assert resp.json()["saved"] is True
    assert (await client.get("/api/history")).json()["saved_count"] == 1


@pytest.mark.asyncio
async def test_save_limit_reached_is_409(app, client) -> None:
    app.state.history = QueryHistory(saved_limit=1)
    first = await _seed(client, "first")
    second = await _seed(client, "second")

    assert (await client.post(f"/api/history/{first['id']}/save")).status_code == 200
    conflict = await client.post(f"/api/history/{second['id']}/save")
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "saved limit reached"
    # The refusal really is a refusal, not a silent partial write.
    assert (await client.get("/api/history")).json()["saved_count"] == 1


@pytest.mark.asyncio
async def test_delete_entry(client) -> None:
    entry = await _seed(client)
    resp = await client.delete(f"/api/history/{entry['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": entry["id"]}
    assert (await client.get("/api/history")).json()["total"] == 0


@pytest.mark.asyncio
async def test_unknown_ids_are_404(client) -> None:
    assert (await client.post("/api/history/nope/save")).status_code == 404
    assert (await client.delete("/api/history/nope/save")).status_code == 404
    assert (await client.delete("/api/history/nope")).status_code == 404


@pytest.mark.asyncio
async def test_delete_twice_is_404_the_second_time(client) -> None:
    entry = await _seed(client)
    assert (await client.delete(f"/api/history/{entry['id']}")).status_code == 200
    assert (await client.delete(f"/api/history/{entry['id']}")).status_code == 404


# ------------------------------------------------------------------
# Degraded mode: history is in-memory and must outlive a Neo4j outage
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_history_routes_work_with_neo4j_unavailable(app, client) -> None:
    entry = await _seed(client)  # recorded while the graph was still up
    app.state.neo4j_available = False

    assert (await client.get("/api/history")).status_code == 200
    assert (await client.post(f"/api/history/{entry['id']}/save")).status_code == 200
    assert (await client.delete(f"/api/history/{entry['id']}/save")).status_code == 200
    assert (await client.delete(f"/api/history/{entry['id']}")).status_code == 200
    assert (await client.get("/api/history")).json()["total"] == 0


# ------------------------------------------------------------------
# Auth: mutating routes are gated whenever API_KEY is set
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mutating_routes_require_api_key_when_configured(
    app, client, monkeypatch
) -> None:
    entry = await _seed(client)
    monkeypatch.setenv("API_KEY", "s3cret")

    assert (await client.get("/api/history")).status_code == 200  # reads stay open
    assert (await client.post(f"/api/history/{entry['id']}/save")).status_code == 401
    assert (await client.delete(f"/api/history/{entry['id']}/save")).status_code == 401
    assert (await client.delete(f"/api/history/{entry['id']}")).status_code == 401

    headers = {"X-API-Key": "s3cret"}
    ok = await client.post(f"/api/history/{entry['id']}/save", headers=headers)
    assert ok.status_code == 200
    assert ok.json()["saved"] is True
