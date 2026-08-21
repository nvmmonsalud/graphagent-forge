"""Tests for POST /api/ingest/file — multipart upload route, agent/neo4j mocked out.

Fixtures (app/client/rate-limiter reset, `_shutdown_jobs`) are duplicated
from tests/test_routes.py rather than imported, matching this repo's
per-module fixture-duplication convention (see that file's docstring/queue-
full test for the pattern this mirrors).

`POST /api/ingest/file` only registers when python-multipart is importable
(`src.api.routes.HAS_MULTIPART`); requirements.txt already pins it so this
should always be true here, but a route that 404s instead of 202ing on every
test in this module is the tell if that ever regresses.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.jobs import JobManager
from src.api import routes as routes_module
from src.api.routes import router

pytestmark = pytest.mark.skipif(
    not routes_module.HAS_MULTIPART,
    reason="python-multipart not installed — /api/ingest/file is not registered",
)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter's bucket dict is module-level global state."""
    routes_module._rate_buckets.clear()
    yield
    routes_module._rate_buckets.clear()


async def _shutdown_jobs(jobs: JobManager) -> None:
    """Bounded `jobs.shutdown()` for fixture/test teardown — see test_routes.py."""
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
# 202 submit + ?wait=true
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_202_shape(app, client) -> None:
    app.state.agent.ingest_file.return_value = {"success": True, "nodes": 1}
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"source": "my-doc"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body.keys() == {"job_id", "status"}
    assert body["status"] == "queued"
    assert body["job_id"]


@pytest.mark.asyncio
async def test_ingest_file_wait_true_returns_200_with_agent_result(app, client) -> None:
    app.state.agent.ingest_file.return_value = {"success": True, "nodes": 2, "edges": 1}
    resp = await client.post(
        "/api/ingest/file",
        params={"wait": "true"},
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "nodes": 2, "edges": 1}
    app.state.agent.ingest_file.assert_awaited()


# ------------------------------------------------------------------
# Validation errors
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_413_when_oversize(app, client) -> None:
    oversize = b"x" * (3 * 1024 * 1024 + 1)
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("big.txt", oversize, "text/plain")},
    )
    assert resp.status_code == 413
    assert "file too large" in resp.json()["detail"]
    app.state.agent.ingest_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_415_for_unsupported_type(app, client) -> None:
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 415
    app.state.agent.ingest_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_422_for_path_traversal_filename(app, client) -> None:
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("../../etc/passwd", b"hi", "text/plain")},
    )
    assert resp.status_code == 422
    app.state.agent.ingest_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_file_422_for_empty_file(app, client) -> None:
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert resp.status_code == 422
    app.state.agent.ingest_file.assert_not_awaited()


# ------------------------------------------------------------------
# Auth / availability
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_401_when_api_key_configured_and_missing(monkeypatch, client) -> None:
    monkeypatch.setenv("API_KEY", "super-secret")
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_ingest_file_503_when_neo4j_unavailable(app, client) -> None:
    app.state.neo4j_available = False
    resp = await client.post(
        "/api/ingest/file",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 503


# ------------------------------------------------------------------
# Queue-full (own app/manager, mirrors test_routes.py::test_ingest_429_when_job_queue_full)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_429_when_job_queue_full() -> None:
    application = FastAPI()
    application.include_router(router, prefix="/api")
    application.state.agent = AsyncMock()
    application.state.neo4j = AsyncMock()
    application.state.neo4j_available = True
    application.state.jobs = JobManager(broadcast=None, job_timeout=5, max_active=1)

    gate = asyncio.Event()

    async def parked_ingest_file(*args, **kwargs):
        await gate.wait()
        return {"success": True}

    application.state.agent.ingest_file.side_effect = parked_ingest_file
    application.state.jobs.start()

    transport = httpx.ASGITransport(app=application)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            first = await c.post(
                "/api/ingest/file",
                files={"file": ("one.txt", b"one", "text/plain")},
            )
            assert first.status_code == 202

            second = await c.post(
                "/api/ingest/file",
                files={"file": ("two.txt", b"two", "text/plain")},
            )
            assert second.status_code == 429
            assert second.json()["detail"] == "job queue full"

            gate.set()
    finally:
        await _shutdown_jobs(application.state.jobs)


# ------------------------------------------------------------------
# Params-leak: job params carry only filename/content_type/size_bytes,
# never the uploaded content.
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_job_params_never_leak_content(app, client) -> None:
    app.state.agent.ingest_file.return_value = {"success": True}
    secret_marker = "TOTALLY-SECRET-UPLOAD-BODY-MARKER"
    submit_resp = await client.post(
        "/api/ingest/file",
        files={"file": ("notes.txt", secret_marker.encode(), "text/plain")},
    )
    assert submit_resp.status_code == 202
    assert secret_marker not in submit_resp.text

    jobs_resp = await client.get("/api/jobs")
    assert jobs_resp.status_code == 200
    assert secret_marker not in jobs_resp.text

    jobs = jobs_resp.json()["jobs"]
    file_jobs = [j for j in jobs if j["kind"] == "file"]
    assert len(file_jobs) == 1
    assert set(file_jobs[0]["params"].keys()) == {"filename", "content_type", "size_bytes"}
    assert file_jobs[0]["params"]["filename"] == "notes.txt"
    assert file_jobs[0]["params"]["size_bytes"] == len(secret_marker.encode())
