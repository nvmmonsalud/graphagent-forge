"""M2 atomic provenance regressions; opt in only against a disposable Neo4j."""
from __future__ import annotations

import asyncio
import os

import pytest
from neo4j.exceptions import ConstraintError

from src.graph.neo4j_client import Neo4jClient
from src.ingestion.chunks import plan_chunks

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("M2_DISPOSABLE_NEO4J") != "1", reason="Requires M2_DISPOSABLE_NEO4J=1")]


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


def source(sid="s", version="v1"):
    return {"id": sid, "version": version, "title": "Same title", "kind": "text",
            "uri": "", "coverage": {"partial": False, "processed_chars": 123}}


def graph(sid="s", version="v1", content="Original 日本語🙂 evidence", nodes=None):
    chunks = plan_chunks(content, sid, version)
    return {"nodes": [{"id": nid, "label": nid, "properties": {"summary": nid},
                       "evidence_ids": [chunks[0]["id"]]} for nid in (nodes or [])],
            "edges": [], "chunks": chunks}


async def publish(db, sid="s", version="v1", expected=None, data=None):
    return await db.publish_source_graph(data if data is not None else graph(sid, version),
                                         source=source(sid, version), expected_version=expected)


async def query(db, text, **kwargs):
    async with db.driver.session() as session:
        result = await session.run(text, **kwargs)
        return [dict(r) async for r in result]


async def test_unicode_offsets_coverage_and_empty_entity_source_roundtrip(db):
    content = "é🙂日本語" * 2000
    data = graph(content=content)
    await publish(db, data=data)
    chunks = await db.get_source_evidence("s")
    assert len(chunks) == 2
    for chunk in chunks:
        assert content[chunk["start"]:chunk["end"]] == chunk["text"]
        assert chunk["version"] == "v1" and chunk["title"] == "Same title"
        assert await db.get_evidence(chunk["id"]) == chunk
    assert (await db.get_source("s"))["coverage"] == source()["coverage"]
    assert (await db.get_sources())[0]["coverage"] == source()["coverage"]
    assert await db.validate_evidence_ids(["ev_unknown"]) == []


async def test_long_document_fact_after_50000_is_retrieved_without_entities(db):
    content = "ordinary background " * 3300 + "\nZirconium launchcode is marmalade.\n"
    data = graph(content=content)
    await publish(db, data=data)
    found = await db.search_evidence("Zirconium launchcode")
    assert found and "marmalade" in found[0]["text"]
    assert found[0]["start"] > 50000
    assert content[found[0]["start"]:found[0]["end"]] == found[0]["text"]


async def test_lexical_match_ranks_ahead_of_entity_link(db):
    await publish(db, data=graph(content="linked unrelated background", nodes=["n"]))
    other = graph("other", content="specific marmalade launchcode")
    await publish(db, sid="other", data=other)
    results = await db.search_evidence("marmalade", entity_ids=["n"])
    assert [r["source_id"] for r in results] == ["other", "s"]


async def test_refresh_delete_clear_reject_all_stale_ids(db):
    original = graph(nodes=["old"])
    await publish(db, data=original)
    old = original["chunks"][0]["id"]
    replacement = graph(version="v2", nodes=["new"])
    await publish(db, version="v2", expected="v1", data=replacement)
    assert await db.get_evidence(old) is None
    current = replacement["chunks"][0]["id"]
    assert await db.get_evidence(current)
    await db.delete_source("s")
    assert await db.get_evidence(current) is None
    tombstone = await db.get_source("s")
    replacement = graph(version="v3")
    await publish(db, version="v3", expected=tombstone["version"], data=replacement)
    await db.clear_graph()
    assert await db.get_source_evidence("s") == []
    assert await db.get_evidence(replacement["chunks"][0]["id"]) is None
    assert await query(db, "MATCH (c:EvidenceChunk) RETURN count(c) AS n") == [{"n": 0}]


async def test_failed_refresh_preserves_old_chunks_and_metadata(db):
    original = graph(nodes=["old"])
    await publish(db, data=original)
    await query(db, "CREATE CONSTRAINT m2_summary_unique IF NOT EXISTS "
                    "FOR (n:Entity) REQUIRE n.summary IS UNIQUE")
    try:
        bad = graph(version="v2", nodes=["a", "b"])
        for node in bad["nodes"]:
            node["properties"]["summary"] = "collision"
        with pytest.raises(ConstraintError):
            await publish(db, version="v2", expected="v1", data=bad)
        assert (await db.get_source("s"))["version"] == "v1"
        assert await db.get_evidence(original["chunks"][0]["id"])
        assert await db.get_evidence(bad["chunks"][0]["id"]) is None
    finally:
        await query(db, "DROP CONSTRAINT m2_summary_unique IF EXISTS")


async def test_concurrent_refresh_winner_has_only_its_current_chunks(db):
    await publish(db)
    results = await asyncio.gather(
        publish(db, version="v2", expected="v1", data=graph(version="v2")),
        publish(db, version="v3", expected="v1", data=graph(version="v3")))
    assert sum(r["conflict"] for r in results) == 1
    current = await db.get_source("s")
    chunks = await db.get_source_evidence("s")
    assert len(chunks) == 1 and chunks[0]["version"] == current["version"]
    assert await query(db, "MATCH (c:EvidenceChunk) RETURN count(c) AS n") == [{"n": 1}]


@pytest.mark.parametrize("write", ["node", "edge", "graph"])
async def test_legacy_direct_write_invalidates_excerpts_and_references(db, write):
    data = graph(nodes=["a", "b"])
    ev = data["chunks"][0]["id"]
    data["edges"] = [{"source": "a", "target": "b", "evidence_ids": [ev]}]
    await publish(db, data=data)
    if write == "node":
        await db.upsert_node({"id": "a", "label": "changed"}, source_doc="s")
    elif write == "edge":
        await db.upsert_edge({"source": "a", "target": "b"}, source_doc="s")
    else:
        await db.write_graph({"nodes": [{"id": "a", "label": "changed"}], "edges": []},
                             source_doc="s")
    assert await db.get_evidence(ev) is None
    assert await db.search_evidence("evidence") == []
    refs = await query(db, "MATCH (n:Entity) RETURN coalesce(n.evidence_ids, []) AS ids")
    assert all(ev not in row["ids"] for row in refs)


async def test_merge_unions_node_and_collapsed_edge_evidence(db):
    data = graph(content="a" * 8000 + "b" * 3000, nodes=["a", "b", "z"])
    first, second = [c["id"] for c in data["chunks"]]
    data["nodes"][1]["evidence_ids"] = [second]
    data["edges"] = [{"source": a, "target": b, "evidence_ids": [ev]}
                     for a, b, ev in [("a", "z", first), ("b", "z", second),
                                      ("z", "a", first), ("z", "b", second)]]
    await publish(db, data=data)
    assert (await db.merge_nodes(["a", "b"], canonical_id="a"))["ok"]
    nodes = await query(db, "MATCH (n:Entity {id:'a'}) RETURN n.evidence_ids AS ids")
    assert set(nodes[0]["ids"]) == {first, second}
    edges = await query(db, "MATCH ()-[r:RELATES_TO]->() RETURN r.evidence_ids AS ids")
    assert len(edges) == 2 and all(set(r["ids"]) == {first, second} for r in edges)


async def test_deleting_one_merged_source_preserves_other_assertions_and_refs(db):
    left = graph(nodes=["a", "z"])
    right = graph("other", nodes=["b"])
    left_ev, right_ev = left["chunks"][0]["id"], right["chunks"][0]["id"]
    await publish(db, data=left)
    await publish(db, sid="other", data=right)
    await db.merge_nodes(["a", "b"], canonical_id="a")
    # Other source assertion retaining boundary entity when its origin is deleted.
    await query(db, "MATCH (a:Entity {id:'a'}),(z:Entity {id:'z'}) "
                    "CREATE (a)-[:RELATES_TO {source_doc:'other', evidence_ids:$ids}]->(z)",
                ids=[left_ev, right_ev])
    await db.delete_source("s")
    nodes = await query(db, "MATCH (n:Entity {id:'a'}) RETURN n.evidence_ids AS ids")
    assert nodes == [{"ids": [right_ev]}]
    edges = await query(db, "MATCH ()-[r:RELATES_TO]->() RETURN r.evidence_ids AS ids")
    assert edges == [{"ids": [right_ev]}]
    assert await db.get_evidence(left_ev) is None and await db.get_evidence(right_ev)


async def test_stale_linkage_and_tombstones_are_rejected_even_if_chunk_survives(db):
    data = graph()
    await publish(db, data=data)
    ev = data["chunks"][0]["id"]
    await query(db, "MATCH (s:Source {id:'s'}) SET s.version = 'different'")
    assert await db.get_evidence(ev) is None
    assert await db.get_source_evidence("s") == []
    assert await db.search_evidence("evidence") == []
    await query(db, "MATCH (s:Source {id:'s'}) SET s.version='v1', s._deleted=true")
    assert await db.get_evidence(ev) is None


async def test_duplicate_assertion_rows_union_evidence(db):
    data = graph(content="a" * 8000 + "b" * 3000, nodes=["a", "z"])
    ids = [c["id"] for c in data["chunks"]]
    data["edges"] = [{"source": "a", "target": "z", "evidence_ids": [ev]} for ev in ids]
    await publish(db, data=data)
    rows = await query(db, "MATCH ()-[r:RELATES_TO]->() RETURN r.evidence_ids AS ids")
    assert len(rows) == 1 and set(rows[0]["ids"]) == set(ids)


async def test_evidence_read_waits_for_atomic_delete_lock(db):
    data = graph()
    await publish(db, data=data)
    ev = data["chunks"][0]["id"]
    async with db.driver.session() as session:
        tx = await session.begin_transaction()
        await db._lock_graph(tx)
        await db._remove_source_graph(tx, "s")
        read = asyncio.create_task(db.get_evidence(ev))
        # Yield to schedule the reader; it cannot observe uncommitted deletion.
        await asyncio.sleep(0)
        await tx.run("MATCH (s:Source {id:'s'}) SET s.version='deleted', s._deleted=true")
        await tx.commit()
        assert await read is None


# Declare relevance independently of retrieval output. This intentionally small,
# synthetic corpus exercises actual Cypher ranking; it is not a general benchmark.
_RETRIEVAL_CORPUS = {
    "quality_long": "Routine archival background. " * 2200
                    + "\nThe heliotrope launchcode is marmalade.\n",
    "quality_unicode": "東京研究所の開館時刻は午前九時です。",
    "quality_conflict_a": "The kestrel program launch date is 2030-04-12.",
    "quality_conflict_b": "The kestrel program launch date is 2030-05-20.",
    "quality_budget": "The obsidian laboratory annual budget is 42 million euros.",
    "quality_retention": "The amethyst archive retention period is seven years.",
    "quality_distractor_a": "The cafeteria lunch menu includes marmalade toast.",
    "quality_distractor_b": "The unrelated garnet program has a budget and a launch date.",
}
_RETRIEVAL_EXPECTATIONS = (
    ("after_50000", "What is the heliotrope launchcode?", {"quality_long"}),
    ("unicode", "東京研究所 開館時刻", {"quality_unicode"}),
    ("conflicting_sources", "What is the kestrel program launch date?",
     {"quality_conflict_a", "quality_conflict_b"}),
    ("budget", "What is the obsidian laboratory annual budget?", {"quality_budget"}),
    ("retention", "What is the amethyst archive retention period?", {"quality_retention"}),
)


async def test_fixed_corpus_actual_neo4j_lexical_source_recall_at_three(db):
    """Measure expected-source recall within three returned chunks, without an LLM.

    No entity matches or mocked candidates are supplied. This measures candidate
    source retrieval on these five fixtures only, not entailment, answer quality,
    conflict detection, semantic retrieval, or performance on real user questions.
    """
    for sid, content in _RETRIEVAL_CORPUS.items():
        await publish(db, sid=sid, data=graph(sid, content=content))

    measurements = []
    for label, question, expected_sources in _RETRIEVAL_EXPECTATIONS:
        candidates = await db.search_evidence(question, entity_ids=[], limit=3)
        assert len(candidates) <= 3
        retrieved_sources = {chunk["source_id"] for chunk in candidates}
        recalled = len(expected_sources & retrieved_sources)
        recall = recalled / len(expected_sources)
        measurements.append(recall)
        print(f"{label}: expected-source recall@3={recall:.3f} "
              f"({recalled}/{len(expected_sources)}); retrieved={sorted(retrieved_sources)}")
        assert expected_sources <= retrieved_sources, label
        for chunk in candidates:
            content = _RETRIEVAL_CORPUS[chunk["source_id"]]
            assert content[chunk["start"]:chunk["end"]] == chunk["text"]
        if label == "after_50000":
            relevant = [chunk for chunk in candidates if chunk["source_id"] == "quality_long"]
            assert any(chunk["start"] > 50000 and "heliotrope" in chunk["text"]
                       for chunk in relevant)
    macro_recall = sum(measurements) / len(measurements)
    print(f"Fixed synthetic Neo4j lexical fixture: macro expected-source recall@3="
          f"{macro_recall:.3f} across {len(measurements)} questions; "
          "no entailment or generalization claim.")
    assert macro_recall == 1.0
