"""Integration tests against a real Neo4j instance.

Skipped unless NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD are set in the
environment. Run explicitly with: pytest -m integration
"""
from __future__ import annotations

import os
import uuid

import pytest

from src.graph.neo4j_client import Neo4jClient

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD),
        reason="NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD not set — no live Neo4j to test against",
    ),
]


@pytest.fixture
async def neo4j():
    client = Neo4jClient(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    await client.connect()
    await client.init_schema()
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
def source_doc():
    return f"test-source-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_write_graph_and_read_back(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [
            {"id": f"{source_doc}_a", "label": "Alice", "type": "Person",
             "properties": {"summary": "a person"}},
            {"id": f"{source_doc}_b", "label": "Acme", "type": "Org",
             "properties": {"summary": "a company"}},
        ],
        "edges": [
            {"source": f"{source_doc}_a", "target": f"{source_doc}_b",
             "relationship": "WORKS_AT", "properties": {"context": "since 2020"}},
        ],
    }

    try:
        write_result = await neo4j.write_graph(graph_data, source_doc=source_doc)
        assert write_result["nodes_written"] == 2
        assert write_result["edges_written"] == 1

        data = await neo4j.get_graph_data_by_source(source_doc)
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["type"] == "WORKS_AT"
        # Nodes report membership via source_docs (list), not a scalar source_doc.
        for node in data["nodes"]:
            assert "source_doc" not in node
            assert source_doc in node["source_docs"]
    finally:
        await neo4j.delete_source(source_doc)


@pytest.mark.asyncio
async def test_vector_search_does_not_raise(neo4j) -> None:
    results = await neo4j.vector_search([0.1] * 384, limit=5)
    assert isinstance(results, list)


@pytest.mark.asyncio
async def test_sources_delete_and_clear_round_trip(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [
            {"id": f"{source_doc}_x", "label": "X", "type": "Concept", "properties": {}},
        ],
        "edges": [],
    }
    await neo4j.write_graph(graph_data, source_doc=source_doc)

    sources = await neo4j.get_sources()
    assert any(s["source_doc"] == source_doc for s in sources)

    delete_result = await neo4j.delete_source(source_doc)
    assert delete_result["deleted_nodes"] == 1

    sources_after = await neo4j.get_sources()
    assert not any(s["source_doc"] == source_doc for s in sources_after)


@pytest.mark.asyncio
async def test_clear_graph_removes_everything(neo4j, source_doc) -> None:
    graph_data = {
        "nodes": [{"id": f"{source_doc}_z", "label": "Z", "type": "Concept", "properties": {}}],
        "edges": [],
    }
    await neo4j.write_graph(graph_data, source_doc=source_doc)

    result = await neo4j.clear_graph()
    assert result["deleted_nodes"] >= 1

    stats = await neo4j.get_stats()
    assert stats["nodes"] == 0


@pytest.mark.asyncio
async def test_set_node_embeddings_then_vector_search_finds_node(neo4j, source_doc) -> None:
    node_id = f"{source_doc}_embed"
    graph_data = {
        "nodes": [{"id": node_id, "label": "EmbeddedNode", "type": "Concept", "properties": {}}],
        "edges": [],
    }
    try:
        await neo4j.write_graph(graph_data, source_doc=source_doc)

        embedding = [0.05] * 384
        await neo4j.set_node_embeddings(
            [{"id": node_id, "embedding": embedding}], method="nosana"
        )

        results = await neo4j.vector_search(embedding, limit=5)
        ids = [r["id"] for r in results]
        assert node_id in ids
    finally:
        await neo4j.delete_source(source_doc)


# ------------------------------------------------------------------
# Migration: legacy scalar source_doc -> source_docs list, idempotently
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_init_schema_migrates_legacy_scalar_source_doc(neo4j, source_doc) -> None:
    """A pre-migration node has a scalar `source_doc` and no `norm_label`.

    `init_schema` must backfill both, drop the scalar property, and be a
    no-op the second time it runs.
    """
    node_id = f"{source_doc}_legacy"
    try:
        async with neo4j.driver.session() as session:
            await session.run(
                "CREATE (n:Entity {id: $id, label: $label, type: 'Concept', "
                "summary: '', source_doc: $source_doc})",
                id=node_id,
                label="Legacy Node",
                source_doc=source_doc,
            )

        async def _read():
            async with neo4j.driver.session() as session:
                result = await session.run(
                    "MATCH (n:Entity {id: $id}) RETURN n.source_docs AS source_docs, "
                    "n.source_doc AS source_doc, n.norm_label AS norm_label",
                    id=node_id,
                )
                return await result.single()

        await neo4j.init_schema()
        record = await _read()
        assert record["source_docs"] == [source_doc]
        assert record["source_doc"] is None
        assert record["norm_label"] == "legacy node"

        # Idempotent: running init_schema again must not change anything.
        await neo4j.init_schema()
        record2 = await _read()
        assert record2["source_docs"] == [source_doc]
        assert record2["source_doc"] is None
        assert record2["norm_label"] == "legacy node"
    finally:
        await neo4j.delete_source(source_doc)


# ------------------------------------------------------------------
# Merge round trip
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_merge_round_trip(neo4j) -> None:
    """Two same-normalized-label nodes from different docs, each with its own
    in/out edges plus a shared neighbor and a cross-duplicate edge, merged
    into one canonical node.
    """
    doc_a = f"test-source-{uuid.uuid4().hex[:8]}"
    doc_b = f"test-source-{uuid.uuid4().hex[:8]}"

    a1 = f"{doc_a}_dup"
    a2 = f"{doc_b}_dup"
    ext_a = f"{doc_a}_extA"
    ext_b = f"{doc_b}_extB"
    ext_shared = f"{doc_a}_shared"

    short_summary = "short summary"
    long_summary = "a substantially longer summary that should win the merge round trip"

    try:
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": a1, "label": "Kimi AI", "type": "Org",
                     "properties": {"summary": short_summary}},
                    {"id": ext_a, "label": "External A", "type": "Concept", "properties": {}},
                    {"id": ext_shared, "label": "Shared", "type": "Concept", "properties": {}},
                ],
                "edges": [],
            },
            source_doc=doc_a,
        )
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": a2, "label": "KIMI, ai!", "type": "Org",
                     "properties": {"summary": long_summary}},
                    {"id": ext_b, "label": "External B", "type": "Concept", "properties": {}},
                ],
                "edges": [],
            },
            source_doc=doc_b,
        )
        await neo4j.write_graph(
            {
                "nodes": [],
                "edges": [
                    {"source": a1, "target": ext_a, "relationship": "WORKS_AT",
                     "properties": {"context": "since 2020"}},
                    {"source": a1, "target": ext_shared, "relationship": "WORKS_AT",
                     "properties": {"context": "shared-a"}},
                    # Cross-duplicate edge: should become a self-loop on the
                    # canonical node after merge, and be dropped.
                    {"source": a1, "target": a2, "relationship": "LIKES", "properties": {}},
                ],
            },
            source_doc=doc_a,
        )
        await neo4j.write_graph(
            {
                "nodes": [],
                "edges": [
                    {"source": ext_b, "target": a2, "relationship": "KNOWS",
                     "properties": {"context": "friends"}},
                    # Same (endpoint, type) as a1->ext_shared once repointed —
                    # should dedupe to a single edge after merge.
                    {"source": a2, "target": ext_shared, "relationship": "WORKS_AT",
                     "properties": {"context": "shared-b"}},
                ],
            },
            source_doc=doc_b,
        )

        res = await neo4j.merge_nodes([a1, a2])
        assert res["ok"] is True

        canonical_id = res["canonical_id"]
        # Longest label wins when canonical is unspecified: "KIMI, ai!" (9
        # chars) beats "Kimi AI" (7 chars).
        assert canonical_id == a2
        assert a1 in res["removed_ids"]
        assert set(res["canonical"]["source_docs"]) == {doc_a, doc_b}
        assert "Kimi AI" in res["canonical"]["aliases"]
        assert res["canonical"]["summary"] == long_summary

        # The losing duplicate is gone.
        async with neo4j.driver.session() as session:
            result = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=a1
            )
            record = await result.single()
        assert record["c"] == 0

        async with neo4j.driver.session() as session:
            result = await session.run(
                """
                MATCH (c:Entity {id: $canonical_id})-[r]-(other)
                RETURN startNode(r).id AS src, endNode(r).id AS tgt,
                       coalesce(r.type, type(r)) AS rel_type, r.source_doc AS source_doc
                """,
                canonical_id=canonical_id,
            )
            edges = [dict(rec) async for rec in result]

        # No self-loops survive the repoint.
        assert all(e["src"] != e["tgt"] for e in edges)

        # a1's unambiguous edge repointed, preserving its original source_doc.
        works_at_to_a = [e for e in edges if e["tgt"] == ext_a and e["rel_type"] == "WORKS_AT"]
        assert len(works_at_to_a) == 1
        assert works_at_to_a[0]["source_doc"] == doc_a

        # a2's unambiguous edge is untouched, preserving its source_doc.
        knows_from_b = [e for e in edges if e["src"] == ext_b and e["rel_type"] == "KNOWS"]
        assert len(knows_from_b) == 1
        assert knows_from_b[0]["source_doc"] == doc_b

        # (canonical, ext_shared, WORKS_AT) existed on both sides pre-merge —
        # deduped to a single edge.
        works_at_to_shared = [
            e for e in edges if e["tgt"] == ext_shared and e["rel_type"] == "WORKS_AT"
        ]
        assert len(works_at_to_shared) == 1
    finally:
        await neo4j.delete_source(doc_a)
        await neo4j.delete_source(doc_b)


# ------------------------------------------------------------------
# delete_source: membership semantics for nodes shared across docs
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_delete_source_membership_semantics(neo4j) -> None:
    doc_a = f"test-source-{uuid.uuid4().hex[:8]}"
    doc_b = f"test-source-{uuid.uuid4().hex[:8]}"
    shared_id = f"shared-{uuid.uuid4().hex[:8]}"
    only_a_id = f"{doc_a}_only"

    try:
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": shared_id, "label": "Shared", "type": "Concept", "properties": {}},
                    {"id": only_a_id, "label": "OnlyA", "type": "Concept", "properties": {}},
                ],
                "edges": [
                    {"source": only_a_id, "target": shared_id, "relationship": "RELATED",
                     "properties": {}},
                ],
            },
            source_doc=doc_a,
        )
        # Same node id written again under doc_b — simulates a shared entity
        # (e.g. produced by a merge) that belongs to two documents.
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": shared_id, "label": "Shared", "type": "Concept", "properties": {}},
                ],
                "edges": [],
            },
            source_doc=doc_b,
        )

        result_a = await neo4j.delete_source(doc_a)
        assert result_a["removed_memberships"] >= 1
        assert result_a["deleted_edges"] >= 1

        async with neo4j.driver.session() as session:
            res = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=shared_id
            )
            record = await res.single()
        assert record["c"] == 1  # shared node survives doc_a's deletion

        async with neo4j.driver.session() as session:
            res = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=only_a_id
            )
            record = await res.single()
        assert record["c"] == 0  # node with no remaining membership is gone

        result_b = await neo4j.delete_source(doc_b)
        assert result_b["deleted_nodes"] == 1

        async with neo4j.driver.session() as session:
            res = await session.run(
                "MATCH (n:Entity {id: $id}) RETURN count(n) AS c", id=shared_id
            )
            record = await res.single()
        assert record["c"] == 0
    finally:
        # Idempotent no-op cleanup regardless of where the assertions above landed.
        await neo4j.delete_source(doc_a)
        await neo4j.delete_source(doc_b)


# ------------------------------------------------------------------
# Boundary-node invariant after a cross-doc merge
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_boundary_nodes_after_cross_doc_merge(neo4j) -> None:
    doc_a = f"test-source-{uuid.uuid4().hex[:8]}"
    doc_b = f"test-source-{uuid.uuid4().hex[:8]}"

    a1 = f"{doc_a}_dup"
    a2 = f"{doc_b}_dup"
    ext_a = f"{doc_a}_ext"

    try:
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": a1, "label": "Kimi AI", "type": "Org", "properties": {}},
                    {"id": ext_a, "label": "External", "type": "Concept", "properties": {}},
                ],
                "edges": [
                    {"source": a1, "target": ext_a, "relationship": "WORKS_AT",
                     "properties": {}},
                ],
            },
            source_doc=doc_a,
        )
        await neo4j.write_graph(
            {
                "nodes": [{"id": a2, "label": "KIMI AI!", "type": "Org", "properties": {}}],
                "edges": [],
            },
            source_doc=doc_b,
        )

        res = await neo4j.merge_nodes([a1, a2])
        assert res["ok"] is True

        # Whichever side won the merge, every edge endpoint returned for a
        # source-scoped read must itself be present in that same read's node
        # list — including the merged-away boundary neighbor.
        for doc in (doc_a, doc_b):
            data = await neo4j.get_graph_data_by_source(doc)
            node_ids = {n["id"] for n in data["nodes"]}
            for edge in data["edges"]:
                assert edge["source"] in node_ids
                assert edge["target"] in node_ids
    finally:
        await neo4j.delete_source(doc_a)
        await neo4j.delete_source(doc_b)


# ------------------------------------------------------------------
# suggest_similar: disabled without >=2 nosana-embedded nodes
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_suggest_similar_disabled_without_nosana_embeddings(neo4j, source_doc) -> None:
    node_id = f"{source_doc}_a"
    try:
        await neo4j.write_graph(
            {
                "nodes": [
                    {"id": node_id, "label": "NoEmbedding", "type": "Concept", "properties": {}}
                ],
                "edges": [],
            },
            source_doc=source_doc,
        )
        # No embeddings set at all here, so — assuming a clean database —
        # fewer than 2 nodes carry embedding_method == 'nosana'.
        result = await neo4j.suggest_similar()
        assert result["enabled"] is False
        assert result["reason"]
        assert result["pairs"] == []
    finally:
        await neo4j.delete_source(source_doc)
