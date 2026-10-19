"""Identity of the configured semantic embedding space (never an endpoint URL)."""
from __future__ import annotations

import hashlib
import os

EMBEDDING_DIM = 384


def configured_embedding_space() -> dict:
    """Return the operator's model/version and the fixed vector dimension.

    A digest identifies unnamed endpoints without exposing credentials in their
    URL. Changing endpoint, model or version requires re-embedding source text.
    """
    model = os.getenv("NOSANA_EMBEDDING_MODEL", "").strip()
    if not model:
        endpoint = os.getenv("NOSANA_EMBEDDING_URL", "").strip()
        model = "endpoint-sha256:" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
    return {
        "model": model,
        "version": os.getenv("NOSANA_EMBEDDING_VERSION", "1").strip() or "1",
        "dimension": EMBEDDING_DIM,
    }
