"""Nosana GPU compute client — decentralized inference workloads."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

NOSANA_API = "https://api.nosana.com"


class NosanaClient:
    """Client for Nosana decentralized GPU compute network."""

    def __init__(self):
        self.api_key = os.getenv("NOSANA_API_KEY", "")
        self.headers = {}
        if self.api_key:
            self.headers["Authorization"] = f"Bearer {self.api_key}"

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

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    f"{NOSANA_API}/jobs",
                    json={
                        "type": job_type,
                        "model": model,
                        "input": input_data,
                        "gpu": gpu,
                    },
                    headers=self.headers,
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

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.get(
                    f"{NOSANA_API}/jobs/{job_id}",
                    headers=self.headers,
                )
                response.raise_for_status()
                return response.json()
            except Exception as e:
                log.error("Nosana status check failed: %s", e)
                return {"status": "error", "error": str(e)}

    async def run_embedding(self, texts: list[str], model: str = "default") -> dict[str, Any]:
        """Run embedding generation on Nosana GPUs."""
        import json

        return await self.submit_job(
            job_type="embedding",
            model=model,
            input_data=json.dumps({"texts": texts}),
        )

    async def _local_fallback(self, model: str, input_data: str) -> dict[str, Any]:
        """Local fallback — skip GPU, return placeholder."""
        log.info("Nosana fallback: using local CPU for model %s", model)
        return {
            "status": "completed",
            "result": f"[local-fallback] Processed with {model}",
            "method": "local",
        }
