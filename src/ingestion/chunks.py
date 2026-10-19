"""Bounded, deterministic excerpts over normalized Unicode source text."""
from __future__ import annotations

import hashlib
import json

CHUNK_SIZE = 8000
CHUNK_OVERLAP = 400
MAX_CHUNKS = 12
MAX_DOCUMENT_CHARS = 200000


def chunk_id(source_id: str, version: str, ordinal: int, text: str) -> str:
    payload = [source_id, version, ordinal, hashlib.sha256(text.encode()).hexdigest()]
    return "ev_" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def plan_chunks(content: str, source_id: str, version: str) -> list[dict]:
    """Plan a bounded prefix with exact offsets; overlapping chars count once."""
    content = content[:MAX_DOCUMENT_CHARS]
    chunks = []
    start = 0
    while start < len(content) and len(chunks) < MAX_CHUNKS:
        end = min(start + CHUNK_SIZE, len(content))
        text = content[start:end]
        ordinal = len(chunks)
        chunks.append({
            "id": chunk_id(source_id, version, ordinal, text),
            "source_id": source_id, "version": version, "ordinal": ordinal,
            "start": start, "end": end, "text": text,
        })
        if end == len(content):
            break
        start = end - CHUNK_OVERLAP
    return chunks
