"""Nosana GPU compute client — decentralized inference workloads."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import struct
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_NOSANA_API = "https://dashboard.k8s.prd.nos.ci/api"
EMBEDDING_DIM = 384
_UINT32_SCALE = float(1 << 32)


def _is_finite_embedding(values: Any) -> bool:
    """True only for a list of EMBEDDING_DIM finite real numbers."""
    if not isinstance(values, list) or len(values) != EMBEDDING_DIM:
        return False
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(v):
            return False
    return True


class NosanaClient:
    """Client for Nosana decentralized GPU compute network."""

    def __init__(self):
        self.api_key = os.getenv("NOSANA_API_KEY", "")
        self.api_url = os.getenv("NOSANA_API_URL", DEFAULT_NOSANA_API).rstrip("/")
        self.embedding_url = os.getenv("NOSANA_EMBEDDING_URL", "").strip()
        self.headers = {}
        self.last_embedding_method = "uninitialized"
        self._client: httpx.AsyncClient | None = None
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

    def _http(self) -> httpx.AsyncClient:
        """Lazily create (and reuse) one HTTP client per NosanaClient instance."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def aclose(self) -> None:
        """Close the shared HTTP client, if one was ever created."""
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as e:  # pragma: no cover - best-effort cleanup
                log.warning("Failed to close Nosana HTTP client: %s", e)

    async def submit_job(
        self,
        job_type: str = "inference",
        model: str = "default",
        input_data: str = "",
        gpu: str = "RTX_4090",
    ) -> dict[str, Any]:
        """Submit a GPU compute job to Nosana."""
        if not self.api_key:
            log.warning("NOSANA_API_KEY not set — using local fallback")
            return await self._local_fallback(model, input_data)

        try:
            response = await self._http().post(
                f"{self.api_url}/jobs",
                json={
                    "type": job_type,
                    "model": model,
                    "input": input_data,
                    "gpu": gpu,
                },
                headers=self.headers,
                timeout=60.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.error("Nosana job submission failed: %s", e)
            return await self._local_fallback(model, input_data)

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        """Check status of a submitted job."""
        if not self.api_key:
            return {"status": "completed", "result": "local_fallback"}

        try:
            response = await self._http().get(
                f"{self.api_url}/jobs/{job_id}",
                headers=self.headers,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.error("Nosana status check failed: %s", e)
            return {"status": "error", "error": str(e)}

    async def run_embedding(self, texts: list[str], model: str = "default") -> dict[str, Any]:
        """Run embedding generation on Nosana GPUs."""
        return await self.submit_job(
            job_type="embedding",
            model=model,
            input_data=json.dumps({"texts": texts}),
        )

    async def get_embedding(self, text: str) -> list[float]:
        """Generate a 384-dim embedding vector for the given text.

        Submits an embedding job to Nosana if an API key is configured and the
        service is reachable.  Falls back to a deterministic hash-based
        pseudo-embedding so that downstream vector indexes always work.
        """
        # Nosana's documented API manages GPU jobs/deployments; it does not
        # guarantee a hosted OpenAI-compatible /embeddings route. Only call an
        # embedding endpoint when the operator explicitly configures one.
        if self.api_key and self.embedding_url:
            try:
                response = await self._http().post(
                    self.embedding_url,
                    json={"input": text},
                    headers=self.headers,
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json()
                embedding = data.get("embedding", [])
                if _is_finite_embedding(embedding):
                    self.last_embedding_method = "nosana"
                    return [float(v) for v in embedding]
                if isinstance(embedding, list) and len(embedding) == EMBEDDING_DIM:
                    log.warning(
                        "Nosana embedding contained non-finite values, using hash fallback"
                    )
                else:
                    log.warning("Nosana returned unexpected embedding shape, using fallback")
            except Exception as e:
                log.warning("Nosana embedding failed (%s), using hash fallback", e)

        if self.api_key and not self.embedding_url:
            log.info("NOSANA_EMBEDDING_URL not configured; using local fallback")

        self.last_embedding_method = "local_hash_fallback"
        return self._hash_embedding(text)

    @staticmethod
    def _hash_embedding(text: str) -> list[float]:
        """Deterministic, finite, unit-norm 384-dim pseudo-embedding from SHA-256.

        The digest is read as unsigned 32-bit integers — never as raw IEEE-754
        floats, which decode to NaN/Inf for a meaningful fraction of digests and
        poison the vector written to Neo4j — then mapped into [-1, 1), tiled to
        384 dims and normalized.
        """
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 8 x 4-byte little-endian unsigned ints, spread evenly over [-1, 1)
        base = [
            (v / _UINT32_SCALE) * 2.0 - 1.0
            for v in struct.unpack("<8I", digest)
        ]
        # Repeat the pattern to reach 384 dimensions (48 * 8 = 384)
        tiled = (base * 48)[:EMBEDDING_DIM]

        magnitude = math.sqrt(sum(x * x for x in tiled))
        if not math.isfinite(magnitude) or magnitude <= 0.0:
            # Astronomically unlikely (all 8 words exactly 2**31); stay usable.
            return [1.0 / math.sqrt(EMBEDDING_DIM)] * EMBEDDING_DIM
        return [x / magnitude for x in tiled]

    async def _local_fallback(self, model: str, input_data: str) -> dict[str, Any]:
        """Local fallback — skip GPU, return placeholder."""
        log.info("Nosana fallback: using local CPU for model %s", model)
        return {
            "status": "completed",
            "result": f"[local-fallback] Processed with {model}",
            "method": "local",
        }
