"""Predeclared curated offline M2 gates; no inference/entailment quality claims.

Retrieved candidates and provider outputs are fixture inputs. This evaluates
orchestration, exact original-excerpt offsets and citation identifier validity,
not a real provider's answers or the graph database's search ranking.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.graph.graphrag import GraphRAGEngine

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "m2_evaluation.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["name"])
async def test_curated_evidence_orchestration(case):
    documents, excerpts = {}, {}
    for source in FIXTURE["sources"]:
        prefix = source.get("prefix", "x" * source.get("prefix_chars", 0))
        full_text = prefix + source["text"]
        documents[source["source_id"]] = full_text
        excerpts[source["evidence_id"]] = {
            "id": source["evidence_id"], "source_id": source["source_id"],
            "version": source["version"], "ordinal": 0,
            "start": len(prefix), "end": len(full_text), "text": source["text"],
            "title": source["source_id"], "uri": "",
        }
    retrieved = [excerpts[key] for key in case["retrieved"]]
    graph = AsyncMock()
    graph.search_nodes.return_value = []
    graph.search_evidence.return_value = retrieved
    graph.validate_evidence_ids.return_value = retrieved
    nosana = AsyncMock()
    nosana.get_embedding_result.return_value = {"method": "local_hash_fallback"}

    async def generate(question, sent):
        assert question == case["question"]
        assert [chunk["id"] for chunk in sent] == case["retrieved"]
        for chunk in sent:
            assert documents[chunk["source_id"]][chunk["start"]:chunk["end"]] == chunk["text"]
        if case.get("refresh"):
            # Refresh becomes visible while the provider is generating an answer.
            graph.validate_evidence_ids.return_value = [
                {**chunk, "version": "v2"} if chunk["id"] == case["refresh"] else chunk
                for chunk in retrieved
            ]
        return {"answer": "Fixture answer from the supplied excerpts.",
                "citations": case["citations"], "insufficient_evidence": False,
                "conflicts": case.get("conflicts", [])}

    with patch("src.graph.graphrag.answer_with_citations", new=AsyncMock(side_effect=generate)):
        result = await GraphRAGEngine(graph, nosana).query(case["question"])
    assert result["status"] == case["expected_status"]
    if result["status"] == "answered":
        assert [citation["id"] for citation in result["citations"]] == case["citations"]
        assert result["conflicts"] == case.get("conflicts", [])
        if "expected_start" in case:
            assert result["citations"][0]["start"] == case["expected_start"]
    else:
        assert result["citations"] == []
