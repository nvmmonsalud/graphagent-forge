"""Tests for the local (non-Daytona) fallback of DaytonaExecutor.verify_graph."""
from __future__ import annotations

import pytest

from src.agent.daytona_exec import DaytonaExecutor


@pytest.mark.asyncio
async def test_verify_graph_small_valid_graph() -> None:
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

    assert result["method"] == "local"
    assert result["valid"] is True
    assert result["node_count"] == 2
    assert result["edge_count"] == 1
    assert result["orphan_count"] == 0


@pytest.mark.asyncio
async def test_verify_graph_detects_orphan_edge() -> None:
    executor = DaytonaExecutor()
    graph = {
        "nodes": [{"id": "a", "label": "Alice", "properties": {}}],
        "edges": [{"source": "a", "target": "missing", "relationship": "KNOWS"}],
    }

    result = await executor.verify_graph(graph)

    assert result["method"] == "local"
    assert result["valid"] is False
    assert result["orphan_count"] == 1


@pytest.mark.asyncio
async def test_verify_graph_large_graph_no_arg_max_error() -> None:
    """Regression test: a ~3MB graph must go over stdin, not argv (ARG_MAX)."""
    executor = DaytonaExecutor()
    # Pad summaries so the serialized JSON comfortably exceeds typical ARG_MAX
    # limits (~2MB on Linux) if it were ever passed on the command line.
    padding = "x" * 2000
    nodes = [
        {"id": f"n{i}", "label": f"Node{i}", "properties": {"summary": padding}}
        for i in range(1600)
    ]
    edges = [
        {"source": f"n{i}", "target": f"n{i + 1}", "relationship": "NEXT"}
        for i in range(1599)
    ]
    graph = {"nodes": nodes, "edges": edges}

    import json

    assert len(json.dumps(graph)) > 3 * 1024 * 1024

    result = await executor.verify_graph(graph)

    assert result["method"] == "local"
    assert result["valid"] is True
    assert result["node_count"] == 1600
    assert result["edge_count"] == 1599
