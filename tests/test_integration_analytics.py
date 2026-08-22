"""Integration tests for Neo4jClient.get_analytics against a real Neo4j.

Skipped unless NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD are set in the
environment. Run explicitly with: pytest -m integration

The point of running these live is the Cypher itself: `COUNT { (n)--() }` and
`coalesce(r.type, type(r))` are the two constructs that can't be exercised
offline, and neither GDS nor APOC is available to lean on.
"""
from __future__ import annotations

import os
import uuid

import pytest

from src.graph.neo4j_client import Neo4jClient

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD),
        reason="NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD not set — no live Neo4j to test against",
    ),
]


@pytest.fixture
async def neo4j():
    client = Neo4jClient(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    await client.connect()
    await client.init_schema()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def source_doc():
    return f"test-analytics-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_get_analytics_counts_types_degrees_and_isolates(neo4j, source_doc) -> None:
    """A star of 3 plus one isolate, under types unique to this run.

    Every count assertion is scoped by a uuid-derived type/relationship name,
    and the shared totals (isolated_nodes) are asserted as a DELTA, so the
    test stays exact whatever else lives in the database.
    """
    tag = source_doc.replace("-", "_")
    hub_type = f"Hub_{tag}"
    spoke_type = f"Spoke_{tag}"
    rel_type = f"LINKS_{tag}"

    nodes = [
        {"id": f"{source_doc}_hub", "label": "Hub", "type": hub_type,
         "properties": {"summary": "the centre"}},
        {"id": f"{source_doc}_s1", "label": "Spoke1", "type": spoke_type,
         "properties": {"summary": "a spoke"}},
        {"id": f"{source_doc}_s2", "label": "Spoke2", "type": spoke_type,
         "properties": {"summary": "a spoke"}},
        {"id": f"{source_doc}_s3", "label": "Spoke3", "type": spoke_type,
         "properties": {"summary": "a spoke"}},
        {"id": f"{source_doc}_alone", "label": "Alone", "type": spoke_type,
         "properties": {"summary": "no edges at all"}},
    ]
    edges = [
        {"source": f"{source_doc}_hub", "target": f"{source_doc}_s{i}",
         "relationship": rel_type, "properties": {"context": "star"}}
        for i in (1, 2, 3)
    ]

    before = await neo4j.get_analytics(top=50)

    try:
        await neo4j.write_graph({"nodes": nodes, "edges": edges}, source_doc=source_doc)

        after = await neo4j.get_analytics(top=50)

        assert set(after) == {"node_types", "edge_types", "top_degree", "isolated_nodes"}

        node_counts = {row["type"]: row["count"] for row in after["node_types"]}
        assert node_counts[hub_type] == 1
        assert node_counts[spoke_type] == 4

        edge_counts = {row["type"]: row["count"] for row in after["edge_types"]}
        # The semantic name lives in r.type; a raw "RELATES_TO" here would mean
        # the coalesce() on the edge read was dropped.
        assert edge_counts[rel_type] == 3
        assert rel_type != "RELATES_TO"

        degrees = {row["id"]: row["degree"] for row in after["top_degree"]}
        if f"{source_doc}_hub" in degrees:
            assert degrees[f"{source_doc}_hub"] == 3
            hub_row = next(r for r in after["top_degree"] if r["id"] == f"{source_doc}_hub")
            assert hub_row["label"] == "Hub"
            assert hub_row["type"] == hub_type
        else:
            # Legitimately crowded out of the top 50 by a busier graph.
            assert all(d >= 3 for d in degrees.values())

        # Ordering contract: degree descending.
        ordered = [row["degree"] for row in after["top_degree"]]
        assert ordered == sorted(ordered, reverse=True)

        assert after["isolated_nodes"] - before["isolated_nodes"] == 1
    finally:
        await neo4j.delete_source(source_doc)

    cleaned = await neo4j.get_analytics(top=50)
    cleaned_node_types = {row["type"] for row in cleaned["node_types"]}
    cleaned_edge_types = {row["type"] for row in cleaned["edge_types"]}
    assert hub_type not in cleaned_node_types
    assert spoke_type not in cleaned_node_types
    assert rel_type not in cleaned_edge_types
    assert cleaned["isolated_nodes"] == before["isolated_nodes"]


@pytest.mark.asyncio
async def test_get_analytics_clamps_top(neo4j) -> None:
    """`top` is clamped, not interpolated raw — it lands in a literal LIMIT."""
    result = await neo4j.get_analytics(top=10_000)
    assert len(result["top_degree"]) <= 50

    result = await neo4j.get_analytics(top=0)
    assert len(result["top_degree"]) <= 1
