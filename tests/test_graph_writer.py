"""Tests for src.ingestion.graph_writer.ingest_to_graph."""
from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from src.ingestion.graph_writer import ingest_to_graph


def _doc_hash(source_doc: str) -> str:
    return hashlib.md5(source_doc.encode()).hexdigest()[:8]


@pytest.mark.asyncio
async def test_id_prefixing_and_edge_remap() -> None:
    source_doc = "https://example.com/article"
    extracted = {
        "nodes": [
            {"id": "n1", "label": "Alice", "type": "Person", "properties": {}},
            {"id": "n2", "label": "Acme", "type": "Org", "properties": {}},
        ],
        "edges": [
            {"source": "n1", "target": "n2", "relationship": "WORKS_AT", "properties": {}},
        ],
    }
    neo4j = AsyncMock()
    neo4j.write_graph.return_value = {"nodes_written": 2, "edges_written": 1}

    with patch(
        "src.ingestion.graph_writer.extract_entities", AsyncMock(return_value=extracted)
    ):
        result = await ingest_to_graph(neo4j, "some content", source_doc=source_doc)

    prefix = _doc_hash(source_doc)
    written_graph = neo4j.write_graph.call_args.args[0]
    node_ids = {n["id"] for n in written_graph["nodes"]}
    assert node_ids == {f"{prefix}_n1", f"{prefix}_n2"}

    edge = written_graph["edges"][0]
    assert edge["source"] == f"{prefix}_n1"
    assert edge["target"] == f"{prefix}_n2"

    assert result["success"] is True
    assert result["nodes"] == 2
    assert result["edges"] == 1
    assert result["dropped_edges"] == 0
    neo4j.write_graph.assert_awaited_once()


@pytest.mark.asyncio
async def test_hallucinated_edges_dropped() -> None:
    source_doc = "doc-2"
    extracted = {
        "nodes": [
            {"id": "n1", "label": "Alice", "type": "Person", "properties": {}},
        ],
        "edges": [
            # Points at an id the LLM never declared as a node.
            {"source": "n1", "target": "ghost", "relationship": "KNOWS", "properties": {}},
        ],
    }
    neo4j = AsyncMock()
    neo4j.write_graph.return_value = {"nodes_written": 1, "edges_written": 0}

    with patch(
        "src.ingestion.graph_writer.extract_entities", AsyncMock(return_value=extracted)
    ):
        result = await ingest_to_graph(neo4j, "some content", source_doc=source_doc)

    written_graph = neo4j.write_graph.call_args.args[0]
    assert written_graph["edges"] == []
    assert result["dropped_edges"] == 1
    assert result["extracted_edges"] == 0
    assert result["success"] is True


@pytest.mark.asyncio
async def test_extraction_error_propagates_and_skips_write() -> None:
    neo4j = AsyncMock()

    with patch(
        "src.ingestion.graph_writer.extract_entities",
        AsyncMock(return_value={"nodes": [], "edges": [], "error": "KIMI_API_KEY not configured"}),
    ):
        result = await ingest_to_graph(neo4j, "some content", source_doc="doc-3")

    assert result["success"] is False
    assert result["error"] == "KIMI_API_KEY not configured"
    neo4j.write_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_nodes_extracted_reports_failure_without_write() -> None:
    neo4j = AsyncMock()

    with patch(
        "src.ingestion.graph_writer.extract_entities",
        AsyncMock(return_value={"nodes": [], "edges": []}),
    ):
        result = await ingest_to_graph(neo4j, "some content", source_doc="doc-4")

    assert result["success"] is False
    neo4j.write_graph.assert_not_awaited()


# ------------------------------------------------------------------
# sanitize_graph — LLM output is untrusted input
# ------------------------------------------------------------------
def test_sanitize_drops_malformed_items_and_clips_strings() -> None:
    from src.ingestion import graph_writer

    data = {
        "nodes": [
            "not a dict",
            {"id": ["list"], "label": "Bad id"},
            {"id": "a", "label": {"nested": 1}},
            {"id": "a", "label": "A" * 500, "type": 7, "properties": {"summary": "s" * 5000}},
            {"id": "a", "label": "Duplicate id"},
            {"id": 2, "label": "Numeric id", "properties": "not a dict"},
        ],
        "edges": [
            None,
            {"source": "a", "target": None},
            {"source": "a", "target": 2, "relationship": ["X"], "properties": {"context": 9}},
        ],
    }

    out, dropped_nodes, dropped_edges = graph_writer.sanitize_graph(data)

    assert [n["id"] for n in out["nodes"]] == ["a", "2"]
    first = out["nodes"][0]
    assert len(first["label"]) == graph_writer.MAX_LABEL_LEN
    assert first["type"] == "7"
    assert len(first["properties"]["summary"]) == graph_writer.MAX_SUMMARY_LEN
    assert out["nodes"][1]["properties"] == {"summary": ""}
    assert out["edges"] == [
        {"source": "a", "target": "2", "relationship": "RELATES_TO",
         "properties": {"context": "9"}},
    ]
    assert (dropped_nodes, dropped_edges) == (4, 2)


def test_sanitize_caps_node_and_edge_counts() -> None:
    from src.ingestion import graph_writer

    n = graph_writer.MAX_NODES + 50
    data = {
        "nodes": [{"id": f"n{i}", "label": f"N{i}"} for i in range(n)],
        "edges": [{"source": "n0", "target": "n1"}] * (graph_writer.MAX_EDGES + 5),
    }
    out, dropped_nodes, dropped_edges = graph_writer.sanitize_graph(data)
    assert len(out["nodes"]) == graph_writer.MAX_NODES
    assert len(out["edges"]) == graph_writer.MAX_EDGES
    assert (dropped_nodes, dropped_edges) == (50, 5)


def test_sanitize_handles_non_list_payloads() -> None:
    from src.ingestion import graph_writer

    out, dropped_nodes, dropped_edges = graph_writer.sanitize_graph({"nodes": "x", "edges": 5})
    assert out["nodes"] == [] and out["edges"] == []
    assert (dropped_nodes, dropped_edges) == (0, 0)
