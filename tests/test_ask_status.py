"""Contract tests for GraphRAGEngine.query's `status` field.

`status` is one of answered, no_context, llm_unavailable, insufficient_evidence; the
frontend uses it to distinguish a real (if underwhelming) answer from a
plumbing failure, instead of guessing from the answer text.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.graph.graphrag import GraphRAGEngine
from src.ingestion.entity_parser import LLM_UNAVAILABLE_PREFIX


def _mock_nosana(method: str = "local_hash_fallback") -> AsyncMock:
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = {
        "embedding": [0.1] * 384, "method": method,
    }
    return nosana


@pytest.mark.asyncio
async def test_no_candidates_yields_no_context_status() -> None:
    neo4j = AsyncMock()
    neo4j.search_nodes.return_value = []
    neo4j.search_evidence.return_value = []
    engine = GraphRAGEngine(neo4j=neo4j, nosana=_mock_nosana())

    result = await engine.query("What is the meaning of life?")

    assert result["status"] == "no_context"
    assert result["context_nodes"] == []
    assert result["sources"] == []


@pytest.mark.asyncio
async def test_candidates_but_missing_kimi_key_yields_llm_unavailable_status(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)

    neo4j = AsyncMock()
    neo4j.search_nodes.return_value = [
        {"id": "n1", "label": "Test Entity", "source_docs": ["doc1"]}
    ]
    neo4j.get_entity_context.return_value = {
        "text": "Test Entity (Concept): a thing", "source_docs": ["doc1"],
        "entity_ids": ["n1"], "nodes": [], "edges": [],
    }
    neo4j.search_evidence.return_value = [{
        "id": "ev_one", "source_id": "doc1", "version": "v1",
        "text": "Original", "start": 0, "end": 8,
    }]
    engine = GraphRAGEngine(neo4j=neo4j, nosana=_mock_nosana())

    result = await engine.query("Tell me about the test entity")

    assert result["status"] == "llm_unavailable"
    assert result["answer"].startswith(LLM_UNAVAILABLE_PREFIX)
    assert result["context_nodes"] == ["Test Entity"]


@pytest.mark.asyncio
async def test_status_field_is_one_of_the_four_literals() -> None:
    neo4j = AsyncMock()
    neo4j.search_nodes.return_value = []
    neo4j.search_evidence.return_value = []
    engine = GraphRAGEngine(neo4j=neo4j, nosana=_mock_nosana())

    result = await engine.query("anything")

    assert result["status"] in {
        "answered", "no_context", "llm_unavailable", "insufficient_evidence",
    }
