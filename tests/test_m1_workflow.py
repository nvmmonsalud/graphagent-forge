"""M1 source identity, publication callers and per-result embedding contracts."""
from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock

import pytest

import src.agent.core as core
import src.ingestion.graph_writer as writer
from src.agent.core import GraphAgent
from src.ingestion.sources import build_source, canonical_url


def test_url_title_does_not_define_identity():
    a = build_source("url", "https://a.example/x", "Same title", "content")
    b = build_source("url", "https://b.example/x", "Same title", "content")
    assert a["id"] != b["id"]
    assert a["id"] == build_source("url", a["uri"], "Renamed", "changed")["id"]
    assert a["version"] != build_source("url", a["uri"], "Renamed", "changed")["version"]


def test_url_canonicalization_preserves_path_and_query():
    assert canonical_url("HTTPS://EXAMPLE.COM:443/a?q=1&q=2#x") == (
        "https://example.com/a?q=1&q=2"
    )
    assert canonical_url("http://[2001:4860:4860::8888]:80/a") == (
        "http://[2001:4860:4860::8888]/a"
    )
    a = build_source("url", "https://example.com/?a=1", "Title", "text")
    b = build_source("url", "https://example.com/?a=2", "Title", "text")
    assert a["id"] != b["id"]
    assert a["uri"] == b["uri"] == "https://example.com/"
    signed = build_source("url", "https://example.com/x?access_token=secret", "Title", "text")
    assert "secret" not in str(signed)
    assert signed["uri"] == "https://example.com/x"
    for title in ("", "https://example.com/x?access_token=secret"):
        source = build_source(
            "url", "https://example.com/x?access_token=secret", title, "text",
        )
        assert source["title"] == "https://example.com/x"
        assert "secret" not in str(source)
    with pytest.raises(ValueError, match="credentials"):
        canonical_url("https://name:secret@example.com/")


def test_named_sources_refresh_and_are_kind_scoped(monkeypatch):
    a = build_source("text", "notes.txt", "Notes", "text")
    b = build_source("file", "notes.txt", "Notes", "text")
    assert a["id"] != b["id"]
    assert a["id"] == build_source("text", "notes.txt", "New title", "new")["id"]
    monkeypatch.setenv("KIMI_MODEL", "changed-extraction-model")
    changed = build_source("text", "notes.txt", "Notes", "text")
    assert changed["id"] == a["id"]
    assert changed["content_hash"] == a["content_hash"]
    assert changed["version"] != a["version"]


@pytest.fixture
def extracted_graph():
    return {
        "nodes": [{"id": "one", "label": "Ada"}, {"id": "two", "label": "Engine"}],
        "edges": [{"source": "one", "target": "two", "relationship": "BUILT"}],
    }


@pytest.mark.asyncio
async def test_unchanged_skips_paid_extraction_and_postprocessing(monkeypatch):
    neo4j = AsyncMock()
    source = build_source("text", "manual", "manual", "Ada")
    neo4j.get_source.return_value = source
    extract = AsyncMock()
    monkeypatch.setattr(writer, "extract_entities", extract)
    agent = GraphAgent(neo4j)
    agent.nosana = AsyncMock()
    agent.daytona = AsyncMock()
    agent._get_ws_manager = lambda: None
    result = await agent.ingest_text("Ada")
    assert result["success"] and result["unchanged"]
    assert not result["embedding_stored"]
    extract.assert_not_awaited()
    neo4j.publish_source_graph.assert_not_awaited()
    agent.nosana.get_embedding_result.assert_not_awaited()
    agent.daytona.verify_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_publication_receives_pre_extraction_version_and_namespaces(
    monkeypatch, extracted_graph,
):
    neo4j = AsyncMock()
    source = build_source("text", "manual", "manual", "new")
    neo4j.get_source.return_value = {"version": "old"}
    neo4j.publish_source_graph.return_value = {
        "nodes_written": 2, "edges_written": 1, "unchanged": False, "conflict": False,
    }
    monkeypatch.setattr(writer, "extract_entities", AsyncMock(return_value=extracted_graph))
    result = await writer.ingest_to_graph(neo4j, "new", source=source)
    call = neo4j.publish_source_graph.await_args
    assert call.kwargs["expected_version"] == "old"
    published_source = call.kwargs["source"]
    assert published_source == {**source, "coverage": result["coverage"]}
    graph = call.args[0]
    assert len(graph["nodes"][0]["id"].split("_")[0]) == 64
    assert graph["edges"][0]["source"] == graph["nodes"][0]["id"]
    assert result["source_metadata"] == published_source
    neo4j.write_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_refresh_never_requests_publication(monkeypatch):
    neo4j = AsyncMock()
    neo4j.get_source.return_value = {"version": "old"}
    monkeypatch.setattr(
        writer, "extract_entities", AsyncMock(return_value={"error": "unavailable"}),
    )
    source = build_source("text", "manual", "manual", "changed")
    result = await writer.ingest_to_graph(neo4j, "changed", source=source)
    assert not result["success"]
    neo4j.publish_source_graph.assert_not_awaited()
    neo4j.write_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_publish_conflict_is_business_failure(monkeypatch, extracted_graph):
    neo4j = AsyncMock()
    neo4j.get_source.return_value = {"version": "old"}
    neo4j.publish_source_graph.return_value = {
        "nodes_written": 0, "edges_written": 0, "unchanged": False, "conflict": True,
    }
    monkeypatch.setattr(writer, "extract_entities", AsyncMock(return_value=extracted_graph))
    source = build_source("text", "manual", "manual", "changed")
    result = await writer.ingest_to_graph(neo4j, "changed", source=source)
    assert result["success"] is False and result["conflict"] is True
    assert neo4j.publish_source_graph.await_args.kwargs["expected_version"] == "old"


@pytest.mark.asyncio
async def test_equal_titles_url_orchestration_uses_distinct_sources(monkeypatch):
    monkeypatch.setattr(core, "extract_from_url", AsyncMock(return_value={
        "content": "Ada", "title": "Common title",
    }))
    ingest = AsyncMock(return_value={"success": False})
    monkeypatch.setattr(core, "ingest_to_graph", ingest)
    agent = GraphAgent(AsyncMock())
    await agent.ingest_url("https://a.example/x")
    await agent.ingest_url("https://b.example/x")
    sources = [call.kwargs["source"] for call in ingest.await_args_list]
    assert sources[0]["id"] != sources[1]["id"]
    assert sources[0]["title"] == sources[1]["title"]


@pytest.mark.asyncio
async def test_versioned_namespace_changes_on_refresh(extracted_graph):
    old, _ = writer.prefix_and_validate(copy.deepcopy(extracted_graph), "source", "old")
    new, _ = writer.prefix_and_validate(copy.deepcopy(extracted_graph), "source", "new")
    assert old["nodes"][0]["id"] != new["nodes"][0]["id"]


@pytest.mark.asyncio
async def test_mixed_embedding_batches_preserve_per_result_metadata(monkeypatch):
    neo4j = AsyncMock()
    agent = GraphAgent(neo4j)
    agent.nosana = AsyncMock()
    # Deliberately misleading global diagnostic; callers must ignore it.
    agent.nosana.last_embedding_method = "nosana"
    nodes = [{"id": "a", "label": "Ada"}, {"id": "b", "label": "Engine"}]
    neo4j.get_nodes_by_source.return_value = nodes
    neo4j.set_node_embeddings.return_value = 2
    neo4j.get_graph_data_by_source.return_value = {"nodes": nodes, "edges": []}
    results = {
        "Ada": {"embedding": [0.1] * 384, "method": "nosana", "model": "m",
                "version": "1", "dimension": 384},
        "Engine": {"embedding": [0.2] * 384, "method": "local_hash_fallback", "model": "m",
                   "version": "1", "dimension": 384},
    }

    async def embed(text):
        await asyncio.sleep(0)
        return results[text]

    agent.nosana.get_embedding_result.side_effect = embed
    agent.daytona = AsyncMock()
    manager = AsyncMock()
    agent._get_ws_manager = lambda: manager
    result = await agent._post_ingest(
        {"success": True, "source_metadata": {"id": "source"}}, "source", "content",
    )
    neo4j.get_nodes_by_source.assert_awaited_once_with("source", limit=5000)
    items = neo4j.set_node_embeddings.await_args.args[0]
    assert items == [
        {"id": "a", **results["Ada"], "expected_label": "Ada", "expected_summary": ""},
        {"id": "b", **results["Engine"], "expected_label": "Engine", "expected_summary": ""},
    ]
    assert neo4j.set_node_embeddings.await_args.kwargs == {}
    assert result["embedding_method"] == "mixed"
    manager.broadcast.assert_awaited_once_with({"type": "graph_reset", "source": "source"})


@pytest.mark.asyncio
async def test_reset_broadcast_failure_does_not_fail_publication():
    neo4j = AsyncMock()
    neo4j.get_nodes_by_source.return_value = []
    neo4j.get_graph_data_by_source.return_value = {"nodes": [], "edges": []}
    agent = GraphAgent(neo4j)
    agent.daytona = AsyncMock()
    manager = AsyncMock()
    manager.broadcast.side_effect = RuntimeError("closed connection")
    agent._get_ws_manager = lambda: manager
    result = await agent._post_ingest(
        {"success": True, "source_metadata": {"id": "source"}}, "source", "content",
    )
    assert result["success"]
    agent.daytona.verify_graph.assert_awaited_once()


@pytest.mark.asyncio
async def test_embedding_count_respects_concurrent_graph_guard():
    neo4j = AsyncMock()
    neo4j.get_nodes_by_source.return_value = [{"id": "a", "label": "Ada", "summary": "old"}]
    # The graph changed while the provider worked; the graph guard discarded it.
    neo4j.set_node_embeddings.return_value = 0
    agent = GraphAgent(neo4j)
    agent.nosana = AsyncMock()
    agent.nosana.get_embedding_result.return_value = {
        "embedding": [0.1] * 384, "method": "nosana", "model": "m",
        "version": "1", "dimension": 384,
    }
    assert await agent._embed_source_nodes("source") == 0
    item = neo4j.set_node_embeddings.await_args.args[0][0]
    assert item["expected_summary"] == "old"
    assert item["expected_label"] == "Ada"
