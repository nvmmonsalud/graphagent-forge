"""The "[object Object]" guard.

FastAPI's default 422 body is `{"detail": [{"loc": [...], "msg": ...}, ...]}`.
The frontend renders `String(e.detail)`, which stringifies a list of dicts to
the literal text "[object Object]" — useless to a user. `install_exception_handlers`
must flatten `detail` to a plain string before it reaches the client.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.jobs import JobManager
from src.api.routes import install_exception_handlers, router


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(router, prefix="/api")
    install_exception_handlers(application)
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


@pytest.mark.asyncio
async def test_invalid_url_validation_error_is_a_plain_string(client) -> None:
    resp = await client.post("/api/ingest/url", json={"url": "not-a-url"})
    assert resp.status_code == 422

    body = resp.json()
    assert isinstance(body["detail"], str)
    assert "[object Object]" not in body["detail"]
    assert "body.url" in body["detail"]


@pytest.mark.asyncio
async def test_empty_question_validation_error_is_a_plain_string(client) -> None:
    resp = await client.post("/api/ask", json={"question": ""})
    assert resp.status_code == 422

    body = resp.json()
    assert isinstance(body["detail"], str)
    assert "[object Object]" not in body["detail"]
    assert "body.question" in body["detail"]


@pytest.mark.asyncio
async def test_missing_body_field_validation_error_is_a_plain_string(client) -> None:
    resp = await client.post("/api/ingest/text", json={})
    assert resp.status_code == 422

    body = resp.json()
    assert isinstance(body["detail"], str)
    assert "[object Object]" not in body["detail"]
    assert "body.text" in body["detail"]
