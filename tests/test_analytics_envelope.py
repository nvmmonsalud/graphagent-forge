"""Contract test for DaytonaExecutor.analyze_graph's two-shape envelope.

Success:
    {"ok": True, "components", "top_pagerank", "top_betweenness",
     "avg_clustering", "betweenness_reason", "node_count", "edge_count",
     "method", "duration_ms", ["boot_ms", "sandbox_id"]}

Failure:
    {"ok": False, "error": <literal>, "method", "duration_ms"}

The failure shape has NO metric keys — their absence is the machine-readable
"the metrics could not be computed" signal, so a failed run can never be read
as a real structural verdict (an empty `components` would look like an empty
graph). `error` is always one of exactly two literals, and those two are
disjoint from verify_graph's pair: a failed analytics run must never be
mistakable for a failed integrity verification.
"""
from __future__ import annotations

import json

import pytest

from src.agent.daytona_exec import (
    ANALYTICS_ERROR_TIMEOUT,
    ANALYTICS_ERROR_UNAVAILABLE,
    VERIFY_ERROR_TIMEOUT,
    VERIFY_ERROR_UNAVAILABLE,
    DaytonaExecutor,
)

ANALYTICS_ERROR_LITERALS = {ANALYTICS_ERROR_UNAVAILABLE, ANALYTICS_ERROR_TIMEOUT}
VERIFY_ERROR_LITERALS = {VERIFY_ERROR_UNAVAILABLE, VERIFY_ERROR_TIMEOUT}

METRIC_KEYS = (
    "components",
    "top_pagerank",
    "top_betweenness",
    "avg_clustering",
    "node_count",
    "edge_count",
)

GRAPH = {
    "nodes": [
        {"id": "a", "label": "Alice", "type": "Person"},
        {"id": "b", "label": "Acme", "type": "Org"},
        {"id": "c", "label": "Bob", "type": "Person"},
    ],
    "edges": [
        {"source": "a", "target": "b", "type": "WORKS_AT"},
        {"source": "c", "target": "b", "type": "WORKS_AT"},
    ],
}


def test_analytics_literals_are_disjoint_from_verify_literals() -> None:
    assert ANALYTICS_ERROR_LITERALS.isdisjoint(VERIFY_ERROR_LITERALS)
    assert len(ANALYTICS_ERROR_LITERALS) == 2


@pytest.mark.asyncio
async def test_analyze_graph_success_envelope() -> None:
    executor = DaytonaExecutor()
    assert executor.client is None  # no DAYTONA_API_KEY in the test env

    result = await executor.analyze_graph(GRAPH)

    assert result["ok"] is True
    assert result["method"] == "local"
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0
    for key in METRIC_KEYS:
        assert key in result, key
    assert result["components"]["count"] == 1
    assert result["components"]["largest"] == 3
    assert result["components"]["sizes"] == [3]
    assert result["betweenness_reason"] is None
    assert isinstance(result["avg_clustering"], float)


@pytest.mark.asyncio
async def test_analyze_graph_failure_envelope_has_no_metric_keys(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _boom(*args, **kwargs):
        return {"success": False, "error": "boom /secret/bolt/uri"}

    monkeypatch.setattr(executor, "_local_fallback", _boom)

    result = await executor.analyze_graph(GRAPH)

    assert result["ok"] is False
    assert result["error"] == ANALYTICS_ERROR_UNAVAILABLE
    assert result["method"] == "local"
    assert isinstance(result["duration_ms"], int)
    for key in METRIC_KEYS:
        assert key not in result, key


@pytest.mark.asyncio
async def test_analyze_graph_failure_never_leaks_raw_exception_text(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _raise(*args, **kwargs):
        raise RuntimeError("bolt://user:pw@host")

    monkeypatch.setattr(executor, "_local_fallback", _raise)

    result = await executor.analyze_graph(GRAPH)

    assert result["ok"] is False
    assert result["error"] in ANALYTICS_ERROR_LITERALS
    assert "components" not in result

    dumped = json.dumps(result)
    assert "bolt" not in dumped
    assert "pw@host" not in dumped
    assert "RuntimeError" not in dumped


@pytest.mark.asyncio
async def test_analyze_graph_timeout_maps_to_timeout_literal(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _timeout(*args, **kwargs):
        raise TimeoutError("took far too long")

    monkeypatch.setattr(executor, "_local_fallback", _timeout)

    result = await executor.analyze_graph(GRAPH)

    assert result["ok"] is False
    assert result["error"] == ANALYTICS_ERROR_TIMEOUT
    assert "components" not in result


@pytest.mark.asyncio
async def test_analyze_graph_timeout_wording_maps_to_timeout_literal(monkeypatch) -> None:
    """The local runner reports its 30s cap as a string, not an exception."""
    executor = DaytonaExecutor()

    async def _timed_out(*args, **kwargs):
        return {"success": False, "error": "Execution timed out (30s)"}

    monkeypatch.setattr(executor, "_local_fallback", _timed_out)

    result = await executor.analyze_graph(GRAPH)

    assert result["ok"] is False
    assert result["error"] == ANALYTICS_ERROR_TIMEOUT
    assert "components" not in result
