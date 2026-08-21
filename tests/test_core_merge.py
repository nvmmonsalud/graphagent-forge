"""Tests for GraphAgent's merge/dedup surface in src.agent.core — offline.

GraphAgent's __init__ constructs its own NosanaClient/DaytonaExecutor and
takes only `neo4j` as a constructor arg (see src/main.py:
`app.state.agent = GraphAgent(neo4j)`). There is no existing agent-level
test module to mirror, so these tests build a GraphAgent with an AsyncMock
neo4j and then replace `.nosana` with an AsyncMock, and patch
`_get_ws_manager` (an instance-attribute override, since it's looked up as
`self._get_ws_manager()`) to hand back an AsyncMock broadcast manager.

These target the CONTRACT in the task brief. `merge_entities`,
`find_duplicates`, and the AUTO_MERGE hook in `_post_ingest` already exist in
the current src/core.py, but `neo4j.merge_nodes` / `find_duplicate_groups` /
`suggest_similar` / `set_node_embeddings(method=...)` are new Neo4jClient
surface being landed in parallel — these tests mock that surface directly on
an AsyncMock neo4j, so they don't depend on the real Neo4jClient existing yet.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agent.core import GraphAgent


def _make_agent() -> tuple[GraphAgent, AsyncMock, AsyncMock, AsyncMock]:
    """Build a GraphAgent with AsyncMock neo4j/nosana and a patched ws manager."""
    neo4j = AsyncMock()
    agent = GraphAgent(neo4j)
    nosana = AsyncMock()
    agent.nosana = nosana
    manager = AsyncMock()
    agent._get_ws_manager = lambda: manager
    return agent, neo4j, nosana, manager


# ------------------------------------------------------------------
# merge_entities
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_entities_happy_path() -> None:
    agent, neo4j, nosana, manager = _make_agent()

    canonical = {
        "id": "a",
        "label": "Acme",
        "type": "Org",
        "summary": "a company",
        "source_docs": ["doc1", "doc2"],
        "aliases": ["Acme Inc"],
    }
    neo4j.merge_nodes.return_value = {
        "ok": True,
        "canonical_id": "a",
        "aliases": ["Acme Inc"],
        "merged": 2,
        "removed_ids": ["b"],
        "canonical": canonical,
        "edges": [{"source": "a", "target": "c", "type": "WORKS_WITH"}],
    }
    nosana.get_embedding.return_value = [0.1] * 384
    nosana.last_embedding_method = "nosana"

    # Duplicate id in the request should be deduped before hitting neo4j.
    result = await agent.merge_entities(["a", "b", "a"], canonical_id=None)

    # merge_nodes awaited with deduped ids.
    call = neo4j.merge_nodes.call_args
    called_ids = call.args[0] if call.args else call.kwargs["node_ids"]
    assert list(called_ids) == ["a", "b"]

    # Re-embed canonical with the reported method.
    nosana.get_embedding.assert_awaited_once()
    neo4j.set_node_embeddings.assert_awaited_once()
    embed_call = neo4j.set_node_embeddings.call_args
    items = embed_call.args[0] if embed_call.args else embed_call.kwargs["items"]
    assert items == [{"id": "a", "embedding": [0.1] * 384}]
    assert embed_call.kwargs.get("method") == "nosana"

    # Broadcast graph_merge payload.
    manager.broadcast.assert_awaited_once()
    payload = manager.broadcast.call_args.args[0]
    assert payload["type"] == "graph_merge"
    assert payload["removed_ids"] == ["b"]
    assert payload["canonical"] == canonical
    assert payload["edges"] == [{"source": "a", "target": "c", "type": "WORKS_WITH"}]

    # Return shape.
    assert result["merged"] == 2
    assert result["canonical_id"] == "a"
    assert result["aliases"] == ["Acme Inc"]
    assert result["removed_ids"] == ["b"]
    assert result["canonical"] == canonical
    assert "error" not in result


@pytest.mark.asyncio
async def test_merge_entities_not_found() -> None:
    agent, neo4j, nosana, manager = _make_agent()
    neo4j.merge_nodes.return_value = {"ok": False, "missing": ["ghost"]}

    result = await agent.merge_entities(["a", "ghost"])

    assert result == {"error": "not_found", "missing": ["ghost"]}
    nosana.get_embedding.assert_not_awaited()
    neo4j.set_node_embeddings.assert_not_awaited()
    manager.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_entities_embedding_failure_is_tolerated() -> None:
    agent, neo4j, nosana, manager = _make_agent()

    canonical = {
        "id": "a",
        "label": "Acme",
        "type": "Org",
        "summary": "a company",
        "source_docs": ["doc1"],
        "aliases": [],
    }
    neo4j.merge_nodes.return_value = {
        "ok": True,
        "canonical_id": "a",
        "aliases": [],
        "merged": 2,
        "removed_ids": ["b"],
        "canonical": canonical,
        "edges": [],
    }
    nosana.get_embedding.side_effect = RuntimeError("nosana unreachable")

    # Must not raise.
    result = await agent.merge_entities(["a", "b"])

    assert result["merged"] == 2
    assert result["canonical_id"] == "a"
    neo4j.set_node_embeddings.assert_not_awaited()
    # Merge itself still completes and broadcasts despite the embedding failure.
    manager.broadcast.assert_awaited_once()


# ------------------------------------------------------------------
# find_duplicates
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_find_duplicates_combines_tiers_and_reason() -> None:
    agent, neo4j, nosana, manager = _make_agent()

    neo4j.find_duplicate_groups.return_value = [
        {
            "norm_label": "kimi ai",
            "nodes": [{"id": "a", "label": "Kimi AI"}, {"id": "b", "label": "Kimi, AI!"}],
        }
    ]
    neo4j.suggest_similar.return_value = {
        "pairs": [
            {
                "similarity": 0.93,
                "nodes": [{"id": "c", "label": "Anthropic"}, {"id": "d", "label": "Anthropic Inc"}],
            }
        ],
        "enabled": True,
        "reason": None,
    }

    result = await agent.find_duplicates(limit=20)

    groups = result["groups"]
    tier1 = [g for g in groups if g["tier"] == 1]
    tier2 = [g for g in groups if g["tier"] == 2]

    assert len(tier1) == 1
    assert tier1[0]["norm_label"] == "kimi ai"
    assert tier1[0]["nodes"] == neo4j.find_duplicate_groups.return_value[0]["nodes"]

    assert len(tier2) == 1
    assert tier2[0]["norm_label"] is None
    assert tier2[0]["similarity"] == 0.93
    assert tier2[0]["nodes"] == neo4j.suggest_similar.return_value["pairs"][0]["nodes"]

    assert result["tier2_reason"] is None


@pytest.mark.asyncio
async def test_find_duplicates_surfaces_disabled_reason() -> None:
    agent, neo4j, nosana, manager = _make_agent()

    neo4j.find_duplicate_groups.return_value = []
    neo4j.suggest_similar.return_value = {
        "pairs": [],
        "enabled": False,
        "reason": "fewer than 2 nodes have nosana embeddings",
    }

    result = await agent.find_duplicates(limit=20)

    assert result["groups"] == []
    assert result["tier2_reason"] == "fewer than 2 nodes have nosana embeddings"


# ------------------------------------------------------------------
# AUTO_MERGE hook in _post_ingest
# ------------------------------------------------------------------
def _stub_post_ingest_collaborators(agent: GraphAgent, neo4j: AsyncMock) -> None:
    """Stub _post_ingest's other collaborators so only AUTO_MERGE is exercised."""
    neo4j.get_nodes_by_source.return_value = []
    neo4j.get_graph_data_by_source.return_value = {"nodes": [], "edges": []}
    agent.daytona = AsyncMock()
    agent.daytona.verify_graph.return_value = {"valid": True}


@pytest.mark.asyncio
async def test_auto_merge_exact_merges_duplicate_groups(monkeypatch) -> None:
    agent, neo4j, nosana, manager = _make_agent()
    _stub_post_ingest_collaborators(agent, neo4j)
    monkeypatch.setenv("AUTO_MERGE", "exact")

    groups = [
        {"norm_label": "kimi ai", "nodes": [{"id": "a"}, {"id": "b"}]},
        {"norm_label": "anthropic", "nodes": [{"id": "c"}, {"id": "d"}]},
    ]
    neo4j.find_duplicate_groups.return_value = groups

    merge_calls: list[list[str]] = []

    async def fake_merge_entities(node_ids, canonical_id=None):
        merge_calls.append(list(node_ids))
        canonical_id = node_ids[0]
        return {
            "merged": len(node_ids),
            "canonical_id": canonical_id,
            "aliases": [],
            "removed_ids": node_ids[1:],
            "canonical": {"id": canonical_id, "label": f"label-{canonical_id}"},
        }

    agent.merge_entities = fake_merge_entities

    result = {"success": True}
    out = await agent._post_ingest(result, source_doc="doc1", content="some text")

    neo4j.find_duplicate_groups.assert_awaited_once()
    call = neo4j.find_duplicate_groups.call_args
    assert call.kwargs.get("limit") == 50
    assert call.kwargs.get("source_doc") == "doc1"

    assert merge_calls == [["a", "b"], ["c", "d"]]

    assert out["merged_entities"] == [
        {"canonical_id": "a", "label": "label-a", "merged": 2},
        {"canonical_id": "c", "label": "label-c", "merged": 2},
    ]


@pytest.mark.asyncio
async def test_auto_merge_not_invoked_when_env_unset() -> None:
    agent, neo4j, nosana, manager = _make_agent()
    _stub_post_ingest_collaborators(agent, neo4j)
    # AUTO_MERGE deliberately not set (autouse _isolated_env fixture clears
    # the sponsor-service vars but not AUTO_MERGE — assert directly to be sure
    # this test isn't accidentally relying on ambient environment).
    import os
    assert os.getenv("AUTO_MERGE") != "exact"

    result = {"success": True}
    out = await agent._post_ingest(result, source_doc="doc1", content="some text")

    neo4j.find_duplicate_groups.assert_not_awaited()
    assert "merged_entities" not in out


# ------------------------------------------------------------------
# Progress reporting in _post_ingest (job-queue contract)
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_post_ingest_reports_progress_stages_and_tolerates_raising_callback() -> None:
    agent, neo4j, nosana, manager = _make_agent()
    _stub_post_ingest_collaborators(agent, neo4j)
    import os
    assert os.getenv("AUTO_MERGE") != "exact"

    stages: list[str] = []

    async def recorder(stage: str) -> None:
        stages.append(stage)

    out = await agent._post_ingest(
        {"success": True}, source_doc="d", content="c", progress=recorder
    )
    assert stages == ["embedding", "broadcasting", "verifying"]
    assert out["success"] is True

    async def raising_recorder(stage: str) -> None:
        raise RuntimeError("progress callback exploded")

    # A broken progress callback must never fail the ingest.
    out2 = await agent._post_ingest(
        {"success": True}, source_doc="d", content="c", progress=raising_recorder
    )
    assert out2["success"] is True
