"""Re-embedding is explicit, bounded, and never indexes fallback vectors."""
from unittest.mock import AsyncMock

from scripts.reembed_graph import reembed
from src.graph.embedding_space import configured_embedding_space


async def test_dry_run_has_no_provider_or_schema_side_effects():
    client, provider = AsyncMock(), AsyncMock()
    client.get_all_graph_data.return_value = {
        "nodes": [{"id": "a", "label": "A"}], "truncated": False,
    }
    report = await reembed(client, provider)
    assert report["planned"] == 1 and report["stored"] == 0
    provider.get_embedding_result.assert_not_called()
    client.set_node_embeddings.assert_not_called()
    client.init_schema.assert_not_called()


async def test_apply_skips_fallback_and_guards_stale_content():
    client, provider = AsyncMock(), AsyncMock()
    client.get_nodes_by_source.return_value = [
        {"id": "a", "label": "A", "summary": "old evidence"},
        {"id": "b", "label": "B"},
    ]
    provider.get_embedding_result.side_effect = [
        {"embedding": [0.1] * 384, "method": "nosana", **configured_embedding_space()},
        {"embedding": [0.1] * 384, "method": "local_hash_fallback",
         **configured_embedding_space()},
    ]
    client.set_node_embeddings.return_value = 0
    report = await reembed(client, provider, source_doc="source", apply=True)
    assert report["stored"] == 0
    assert report["unavailable"] == 1 and report["rejected"] == 1
    item = client.set_node_embeddings.await_args.args[0][0]
    assert item["expected_label"] == "A" and item["expected_summary"] == "old evidence"
    client.set_node_embeddings.assert_awaited_once()
    client.get_nodes_by_source.assert_awaited_once_with("source", limit=5000)
