"""Numeric checks for the structural-analytics script.

These drive the REAL local subprocess path (no mocking of `_local_fallback`)
so the script text itself is under test — it runs as stdlib-only Python in a
sandbox where nothing can be imported to help it, and a typo in the Brandes
or PageRank loop would otherwise only surface on demo day.
"""
from __future__ import annotations

import pytest

from src.agent.daytona_exec import DaytonaExecutor


def _nodes(ids):
    return [{"id": i, "label": i.upper(), "type": "Thing"} for i in ids]


def _edges(pairs):
    return [{"source": s, "target": t, "type": "REL"} for s, t in pairs]


@pytest.mark.asyncio
async def test_triangle_plus_isolated_node() -> None:
    """Components split on the isolate; a closed triangle clusters perfectly.

    Self-loops and parallel edges are also folded away here — they must not
    inflate the edge count or the clustering coefficient.
    """
    executor = DaytonaExecutor()
    graph = {
        "nodes": _nodes(["a", "b", "c", "lonely"]),
        "edges": _edges(
            [("a", "b"), ("b", "c"), ("c", "a"), ("b", "a"), ("a", "a")]
        ),
    }

    result = await executor.analyze_graph(graph)

    assert result["ok"] is True
    assert result["components"]["count"] == 2
    assert result["components"]["largest"] == 3
    assert result["components"]["sizes"] == [3, 1]
    assert result["edge_count"] == 3  # parallel + self-loop deduped away
    # Only the triangle's nodes have degree >= 2; each is perfectly clustered.
    assert result["avg_clustering"] == 1.0


@pytest.mark.asyncio
async def test_path_graph_middle_node_has_highest_betweenness() -> None:
    executor = DaytonaExecutor()
    ids = [f"n{i}" for i in range(5)]
    graph = {
        "nodes": _nodes(ids),
        "edges": _edges([(f"n{i}", f"n{i + 1}") for i in range(4)]),
    }

    result = await executor.analyze_graph(graph)

    assert result["ok"] is True
    assert result["betweenness_reason"] is None
    scores = {e["id"]: e["score"] for e in result["top_betweenness"]}
    assert scores["n2"] == max(scores.values())
    others = [v for k, v in scores.items() if k != "n2"]
    assert all(scores["n2"] > v for v in others)  # strictly highest
    # Endpoints of a path lie on no shortest path between other pairs.
    assert scores["n0"] == 0.0
    assert scores["n4"] == 0.0
    # A tree has no triangles.
    assert result["avg_clustering"] == 0.0


@pytest.mark.asyncio
async def test_star_hub_has_highest_pagerank() -> None:
    executor = DaytonaExecutor()
    spokes = [f"s{i}" for i in range(6)]
    graph = {
        "nodes": _nodes(["hub", *spokes]),
        "edges": _edges([("hub", s) for s in spokes]),
    }

    result = await executor.analyze_graph(graph)

    assert result["ok"] is True
    top = result["top_pagerank"][0]
    assert top["id"] == "hub"
    assert top["label"] == "HUB"
    assert top["type"] == "Thing"
    spoke_scores = [e["score"] for e in result["top_pagerank"] if e["id"] != "hub"]
    assert all(top["score"] > s for s in spoke_scores)
    assert result["components"]["count"] == 1


@pytest.mark.asyncio
async def test_betweenness_budget_skips_only_betweenness() -> None:
    """One node over the 400-node budget: betweenness off, everything else on."""
    executor = DaytonaExecutor()
    assert executor.betweenness_max == 400  # clamped default

    ids = [f"n{i}" for i in range(401)]
    graph = {
        "nodes": _nodes(ids),
        "edges": _edges([(f"n{i}", f"n{i + 1}") for i in range(400)]),
    }

    result = await executor.analyze_graph(graph)

    assert result["ok"] is True
    assert result["top_betweenness"] == []
    assert result["betweenness_reason"] is not None
    # The reason carries the REAL measured numbers, not a canned sentence.
    assert "401" in result["betweenness_reason"]
    assert "400" in result["betweenness_reason"]
    # Components and PageRank are unaffected by the betweenness budget.
    assert result["components"]["count"] == 1
    assert result["components"]["largest"] == 401
    assert len(result["top_pagerank"]) == 50  # the script's generous top-N
    assert result["skipped"] is False


@pytest.mark.asyncio
async def test_dangling_edge_endpoints_are_ignored() -> None:
    """A graph payload can carry an edge to a node it didn't ship."""
    executor = DaytonaExecutor()
    graph = {
        "nodes": _nodes(["a", "b"]),
        "edges": _edges([("a", "b"), ("a", "ghost"), ("ghost", "b")]),
    }

    result = await executor.analyze_graph(graph)

    assert result["ok"] is True
    assert result["node_count"] == 2
    assert result["edge_count"] == 1
    assert result["components"]["count"] == 1


@pytest.mark.asyncio
async def test_empty_graph_is_all_zeroes() -> None:
    executor = DaytonaExecutor()

    result = await executor.analyze_graph({"nodes": [], "edges": []})

    assert result["ok"] is True
    assert result["components"] == {"count": 0, "largest": 0, "sizes": []}
    assert result["top_pagerank"] == []
    assert result["top_betweenness"] == []
    assert result["avg_clustering"] == 0.0
