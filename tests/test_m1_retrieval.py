"""M1 retrieval identity, complete evidence and embedding provenance regressions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.agent.nosana_client import NosanaClient
from src.graph.embedding_space import configured_embedding_space
from src.graph.graphrag import GraphRAGEngine


def _embedding(method="local_hash_fallback", **changes):
    return {
        **configured_embedding_space(), "embedding": [0.1] * 384,
        "method": method, **changes,
    }


def _context(entity_id, sources, summary):
    return {
        "text": summary, "entity_ids": [entity_id], "source_docs": sources,
        "nodes": [{"id": entity_id, "summary": summary, "source_docs": sources}], "edges": [],
    }


@pytest.mark.asyncio
async def test_duplicate_labels_retrieve_exact_ids_and_all_traversed_sources():
    graph = AsyncMock()
    graph.search_nodes.return_value = [
        {"id": "a", "label": "Mercury", "source_docs": ["planet"]},
        {"id": "b", "label": "Mercury", "source_docs": ["element"]},
    ]
    contexts = {
        "a": _context("a", ["planet", "orbit"], "Mercury is a planet"),
        "b": _context("b", ["element"], "Mercury is an element"),
    }
    graph.get_entity_context.side_effect = lambda entity_id, depth: contexts[entity_id]
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = _embedding()
    with patch("src.graph.graphrag.answer_query", new_callable=AsyncMock) as answer:
        answer.return_value = "Both senses have supporting evidence."
        result = await GraphRAGEngine(graph, nosana).query("Mercury")
    assert [call.args[0] for call in graph.get_entity_context.await_args_list] == ["a", "b"]
    graph.get_node_context.assert_not_called()
    assert result["source_docs"] == ["planet", "orbit", "element"]
    assert result["evidence"] == [contexts["a"], contexts["b"]]
    assert "planet" in answer.call_args.args[1] and "element" in answer.call_args.args[1]
    assert result["status"] == "answered"


@pytest.mark.asyncio
async def test_isolated_entity_summary_is_answer_context():
    graph = AsyncMock()
    graph.search_nodes.return_value = [{"id": "solo", "label": "Solo"}]
    graph.get_entity_context.return_value = _context("solo", ["source"], "Isolated summary")
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = _embedding()
    with patch("src.graph.graphrag.answer_query", new_callable=AsyncMock) as answer:
        answer.return_value = "Grounded answer"
        result = await GraphRAGEngine(graph, nosana).query("Solo")
    assert answer.call_args.args[1] == "Isolated summary"
    assert result["evidence"][0]["edges"] == []
    assert result["source_docs"] == ["source"]


@pytest.mark.asyncio
async def test_disappeared_candidates_return_no_context_without_llm():
    graph = AsyncMock()
    graph.search_nodes.return_value = [{"id": "gone", "label": "Gone"}]
    graph.get_entity_context.return_value = _context("gone", [], "")
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = _embedding()
    with patch("src.graph.graphrag.answer_query", new_callable=AsyncMock) as answer:
        result = await GraphRAGEngine(graph, nosana).query("Gone")
    answer.assert_not_called()
    assert result["status"] == "no_context"
    assert result["evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"method": "local_hash_fallback"}, {"model": "other"}, {"version": "other"},
    {"dimension": 768}, {"embedding": [float("nan")] * 384},
])
async def test_incompatible_embedding_never_queries_semantic_index(changes):
    graph = AsyncMock()
    graph.search_nodes.return_value = []
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = {**_embedding("nosana"), **changes}
    # A stale diagnostic must never authorize a fallback vector for retrieval.
    nosana.last_embedding_method = "nosana"
    await GraphRAGEngine(graph, nosana).query("question")
    graph.vector_search.assert_not_called()


@pytest.mark.asyncio
async def test_semantic_search_supplies_active_model_version():
    graph = AsyncMock()
    graph.search_nodes.return_value = []
    graph.vector_search.return_value = []
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = _embedding("nosana")
    nosana.last_embedding_method = "local_hash_fallback"
    await GraphRAGEngine(graph, nosana).query("question")
    space = configured_embedding_space()
    graph.vector_search.assert_awaited_once_with(
        [0.1] * 384, limit=10, model=space["model"], version=space["version"],
    )


@pytest.mark.asyncio
async def test_concurrent_embedding_results_keep_per_call_metadata(monkeypatch):
    monkeypatch.setenv("NOSANA_API_KEY", "test-key")
    monkeypatch.setenv("NOSANA_EMBEDDING_URL", "https://example.test/embedding")
    monkeypatch.setenv("NOSANA_EMBEDDING_MODEL", "semantic-v1")
    monkeypatch.setenv("NOSANA_EMBEDDING_VERSION", "revision-2")
    client = NosanaClient()
    first_started = asyncio.Event()
    fallback_finished = asyncio.Event()

    async def post(url, *, json, **kwargs):
        if json["input"] == "semantic":
            first_started.set()
            await fallback_finished.wait()
            body = {"embedding": [0.1] * 384}
        else:
            await first_started.wait()
            fallback_finished.set()
            body = {"embedding": []}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    client._client = AsyncMock()
    client._client.is_closed = False
    client._client.post.side_effect = post
    semantic, fallback = await asyncio.gather(
        client.get_embedding_result("semantic"), client.get_embedding_result("fallback"),
    )
    assert semantic["method"] == "nosana"
    assert fallback["method"] == "local_hash_fallback"
    assert fallback["embedding"] == client._hash_embedding("fallback")
    for result in [semantic, fallback]:
        assert result["model"] == "semantic-v1"
        assert result["version"] == "revision-2"
        assert result["dimension"] == 384
    semantic["model"] = "mutated-result"
    assert fallback["model"] == "semantic-v1"
    assert client.embedding_space["model"] == "semantic-v1"
    await client.aclose()


def test_endpoint_model_fingerprint_contains_no_url_credentials(monkeypatch):
    monkeypatch.delenv("NOSANA_EMBEDDING_MODEL", raising=False)
    monkeypatch.setenv("NOSANA_EMBEDDING_URL", "https://user:password@host/embed?token=secret")
    monkeypatch.delenv("NOSANA_EMBEDDING_VERSION", raising=False)
    first = configured_embedding_space()
    assert first["version"] == "1"
    assert first["dimension"] == 384
    assert first["model"].startswith("endpoint-sha256:")
    assert all(secret not in first["model"] for secret in ["password", "host", "secret"])
    monkeypatch.setenv("NOSANA_EMBEDDING_URL", "https://other.test/embed")
    assert configured_embedding_space()["model"] != first["model"]
