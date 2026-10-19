"""Offline evidence identity, transaction, and bounded-query regressions."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.graph.neo4j_client import Neo4jClient
from src.ingestion.chunks import plan_chunks
from tests.test_m1_graph import mocked_client


def source():
    return {"id": "s", "version": "v"}


def data():
    chunks = plan_chunks("Unicode 日本語 evidence", "s", "v")
    return {"nodes": [{"id": "n", "evidence_ids": [chunks[0]["id"]]}],
            "edges": [], "chunks": chunks}


@pytest.mark.parametrize("field,value", [
    ("id", "ev_invented"), ("version", "wrong"), ("source_id", "other"),
    ("ordinal", 1), ("start", -1), ("end", 200001), ("text", "different"),
    ("start", True), ("ordinal", False),
])
async def test_bad_chunk_is_rejected_before_transaction(field, value):
    db, session, _ = mocked_client()
    graph = data()
    graph["chunks"][0][field] = value
    with pytest.raises(ValueError):
        await db.publish_source_graph(graph, source=source(), expected_version=None)
    session.begin_transaction.assert_not_awaited()


@pytest.mark.parametrize("refs", [["invented"], "bad", [None], ["ev_bad"] * 13])
async def test_graph_cannot_invent_evidence_refs(refs):
    db, session, _ = mocked_client()
    graph = data()
    graph["nodes"][0]["evidence_ids"] = refs
    with pytest.raises(ValueError, match="references"):
        await db.publish_source_graph(graph, source=source(), expected_version=None)
    session.begin_transaction.assert_not_awaited()


async def test_bad_edge_reference_is_rejected():
    db, _, _ = mocked_client()
    graph = data()
    graph["edges"] = [{"source": "n", "target": "n", "evidence_ids": ["bad"]}]
    with pytest.raises(ValueError, match="references"):
        await db.publish_source_graph(graph, source=source(), expected_version=None)


def test_exact_unicode_offsets_roundtrip():
    content = "é日本語🙂" * 2000
    chunks = plan_chunks(content, "s", "v")
    assert Neo4jClient._prepare_chunks({"chunks": chunks}, source()) == chunks
    assert all(content[c["start"]:c["end"]] == c["text"] for c in chunks)


async def test_failed_chunk_write_rolls_back_source_and_graph():
    db, _, tx = mocked_client()
    db._remove_source_graph = AsyncMock()
    result = AsyncMock()
    result.single.return_value = {"version": None}
    tx.run.side_effect = [result, result, result, result, RuntimeError("chunk write failed")]
    with pytest.raises(RuntimeError, match="chunk write"):
        await db.publish_source_graph(data(), source=source(), expected_version=None)
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()


async def test_cancelled_evidence_read_releases_mutation_lock():
    db, _, tx = mocked_client()
    tx.run.side_effect = [AsyncMock(), asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await db.get_evidence("ev_123")
    tx.rollback.assert_awaited_once()


async def test_search_parameters_and_result_limit_are_bounded_without_entities():
    db, _, _ = mocked_client()
    db._read_evidence = AsyncMock(return_value=[])
    await db.search_evidence("' malicious MATCH DELETE " + "word " * 2000, limit=1000)
    call = db._read_evidence.call_args
    assert call.kwargs["limit"] == 12
    assert call.kwargs["entity_ids"] == []
    assert len(call.kwargs["terms"]) <= 32
    assert "malicious" not in call.args[0]
    assert "ORDER BY lexical DESC" in call.args[0]
    assert "c.version = s.version" in call.args[0]


@pytest.mark.parametrize("ids", [["x"] * 37, [None], ["x" * 81], "text"])
async def test_evidence_validation_bounds(ids):
    db, session, _ = mocked_client()
    with pytest.raises(ValueError):
        await db.validate_evidence_ids(ids)
    session.begin_transaction.assert_not_awaited()
