"""Tests for src.ingestion.extractor — SSRF guards and body handling."""
from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx
import pytest

import src.ingestion.extractor as extractor
from src.ingestion.extractor import (
    MAX_BODY_BYTES,
    check_url_shape,
    guard_url,
    is_blocked_ip,
)


# ------------------------------------------------------------------
# check_url_shape
# ------------------------------------------------------------------
def test_check_url_shape_rejects_ftp_scheme() -> None:
    assert check_url_shape("ftp://example.com/file") is not None


def test_check_url_shape_rejects_file_scheme() -> None:
    assert check_url_shape("file:///etc/passwd") is not None


def test_check_url_shape_rejects_userinfo() -> None:
    err = check_url_shape("http://user:pass@example.com/")
    assert err is not None
    assert "userinfo" in err


def test_check_url_shape_rejects_empty_host() -> None:
    assert check_url_shape("http:///path") is not None


def test_check_url_shape_accepts_normal_https_url() -> None:
    assert check_url_shape("https://example.com/page") is None


# ------------------------------------------------------------------
# is_blocked_ip
# ------------------------------------------------------------------
@pytest.mark.parametrize(
    "addr",
    [
        "10.1.2.3",
        "127.0.0.1",
        "169.254.1.1",
        "::1",
        "fd00::1",
        "100.64.0.1",
        "::ffff:127.0.0.1",
    ],
)
def test_is_blocked_ip_blocks_private_ranges(addr: str) -> None:
    assert is_blocked_ip(ipaddress.ip_address(addr)) is True


def test_is_blocked_ip_allows_public_address() -> None:
    assert is_blocked_ip(ipaddress.ip_address("8.8.8.8")) is False


# ------------------------------------------------------------------
# guard_url — DNS resolution branch
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_guard_url_blocks_when_any_dns_record_is_private(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    error = await guard_url("http://multi-record.example.test/")
    assert error is not None
    assert "private/reserved" in error


@pytest.mark.asyncio
async def test_guard_url_allows_all_public_dns_records(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 0)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    error = await guard_url("http://all-public.example.test/")
    assert error is None


@pytest.mark.asyncio
async def test_guard_url_literal_private_ip_blocked() -> None:
    error = await guard_url("http://127.0.0.1/")
    assert error is not None


# ------------------------------------------------------------------
# extract_from_url — redirect re-guard + body cap, via MockTransport
# ------------------------------------------------------------------
@pytest.fixture
async def mock_client(monkeypatch):
    """Swap the module's shared client for one wired to a MockTransport."""
    created: dict[str, httpx.AsyncClient] = {}

    def _install(handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        created["client"] = client
        monkeypatch.setattr(extractor, "_client", client)
        return client

    yield _install

    client = created.get("client")
    if client is not None:
        await client.aclose()
    monkeypatch.setattr(extractor, "_client", None)


@pytest.mark.asyncio
async def test_redirect_to_private_ip_is_blocked(mock_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})
        raise AssertionError("should never reach the redirect target")

    mock_client(handler)

    result = await extractor.extract_from_url("http://8.8.8.8/start")
    assert result["error"] is not None
    assert "private/reserved" in result["error"] or "Blocked" in result["error"]


@pytest.mark.asyncio
async def test_redirect_to_public_ip_succeeds(mock_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "8.8.8.8":
            return httpx.Response(302, headers={"location": "http://1.1.1.1/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><head><title>T</title></head><body>hello</body></html>",
        )

    mock_client(handler)

    result = await extractor.extract_from_url("http://8.8.8.8/start")
    assert result["error"] is None
    assert result["final_url"] == "http://1.1.1.1/final"
    assert "hello" in result["content"]


@pytest.mark.asyncio
async def test_body_over_cap_is_truncated(mock_client) -> None:
    big_body = b"x" * (MAX_BODY_BYTES + 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=big_body,
        )

    mock_client(handler)

    result = await extractor.extract_from_url("http://8.8.8.8/big")
    assert result["error"] is None
    assert result["body_truncated"] is True


@pytest.mark.asyncio
async def test_unsupported_content_type_rejected(mock_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=b"%PDF-1.4",
        )

    mock_client(handler)

    result = await extractor.extract_from_url("http://8.8.8.8/doc.pdf")
    assert result["error"] is not None
    assert "content type" in result["error"]


@pytest.mark.asyncio
async def test_too_many_redirects_blocked(mock_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Bounce between two public IPs forever.
        next_host = "1.1.1.1" if request.url.host == "8.8.8.8" else "8.8.8.8"
        return httpx.Response(302, headers={"location": f"http://{next_host}/"})

    mock_client(handler)

    result = await extractor.extract_from_url("http://8.8.8.8/loop")
    assert result["error"] is not None
    assert "redirects" in result["error"]
