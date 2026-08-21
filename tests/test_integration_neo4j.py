"""Integration tests against a real Neo4j instance.

Skipped unless NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD are set in the
environment. Run explicitly with: pytest -m integration
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
    return f"test-source-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_write_graph_and_read_back(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [
            {"id": f"{source_doc}_a", "label": "Alice", "type": "Person",
             "properties": {"summary": "a person"}},
            {"id": f"{source_doc}_b", "label": "Acme", "type": "Org",
             "properties": {"summary": "a company"}},
        ],
        "edges": [
            {"source": f"{source_doc}_a", "target": f"{source_doc}_b",
             "relationship": "WORKS_AT", "properties": {"context": "since 2020"}},
        ],
    }

    try:
        write_result = await neo4j.write_graph(graph_data, source_doc=source_doc)
        assert write_result["nodes_written"] == 2
        assert write_result["edges_written"] == 1

        data = await neo4j.get_graph_data_by_source(source_doc)
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["type"] == "WORKS_AT"
    finally:
        await neo4j.delete_source(source_doc)


@pytest.mark.asyncio
async def test_vector_search_does_not_raise(neo4j) -> None:
    results = await neo4j.vector_search([0.1] * 384, limit=5)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_sources_delete_and_clear_round_trip(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [
            {"id": f"{source_doc}_x", "label": "X", "type": "Concept", "properties": {}},
        ],
        "edges": [],
    }
    await neo4j.write_graph(graph_data, source_doc=source_doc)

    sources = await neo4j.get_sources()
    assert any(s["source_doc"] == source_doc for s in sources)

    delete_result = await neo4j.delete_source(source_doc)
    assert delete_result["deleted_nodes"] == 1

    sources_after = await neo4j.get_sources()
    assert not any(s["source_doc"] == source_doc for s in sources_after)


@pytest.mark.asyncio
async def test_clear_graph_removes_everything(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [{"id": f"{source_doc}_z", "label": "Z", "type": "Concept", "properties": {}}],
        "edges": [],
    }
    await neo4j.write_graph(graph_data, source_doc=source_doc)

    result = await neo4j.clear_graph()
    assert result["deleted_nodes"] >= 1

    stats = await neo4j.get_stats()
    assert stats["nodes"] == 0


@pytest.mark.asyncio
async def test_set_node_embeddings_then_vector_search_finds_node(neo4j, source_doc) -> None:
    node_id = f"{source_doc}_embed"
    graph_data = {
        "nodes": [{"id": node_id, "label": "EmbeddedNode", "type": "Concept", "properties": {}}],
        "edges": [],
    }
    try:
        await neo4j.write_graph(graph_data, source_doc=source_doc)

        embedding = [0.05] * 384
        await neo4j.set_node_embeddings([{"id": node_id, "embedding": embedding}])

        results = await neo4j.vector_search(embedding, limit=5)
        ids = [r["id"] for r in results]
        assert node_id in ids
    finally:
        await neo4j.delete_source(source_doc)
