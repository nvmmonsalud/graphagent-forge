"""Label normalization helpers for entity deduplication.

Pure stdlib — no Neo4j / project imports, so this stays cheap to use from
any layer (write path, migration backfill, tests).
"""
from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def normalize_label(label: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    ``normalize_label('  Kimi, AI!  ') == 'kimi ai'``. ``None`` / empty input
    yields ``''`` so callers can treat "no normal form" uniformly.
    """
    cleaned = _PUNCT_RE.sub("", (label or "").lower())
    return " ".join(cleaned.split())
