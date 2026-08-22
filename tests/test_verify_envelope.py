"""Contract test for DaytonaExecutor.verify_graph's two-shape envelope.

Success:
    {"ok": True, "valid": bool, "node_count", "edge_count", "orphan_edges",
     "orphan_count", "nodes_with_summary", "quality_score", "method",
     "duration_ms", ["boot_ms", "sandbox_id"]}

Failure:
    {"ok": False, "error": <literal>, "method", "duration_ms"}

The failure shape has NO "valid" key — its absence is the machine-readable
"the check could not run" signal, distinct from a real integrity verdict.
`error` is always one of exactly two literals; raw exception text must never
reach the client (it can carry bolt URIs / credentials).
"""
from __future__ import annotations

import json

import pytest

from src.agent.daytona_exec import (
    VERIFY_ERROR_TIMEOUT,
    VERIFY_ERROR_UNAVAILABLE,
    DaytonaExecutor,
)

VALID_ERROR_LITERALS = {VERIFY_ERROR_UNAVAILABLE, VERIFY_ERROR_TIMEOUT}


@pytest.mark.asyncio
async def test_verify_graph_success_envelope() -> None:
    executor = DaytonaExecutor()
    assert executor.client is None  # no DAYTONA_API_KEY in the test env

    graph = {
        "nodes": [
            {"id": "a", "label": "Alice", "properties": {"summary": "a person"}},
            {"id": "b", "label": "Acme", "properties": {"summary": "a company"}},
        ],
        "edges": [{"source": "a", "target": "b", "relationship": "WORKS_AT"}],
    }

    result = await executor.verify_graph(graph)

    assert result["ok"] is True
    assert "valid" in result
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0
    assert result["method"] == "local"


@pytest.mark.asyncio
async def test_verify_graph_failure_envelope_has_no_valid_key_and_scrubs_error(
    monkeypatch,
) -> None:
    executor = DaytonaExecutor()
    assert executor.client is None

    async def _boom(*args, **kwargs):
        return {"success": False, "error": "boom /secret/bolt/uri"}

    monkeypatch.setattr(executor, "_local_fallback", _boom)

    result = await executor.verify_graph({"nodes": [], "edges": []})

    assert result["ok"] is False
    assert "valid" not in result
    assert result["error"] in VALID_ERROR_LITERALS
    assert result["error"] == VERIFY_ERROR_UNAVAILABLE  # no "timed out" in the raw error
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0

    dumped = json.dumps(result)
    assert "secret" not in dumped
    assert "boom" not in dumped


@pytest.mark.asyncio
async def test_verify_graph_failure_maps_timeout_wording_to_timeout_literal(
    monkeypatch,
) -> None:
    executor = DaytonaExecutor()

    async def _timed_out(*args, **kwargs):
        return {"success": False, "error": "Execution timed out (30s)"}

    monkeypatch.setattr(executor, "_local_fallback", _timed_out)

    result = await executor.verify_graph({"nodes": [], "edges": []})

    assert result["ok"] is False
    assert "valid" not in result
    assert result["error"] == VERIFY_ERROR_TIMEOUT
