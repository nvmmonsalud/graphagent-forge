"""M1 transaction/security regressions. Only run on an explicitly disposable database."""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from neo4j.exceptions import ConstraintError

from src.graph.embedding_space import configured_embedding_space
from src.graph.neo4j_client import Neo4jClient
from src.graph.source_migration import migrate_sources, rollback_sources

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("M1_DISPOSABLE_NEO4J") != "1", reason="Requires M1_DISPOSABLE_NEO4J=1")]


@pytest.fixture
async def db():
    client = Neo4jClient(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                         os.environ["NEO4J_PASSWORD"])
    await client.connect()
    await client.init_schema()
    await client.clear_graph()
    async with client.driver.session() as session:
        await session.run("MATCH (s:Source) DELETE s")
    try:
        yield client
    finally:
        await client.clear_graph()
        await client.close()


def source(sid="src_a", version="v1"):
    return dict(id=sid, title="Identical title", kind="url", uri="https://example.org/" + sid,
                content_hash="hash_" + version, version=version, extraction_version="test-v1")


def graph(prefix="a", summary="Center evidence"):
    return {"nodes": [{"id": prefix, "label": "Same label", "type": "Person",
                       "properties": {"summary": summary}}], "edges": []}


async def publish(db, sid="src_a", version="v1", expected=None, data=None):
    return await db.publish_source_graph(data or graph(sid + version), source=source(sid, version),
                                         expected_version=expected)


async def query(db, text, **kwargs):
    async with db.driver.session() as session:
        result = await session.run(text, **kwargs)
        return [dict(r) async for r in result]


async def test_isolated_summary_exact_id_and_title_collision(db):
    await publish(db, data=graph("alice", "Selected center summary"))
    await publish(db, sid="src_b", data=graph("unrelated", "Must not enter selected context"))
    evidence = await db.get_entity_context("alice")
    assert evidence["entity_ids"] == ["alice"]
    assert "Selected center summary" in evidence["text"]
    assert "Must not enter" not in evidence["text"]
    assert evidence["source_docs"] == ["src_a"]
    assert {s["source_doc"] for s in await db.get_sources()} == {"src_a", "src_b"}
    assert len({s["title"] for s in await db.get_sources()}) == 1


async def test_concurrent_first_publication_conflicts_and_refresh_is_atomic(db):
    assert await db.get_source("src_a") is None
    results = await asyncio.gather(publish(db, version="v1"), publish(db, version="v2"))
    assert sum(r["conflict"] for r in results) == 1
    current = await db.get_source("src_a")
    version = current["version"]
    before = await db.get_graph_data_by_source("src_a")
    same = await publish(db, version=version, data=graph("unused"))
    assert same["unchanged"] and not same["conflict"]
    assert await db.get_graph_data_by_source("src_a") == before
    await publish(db, version="v3", expected=version, data=graph("replacement"))
    after = await db.get_graph_data_by_source("src_a")
    assert {n["id"] for n in after["nodes"]} == {"replacement"}


async def test_failed_refresh_rolls_back_deletion_and_source_version(db):
    await publish(db)
    await query(db, "CREATE CONSTRAINT m1_label_unique IF NOT EXISTS "
                    "FOR (n:Entity) REQUIRE n.summary IS UNIQUE")
    try:
        bad = {"nodes": [{"id": "bad1", "label": "duplicate",
                           "properties": {"summary": "collision"}},
                         {"id": "bad2", "label": "duplicate",
                           "properties": {"summary": "collision"}}], "edges": []}
        with pytest.raises(ConstraintError):
            await publish(db, version="v2", expected="v1", data=bad)
        assert (await db.get_source("src_a"))["version"] == "v1"
        assert {n["id"] for n in (await db.get_graph_data_by_source("src_a"))["nodes"]} == {
            "src_av1"}
    finally:
        await query(db, "DROP CONSTRAINT m1_label_unique IF EXISTS")


async def test_deleted_source_rejects_stale_refresh_and_supports_deliberate_reingestion(db):
    await publish(db)
    await db.delete_source("src_a")
    assert await db.get_sources() == []
    assert (await publish(db, version="v2", expected="v1"))["conflict"]
    tombstone = await db.get_source("src_a")
    assert (await publish(db, version="v2", expected=tombstone["version"]))["nodes_written"] == 1


async def test_clear_invalidates_initial_extraction_without_published_version(db):
    assert await db.get_source("unpublished") is None
    await db.clear_graph()
    assert (await publish(db, sid="unpublished"))["conflict"]
    assert await db.get_sources() == []


async def test_context_includes_traversed_node_and_edge_provenance_and_bounds(db):
    await publish(db, sid="a", data=graph("a", "center summary"))
    await publish(db, sid="b", data=graph("b", "middle summary"))
    await publish(db, sid="c", data=graph("c", "outer summary"))
    await db.write_graph({"nodes": [], "edges": [
        {"source": "a", "target": "b", "relationship": "LINK"},
        {"source": "b", "target": "c", "relationship": "LINK"}]}, source_doc="edge_only")
    context = await db.get_entity_context("a", depth=2)
    assert set(context["source_docs"]) == {"a", "b", "c", "edge_only"}
    assert set(context["entity_ids"]) == {"a", "b", "c"}
    assert "middle summary" in context["text"] and "outer summary" in context["text"]
    nodes = [{"id": f"wide{i}", "label": f"Node {i}"} for i in range(80)]
    await db.write_graph({"nodes": nodes, "edges": [
        {"source": "a", "target": n["id"], "relationship": "WIDE"} for n in nodes]}, "wide")
    bounded = await db.get_entity_context("a", depth=4)
    assert len(bounded["edges"]) <= 50
    assert len(bounded["nodes"]) <= 51


async def test_merge_keeps_assertions_and_deletion_invalidates_ambiguous_summary(db):
    await publish(db, sid="a", data=graph("a", "private source a summary"))
    await publish(db, sid="b", data=graph("b", "source b"))
    await publish(db, sid="shared", data=graph("shared"))
    for sid in ("a", "b"):
        await db.write_graph({"nodes": [], "edges": [
            {"source": sid, "target": "shared", "relationship": "LINK"}]}, sid)
    await db.merge_nodes(["a", "b"], canonical_id="a")
    rows = await query(db, "MATCH (:Entity {id:'a'})-[r]->() RETURN r.source_doc AS source")
    assert {r["source"] for r in rows} == {"a", "b"}
    await db.set_node_embeddings([dict(id="a", embedding=[0.1] * 384, method="nosana",
                                      **configured_embedding_space())])
    await db.delete_source("a")
    context = await db.get_entity_context("a")
    assert "private source a summary" not in context["text"]
    assert context["nodes"][0]["summary"] == ""
    assert "a" not in context["source_docs"]
    rows = await query(db, "MATCH (n:Entity {id:'a'}) RETURN n:SemanticEntity AS semantic")
    assert rows == [{"semantic": False}]
    assert len((await db.get_graph_data_by_source("b"))["edges"]) == 1


async def test_mixed_embedding_batch_and_startup_quarantine(db):
    for sid in ("real", "hash", "legacy", "stale", "bad"):
        await publish(db, sid=sid, data=graph(sid))
    space = configured_embedding_space()
    items = [dict(id="real", embedding=[0.1] * 384, method="nosana", **space),
             dict(id="hash", embedding=[0.1] * 384, method="local_hash_fallback", **space),
             dict(id="legacy", embedding=[0.1] * 384, method="nosana"),
             dict(id="stale", embedding=[0.1] * 384, method="nosana",
                  **(space | {"version": "old"})),
             dict(id="bad", embedding=[0.0] * 384, method="nosana", **space)]
    await db.set_node_embeddings(items)
    rows = await query(db, "MATCH (n:SemanticEntity) RETURN n.id AS id")
    assert rows == [{"id": "real"}]
    await query(db, "MATCH (n:Entity {id:'stale'}) SET n:SemanticEntity, "
                    "n.semantic_embedding = n.embedding")
    await db.init_vector_index()
    rows = await query(db, "MATCH (n:SemanticEntity) RETURN n.id AS id")
    assert rows == [{"id": "real"}]
    await query(db, "CALL db.awaitIndexes(30)")
    # Vector index refresh is eventually consistent. A fresh session follows committed writes.
    results = await db.vector_search([0.1] * 384)
    assert {r["id"] for r in results} <= {"real"}
    assert await db.vector_search([0.1] * 384, version="old") == []


async def test_legacy_migration_is_explicit_idempotent_and_reversible(db):
    legacy = "Legacy title " + uuid.uuid4().hex
    await db.write_graph(graph("legacy"), legacy)
    assert not await query(db, "MATCH (s:Source {id:$id}) RETURN s", id=legacy)
    dry = await migrate_sources(db)
    assert legacy in dry["would_create"]
    assert not await query(db, "MATCH (s:Source {id:$id}) RETURN s", id=legacy)
    assert (await migrate_sources(db, apply=True))["created"] == 1
    assert (await migrate_sources(db, apply=True))["created"] == 0
    assert (await db.get_source(legacy))["kind"] == "legacy"
    await query(db, "CREATE (:Source {id:'preexisting', title:'Existing', kind:'legacy'})")
    assert legacy in (await rollback_sources(db))["would_remove"]
    assert (await rollback_sources(db, apply=True))["removed"] == 1
    assert (await db.get_graph_data_by_source(legacy))["nodes"][0]["id"] == "legacy"
    assert (await db.get_source("preexisting"))["title"] == "Existing"
    await migrate_sources(db, apply=True)
    await query(db, "MATCH (s:Source {id:$id}) SET s.title = 'Edited'", id=legacy)
    assert (await rollback_sources(db, apply=True))["removed"] == 0


async def test_stale_embedding_result_cannot_restore_deleted_source_evidence(db):
    await publish(db, sid="a", data=graph("a", "secret summary from a"))
    await publish(db, sid="b", data=graph("b", "b"))
    await db.merge_nodes(["a", "b"], canonical_id="a")
    before = (await db.get_entity_context("a"))["nodes"][0]
    stale = dict(id="a", embedding=[0.1] * 384, method="nosana",
                 expected_summary=before["summary"], expected_label=before["label"],
                 **configured_embedding_space())
    await db.delete_source("a")
    assert await db.set_node_embeddings([stale]) == 0
    assert await query(db, "MATCH (n:SemanticEntity) RETURN n.id AS id") == []


async def test_stale_worker_cannot_write_or_query_retired_embedding_space(db, monkeypatch):
    await publish(db, data=graph("node"))
    old = configured_embedding_space()
    monkeypatch.setenv("NOSANA_EMBEDDING_VERSION", "rotated-test-version")
    await db.init_vector_index()
    monkeypatch.setenv("NOSANA_EMBEDDING_VERSION", old["version"])
    assert await db.set_node_embeddings([
        dict(id="node", embedding=[0.1] * 384, method="nosana", **old)]) == 0
    assert await db.vector_search([0.1] * 384) == []
    assert await query(db, "MATCH (n:SemanticEntity) RETURN n.id AS id") == []


async def test_delete_serializes_against_racing_refresh(db):
    await publish(db)
    await asyncio.gather(publish(db, version="v2", expected="v1"), db.delete_source("src_a"))
    assert await db.get_sources() == []
    assert (await db.get_graph_data_by_source("src_a"))["nodes"] == []


async def test_explicit_migration_discovers_unstarted_scalar_graph_without_rekeying(db):
    await query(db, "CREATE (:Entity {id:'scalar', label:'Original', "
                    "source_doc:'Original legacy title'})")
    report = await migrate_sources(db, apply=True)
    assert "Original legacy title" in report["would_create"]
    row = (await query(db, "MATCH (n:Entity {id:'scalar'}) "
                           "RETURN n.source_doc AS source, n.source_docs AS docs"))[0]
    assert row == {"source": "Original legacy title", "docs": None}
    assert (await rollback_sources(db, apply=True))["removed"] == 1


async def test_legacy_seed_reset_revives_tombstone_and_invalidates_old_publication(db):
    await db.write_graph(graph("legacy_node"), "legacy_seed")
    await db.delete_source("legacy_seed")
    tombstone = await db.get_source("legacy_seed")
    await db.write_graph(graph("replacement_node"), "legacy_seed")
    listed = {s["source_doc"]: s for s in await db.get_sources()}
    assert listed["legacy_seed"]["kind"] == "legacy"
    assert listed["legacy_seed"]["node_count"] == 1
    assert listed["legacy_seed"]["version"] != tombstone["version"]
    assert (await publish(db, sid="legacy_seed", expected=tombstone["version"]))["conflict"]
    await db.clear_graph()
    await db.upsert_node({"id": "again", "label": "Again"}, "legacy_seed")
    assert (await db.get_sources())[0]["source_doc"] == "legacy_seed"
    assert (await db.get_source("legacy_seed"))["kind"] == "legacy"


async def test_merge_invalidates_vector_before_optional_reembedding(db):
    await publish(db, sid="a", data=graph("a", "old"))
    await publish(db, sid="b", data=graph("b", "much longer winning summary"))
    await db.set_node_embeddings([dict(id="a", embedding=[0.1] * 384, method="nosana",
                                      **configured_embedding_space())])
    await db.merge_nodes(["a", "b"], canonical_id="a")
    row = (await query(db, "MATCH (n:Entity {id:'a'}) RETURN n.summary AS summary, "
                           "n:SemanticEntity AS semantic, n.embedding AS embedding, "
                           "n.semantic_embedding AS indexed"))[0]
    assert row == {"summary": "much longer winning summary", "semantic": False,
                   "embedding": None, "indexed": None}


@pytest.mark.parametrize("method", ["write_graph", "upsert_node"])
async def test_legacy_text_rewrite_invalidates_semantic_vector(db, method):
    await publish(db, data=graph("node", "Old text"))
    await db.set_node_embeddings([dict(id="node", embedding=[0.1] * 384, method="nosana",
                                      **configured_embedding_space())])
    replacement = graph("node", "Replacement text")
    if method == "write_graph":
        await db.write_graph(replacement, "src_a")
    else:
        await db.upsert_node(replacement["nodes"][0], "src_a")
    row = (await query(db, "MATCH (n:Entity {id:'node'}) RETURN n.summary AS summary, "
                           "n:SemanticEntity AS semantic, n.embedding AS embedding, "
                           "n.semantic_embedding AS indexed"))[0]
    assert row == {"summary": "Replacement text", "semantic": False,
                   "embedding": None, "indexed": None}
