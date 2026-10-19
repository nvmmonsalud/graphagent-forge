"""Offline graph transaction and semantic-space safety regressions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.graph.embedding_space import configured_embedding_space
from src.graph.neo4j_client import Neo4jClient


@pytest.mark.parametrize("vector", [None, [], [1.0] * 383, [float("nan")] * 384,
                                   [float("inf")] * 384, [True] * 384, [0.0] * 384,
                                   [10**1000] * 384])
def test_invalid_semantic_vector_is_rejected(vector):
    assert not Neo4jClient._valid_vector(vector, 384)


def test_valid_semantic_vector():
    assert Neo4jClient._valid_vector([0.1] * 384, 384)


def mocked_client():
    client = Neo4jClient("bolt://unused", "unused", "unused")
    tx = AsyncMock()
    session = AsyncMock()
    session.begin_transaction.return_value = tx
    client.driver = MagicMock()
    client.driver.session.return_value.__aenter__.return_value = session
    return client, session, tx


async def test_cancelled_publication_rolls_back_lock_and_partial_source():
    client, _, tx = mocked_client()
    tx.run.side_effect = [AsyncMock(), asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await client.publish_source_graph({"nodes": [], "edges": []},
                                          source={"id": "a", "version": "v1"},
                                          expected_version=None)
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()


async def test_invalid_graph_endpoints_never_start_publication():
    client, session, _ = mocked_client()
    with pytest.raises(ValueError, match="endpoints"):
        await client.publish_source_graph({"nodes": [], "edges": [
            {"source": "missing", "target": "missing"}]},
            source={"id": "a", "version": "v1"}, expected_version=None)
    session.begin_transaction.assert_not_awaited()


async def test_per_item_embedding_method_never_inferred_from_batch_or_diagnostic():
    client, _, tx = mocked_client()
    client._space_is_active = AsyncMock(return_value=True)
    result = AsyncMock()
    result.single.return_value = {"stored": 3}
    tx.run.return_value = result
    space = configured_embedding_space()
    items = [dict(id="good", embedding=[0.1] * 384, method="nosana", **space),
             dict(id="hash", embedding=[0.1] * 384, method="local_hash_fallback", **space),
             dict(id="legacy", embedding=[0.1] * 384)]
    assert await client.set_node_embeddings(items, method="nosana") == 3
    prepared = tx.run.call_args.kwargs["items"]
    assert [item["semantic"] for item in prepared] == [True, False, False]


async def test_stale_embedding_worker_cannot_mutate_active_index():
    client, _, tx = mocked_client()
    client._space_is_active = AsyncMock(return_value=False)
    space = configured_embedding_space()
    assert await client.set_node_embeddings([
        dict(id="node", embedding=[0.1] * 384, method="nosana", **space)]) == 0
    # Only the graph lock was acquired; no vectors were attached.
    assert tx.run.await_count == 1
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()
