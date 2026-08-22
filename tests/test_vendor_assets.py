"""Tests for the vendored D3 asset (frontend/vendor/) and its serving.

D3 used to load from https://d3js.org/d3.v7.min.js, which is unreachable on
networks that block public CDNs — a live-demo failure mode for the graph,
the product's centerpiece. It is now vendored locally and served by the app
at /vendor/d3.v7.min.js. These tests guard: the vendored file is present and
plausible, its checksum matches what the README records (so the two can
never silently drift), the frontend references the local copy and no longer
any CDN host, and the FastAPI app actually serves it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_JS = REPO_ROOT / "frontend" / "vendor" / "d3.v7.min.js"
VENDOR_README = REPO_ROOT / "frontend" / "vendor" / "README.md"
FRONTEND_INDEX = REPO_ROOT / "frontend" / "index.html"

MIN_SIZE_BYTES = 100_000
BANNED_CDN_HOSTS = ("d3js.org", "cdn.jsdelivr.net", "unpkg.com", "cdnjs.cloudflare.com")


def _readme_sha256() -> str:
    """Pull the d3.v7.min.js checksum out of the vendor README.

    Parsed rather than hardcoded so the recorded value and the file on disk
    can never drift without a test failure.
    """
    text = VENDOR_README.read_text(encoding="utf-8")
    checksums_section = text.split("### Checksums", 1)
    if len(checksums_section) != 2:
        pytest.fail(f"Could not find a '### Checksums' section in {VENDOR_README}")
    match = re.search(
        r"`d3\.v7\.min\.js`.*?:\s*\n\s*`([0-9a-f]{64})`",
        checksums_section[1],
        flags=re.DOTALL,
    )
    if not match:
        pytest.fail(
            "Could not find a sha256 for d3.v7.min.js in "
            f"{VENDOR_README} — expected a `d3.v7.min.js ...: `<hex>`` line"
        )
    return match.group(1)


def test_vendored_d3_exists_and_is_large_enough() -> None:
    assert VENDOR_JS.exists(), f"missing vendored asset: {VENDOR_JS}"
    size = VENDOR_JS.stat().st_size
    assert size > MIN_SIZE_BYTES, (
        f"{VENDOR_JS} is only {size} bytes — expected > {MIN_SIZE_BYTES}, "
        "this looks truncated or wrong"
    )


def test_vendored_d3_looks_like_real_d3() -> None:
    text = VENDOR_JS.read_text(encoding="utf-8", errors="replace")
    assert "forceSimulation" in text


def test_vendored_d3_checksum_matches_readme() -> None:
    expected = _readme_sha256()
    actual = hashlib.sha256(VENDOR_JS.read_bytes()).hexdigest()
    assert actual == expected, (
        f"frontend/vendor/d3.v7.min.js sha256 ({actual}) does not match the "
        f"value recorded in frontend/vendor/README.md ({expected}) — "
        "re-vendor and update the README, or the file was modified in place"
    )


def test_index_html_references_local_vendor_copy() -> None:
    html = FRONTEND_INDEX.read_text(encoding="utf-8")
    assert 'src="/vendor/d3.v7.min.js"' in html


def test_index_html_has_no_cdn_references() -> None:
    html = FRONTEND_INDEX.read_text(encoding="utf-8")
    for host in BANNED_CDN_HOSTS:
        assert host not in html, f"found banned CDN host reference: {host}"


@pytest.mark.asyncio
async def test_app_serves_vendored_d3() -> None:
    # A static mount needs no app lifespan (no Neo4j/agent startup), so this
    # deliberately does not use LifespanManager/startup — just the ASGI app.
    import src.main as main_module

    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/vendor/d3.v7.min.js")

    assert resp.status_code == 200
    content_type = resp.headers.get("content-type", "")
    assert "javascript" in content_type.lower()
