"""App-level guards in src.main: request body cap and WebSocket origin/cap."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import src.main as main_module
from src.main import MAX_REQUEST_BYTES, app


@pytest.mark.asyncio
async def test_declared_oversized_body_gets_413_before_auth_or_parsing() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        body = b"x" * (MAX_REQUEST_BYTES + 1)
        resp = await client.post(
            "/api/ingest/text", content=body, headers={"content-type": "application/json"}
        )
    assert resp.status_code == 413
    assert resp.json() == {"detail": "request body too large"}


@pytest.mark.asyncio
async def test_streamed_oversized_body_gets_413() -> None:
    async def chunks():
        for _ in range(MAX_REQUEST_BYTES // 65536 + 2):
            yield b"x" * 65536

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/api/ingest/text", content=chunks(), headers={"content-type": "application/json"}
        )
    assert resp.status_code == 413


@pytest.fixture
def ws_client():
    # No lifespan: the WebSocket route only needs the module-level ws_manager.
    return TestClient(app)


def test_websocket_rejects_foreign_origin(ws_client) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with ws_client.websocket_connect(
            "/ws/graph", headers={"origin": "https://evil.example"}
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_websocket_allows_configured_and_same_origin(ws_client) -> None:
    for origin in ("http://localhost:8000", "http://testserver"):
        with ws_client.websocket_connect("/ws/graph", headers={"origin": origin}) as ws:
            ws.send_text("ping")
            assert "pong" in ws.receive_text()


def test_websocket_refuses_when_full(ws_client, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "MAX_WS_CONNECTIONS", 0)
    with pytest.raises(WebSocketDisconnect) as exc:
        with ws_client.websocket_connect("/ws/graph") as ws:
            ws.receive_text()
    assert exc.value.code == 1013


@pytest.mark.asyncio
async def test_security_headers_on_every_http_response() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for path in ("/", "/api/health"):
            resp = await client.get(path)
            assert resp.headers["x-content-type-options"] == "nosniff"
            assert resp.headers["x-frame-options"] == "DENY"
            assert resp.headers["referrer-policy"] == "no-referrer"
            csp = resp.headers["content-security-policy"]
            assert "frame-ancestors 'none'" in csp
            assert "default-src 'self'" in csp


@pytest.mark.asyncio
async def test_api_docs_disabled_by_default() -> None:
    assert main_module.API_DOCS_ENABLED is False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404
