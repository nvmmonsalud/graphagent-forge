"""Stable source identity and extraction-version fingerprints.

Titles are display metadata. Named text and upload sources refresh by their
explicit name; URL sources refresh by a conservatively canonicalized URL.
"""
from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlsplit, urlunsplit

from src.ingestion.chunks import CHUNK_OVERLAP, CHUNK_SIZE, MAX_CHUNKS, MAX_DOCUMENT_CHARS
from src.ingestion.entity_parser import (
    DEFAULT_KIMI_BASE_URL,
    EXTRACT_MAX_TOKENS,
    _resolve_model,
)

EXTRACTION_SCHEMA_VERSION = "graph-extraction-v2-evidence"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def canonical_url(url: str) -> str:
    """Normalize only scheme, host, default port, and fragment.

    Extraction performs DNS/SSRF validation before this helper is called. Never
    preserve userinfo in durable metadata; extractor rejects credentialed URLs.
    Query bytes/order and paths remain meaningful parts of identity.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host:
        raise ValueError("Source URL must use HTTP(S) with a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URL must not contain credentials")
    port = parsed.port
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    return urlunsplit((scheme, host, parsed.path, parsed.query, ""))


def build_source(
    kind: str, identity: str, title: str, content: str, model: str | None = None,
) -> dict:
    """Build the source contract from cleaned extraction text and configuration."""
    if kind not in {"url", "text", "file"}:
        raise ValueError("Unsupported source kind")
    original_identity = identity
    identity = canonical_url(identity) if kind == "url" else identity
    extraction_version = _digest({
        "model": _resolve_model(model),
        "schema": EXTRACTION_SCHEMA_VERSION,
        "chunk_policy": [CHUNK_SIZE, CHUNK_OVERLAP, MAX_CHUNKS, MAX_DOCUMENT_CHARS],
        "max_tokens": EXTRACT_MAX_TOKENS,
        # Store only a digest: a custom endpoint can contain credentials.
        "endpoint": _digest(os.getenv("KIMI_BASE_URL", DEFAULT_KIMI_BASE_URL)),
    })
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    # Queries remain in the hashed identity, but signed/access-token query
    # values must never become publicly returned durable source metadata.
    uri = ""
    if kind == "url":
        parsed = urlsplit(identity)
        uri = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        if not title or title in {identity, original_identity}:
            title = uri
    return {
        "id": "src_" + _digest([kind, identity]),
        "title": title or identity,
        "kind": kind,
        "uri": uri,
        "content_hash": content_hash,
        "version": _digest([content_hash, extraction_version]),
        "extraction_version": extraction_version,
    }
