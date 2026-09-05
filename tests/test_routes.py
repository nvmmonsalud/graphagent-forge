"""Tests for src.api.routes — the HTTP surface, with agent/neo4j mocked out."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.jobs import JobManager
from src.api import routes as routes_module
from src.api.routes import router


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    """The rate limiter's bucket dict is module-level global state.

    RATE_LIMIT_MAX is read from the env once at import time, so a developer
    shell (or demo .env) with RATE_LIMIT_MAX raised would silently change
    what the 10-request tests below observe — pin it to the documented default.
    """
    monkeypatch.setattr(routes_module, "RATE_LIMIT_MAX", 10)
    routes_module._rate_buckets.clear()
    yield
    routes_module._rate_buckets.clear()


async def _shutdown_jobs(jobs: JobManager) -> None:
    """Bounded `jobs.shutdown()` for fixture/test teardown.

    `JobManager.shutdown()` is documented to be safe to await unconditionally,
    but as of this writing it can hang indefinitely if it races a worker that
    is actively mid-`_run_job` (not yet back at the idle `queue.get()`) rather
    than parked on a real await point — reproducible offline with a trivial
    `run` callable and no HTTP layer at all. Bound it here so a regression
    fails one test loudly instead of hanging the whole suite.
    """
    try:
        await asyncio.wait_for(jobs.shutdown(), timeout=5)
    except TimeoutError:
        pytest.fail(
            "JobManager.shutdown() did not return within 5s — likely the "
            "known mid-run-cancellation hang in src/agent/jobs.py"
        )


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
    await _shutdown_jobs(app.state.jobs)


# ------------------------------------------------------------------
# Validation
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_url_invalid_url_is_422(client) -> None:
    resp = await client.post("/api/ingest/url", json={"url": "not-a-url"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ask_empty_question_is_422(client) -> None:
    resp = await client.post("/api/ask", json={"question": ""})
    assert resp.status_code == 422


# ------------------------------------------------------------------
# 503 when graph unavailable
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_graph_stats_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.get("/api/graph/stats")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_sources_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.get("/api/sources")
    assert resp.status_code == 503


# ------------------------------------------------------------------
# 500 -> generic detail, real error only logged
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_internal_error_detail_is_generic(app, client) -> None:
    app.state.agent.get_graph_stats.side_effect = RuntimeError("bolt://secret-uri leaked")
    resp = await client.get("/api/graph/stats")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"
    assert "bolt://" not in resp.text


# ------------------------------------------------------------------
# API key auth (only enforced when API_KEY is set)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_api_key_required_by_default(app, client) -> None:
    app.state.agent.ingest_text.return_value = {"success": True}
    resp = await client.post(
        "/api/ingest/text", json={"text": "hello world", "source": "manual"}
    )
    assert resp.status_code != 401
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "queued"


@pytest.mark.asyncio
async def test_401_when_api_key_configured_and_missing(monkeypatch, client) -> None:
    monkeypatch.setenv("API_KEY", "super-secret")
    resp = await client.post(
        "/api/ingest/text", json={"text": "hello world", "source": "manual"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_202_when_api_key_configured_and_correct(monkeypatch, app, client) -> None:
    monkeypatch.setenv("API_KEY", "super-secret")
    app.state.agent.ingest_text.return_value = {"success": True}
    resp = await client.post(
        "/api/ingest/text",
        json={"text": "hello world", "source": "manual"},
        headers={"X-API-Key": "super-secret"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "queued"


# ------------------------------------------------------------------
# Rate limiting: 10/min on /ask
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limit_kicks_in_after_ten_requests(app, client) -> None:
    app.state.agent.ask.return_value = {"answer": "ok", "sources": []}

    statuses = []
    for _ in range(11):
        resp = await client.post("/api/ask", json={"question": "What is GraphAgent Forge?"})
        statuses.append(resp.status_code)

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


# ------------------------------------------------------------------
# /api/sources
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sources_shape(app, client) -> None:
    app.state.neo4j.get_sources.return_value = [
        {"source_doc": "https://x.example/a", "node_count": 3, "updated_at": "2026-01-01"}
    ]
    resp = await client.get("/api/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert "sources" in body
    assert body["sources"][0]["source_doc"] == "https://x.example/a"


@pytest.mark.asyncio
async def test_delete_source_404_when_nothing_deleted(app, client) -> None:
    app.state.neo4j.delete_source.return_value = {"deleted_nodes": 0}
    resp = await client.request(
        "DELETE", "/api/sources", params={"source_doc": "nope"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_source_200_when_deleted(app, client) -> None:
    app.state.neo4j.delete_source.return_value = {"deleted_nodes": 5}
    resp = await client.request(
        "DELETE", "/api/sources", params={"source_doc": "some-doc"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"deleted_nodes": 5}


# ------------------------------------------------------------------
# Export
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_export_json_has_attachment_disposition(app, client) -> None:
    app.state.neo4j.get_all_graph_data.return_value = {"nodes": [], "edges": []}
    resp = await client.get("/api/graph/export", params={"format": "json"})
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == "attachment; filename=graph-export.json"


@pytest.mark.asyncio
async def test_export_csv_header_row(app, client) -> None:
    app.state.neo4j.get_all_graph_data.return_value = {
        "nodes": [
            {"id": "a", "label": "Alice"},
            {"id": "b", "label": "Acme"},
        ],
        "edges": [
            {"source": "a", "target": "b", "type": "WORKS_AT", "context": "since 2020"},
        ],
    }
    resp = await client.get("/api/graph/export", params={"format": "csv"})
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == "attachment; filename=graph-export.csv"
    lines = resp.text.strip().splitlines()
    assert lines[0] == "source_id,source_label,type,target_id,target_label,context"
    assert lines[1] == "a,Alice,WORKS_AT,b,Acme,since 2020"


@pytest.mark.asyncio
async def test_export_invalid_format_is_422(client) -> None:
    resp = await client.get("/api/graph/export", params={"format": "xml"})
    assert resp.status_code == 422


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_always_200(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["neo4j"] is False


# ------------------------------------------------------------------
# Job queue: ingest submission + wait=true + /api/jobs reads
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_wait_true_returns_200_with_agent_result(app, client) -> None:
    app.state.agent.ingest_text.return_value = {"success": True, "nodes": 1}
    resp = await client.post(
        "/api/ingest/text",
        params={"wait": "true"},
        json={"text": "hello world", "source": "manual"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "nodes": 1}
    app.state.agent.ingest_text.assert_awaited()


@pytest.mark.asyncio
async def test_ingest_wait_true_failure_is_500_internal_error(app, client) -> None:
    app.state.agent.ingest_text.side_effect = RuntimeError("boom")
    resp = await client.post(
        "/api/ingest/text",
        params={"wait": "true"},
        json={"text": "hello world", "source": "manual"},
    )
    assert resp.status_code == 500
    assert resp.json()["detail"] == "internal error"
    assert "boom" not in resp.text


@pytest.mark.asyncio
async def test_get_job_404_when_unknown(client) -> None:
    resp = await client.get("/api/jobs/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "job not found"


@pytest.mark.asyncio
async def test_list_jobs_shape_newest_first(app, client) -> None:
    app.state.agent.ingest_text.return_value = {"success": True}
    first = await client.post("/api/ingest/text", json={"text": "one", "source": "manual"})
    second = await client.post("/api/ingest/text", json={"text": "two", "source": "manual"})
    assert first.status_code == 202
    assert second.status_code == 202

    resp = await client.get("/api/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert "jobs" in body
    ids = [j["id"] for j in body["jobs"]]
    assert ids.index(second.json()["job_id"]) < ids.index(first.json()["job_id"])


@pytest.mark.asyncio
async def test_list_jobs_200_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.get("/api/jobs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_list_jobs_200_after_rate_limit_exhausted(app, client) -> None:
    app.state.agent.ask.return_value = {"answer": "ok", "sources": []}
    for _ in range(10):
        resp = await client.post("/api/ask", json={"question": "hi"})
        assert resp.status_code == 200
    exhausted = await client.post("/api/ask", json={"question": "hi"})
    assert exhausted.status_code == 429

    resp = await client.get("/api/jobs")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_429_when_job_queue_full() -> None:
    # Own app/manager (max_active=1) rather than the shared `app`/`client`
    # fixtures — the shared JobManager's default max_active=20 would make a
    # queue-full 429 hard to trigger deterministically.
    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.state.agent = AsyncMock()
    application.state.neo4j = AsyncMock()
    application.state.neo4j_available = True
    application.state.jobs = JobManager(broadcast=None, job_timeout=5, max_active=1)

    gate = asyncio.Event()

    async def parked_ingest_text(*args, **kwargs):
        await gate.wait()
        return {"success": True}

    application.state.agent.ingest_text.side_effect = parked_ingest_text
    application.state.jobs.start()

    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post("/api/ingest/text", json={"text": "one", "source": "manual"})
            assert first.status_code == 202

            second = await c.post("/api/ingest/text", json={"text": "two", "source": "manual"})
            assert second.status_code == 429
            assert second.json()["detail"] == "job queue full"

            gate.set()
    finally:
        await _shutdown_jobs(application.state.jobs)
