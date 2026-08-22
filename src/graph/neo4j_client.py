"""Neo4j graph client — connection, schema, and queries."""
from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncGraphDatabase

from src.graph.normalize import normalize_label

log = logging.getLogger(__name__)

# Node provenance is a LIST property (`source_docs`) — the same entity can be
# asserted by several documents. Edges keep a SCALAR `r.source_doc` because an
# edge is a per-document assertion.
_NODE_FIELDS = (
    "n.id AS id, n.label AS label, n.type AS type, "
    "n.summary AS summary, n.source_docs AS source_docs, n.aliases AS aliases"
)

# Shared map projection for candidate/merge payloads.
_NODE_PROJECTION = "{.id, .label, .type, .summary, .source_docs}"

_MIGRATE_BATCH = 500


class Neo4jClient:
    """Async Neo4j driver wrapper."""

    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password
        self.driver = None

    async def connect(self):
        self.driver = AsyncGraphDatabase.driver(self.uri, auth=(self.user, self.password))
        await self.driver.verify_connectivity()
        log.info("Neo4j connected: %s", self.uri)

    async def close(self):
        if self.driver:
            await self.driver.close()

    # ------------------------------------------------------------------
    # Schema setup
    # ------------------------------------------------------------------
    async def init_schema(self):
        """Create indexes and constraints for performance."""
        queries = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.label)",
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.type)",
            # Legacy scalar-provenance index: kept so pre-migration graphs stay
            # queryable while `_migrate_source_docs` drains them.
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.source_doc)",
            "CREATE INDEX entity_norm_label IF NOT EXISTS FOR (n:Entity) ON (n.norm_label)",
        ]
        async with self.driver.session() as session:
            for q in queries:
                await session.run(q)

        await self._migrate_source_docs()
        await self._backfill_norm_labels()
        await self.init_vector_index()
        log.info("Neo4j schema initialized")

    async def _migrate_source_docs(self) -> None:
        """Fold the legacy scalar `n.source_doc` into the `n.source_docs` list.

        Idempotent (nodes lose `source_doc` as they are migrated, so a re-run
        matches nothing) and batched. MUST run as an auto-commit query:
        `CALL {} IN TRANSACTIONS` is rejected inside an explicit transaction.
        """
        query = f"""
        MATCH (n:Entity) WHERE n.source_doc IS NOT NULL
        CALL {{
          WITH n
          SET n.source_docs = coalesce(n.source_docs, []) + n.source_doc
          REMOVE n.source_doc
        }} IN TRANSACTIONS OF {_MIGRATE_BATCH} ROWS
        """
        async with self.driver.session() as session:
            result = await session.run(query)
            summary = await result.consume()
        migrated = summary.counters.properties_set if summary else 0
        if migrated:
            log.info("Migrated legacy source_doc → source_docs (%d property writes)", migrated)

    async def _backfill_norm_labels(self) -> None:
        """Fill `n.norm_label` for nodes written before normalization existed.

        Done in Python because punctuation stripping isn't expressible in plain
        Cypher (no APOC). Idempotent: normalized nodes are never re-selected,
        and a punctuation-only label normalizes to '' which is still non-null.
        """
        fetch = f"""
        MATCH (n:Entity)
        WHERE n.norm_label IS NULL AND n.label IS NOT NULL
        RETURN n.id AS id, n.label AS label
        LIMIT {_MIGRATE_BATCH}
        """
        write = """
        UNWIND $items AS it
        MATCH (n:Entity {id: it.id})
        SET n.norm_label = it.norm_label
        """
        total = 0
        async with self.driver.session() as session:
            while True:
                result = await session.run(fetch)
                rows = [dict(record) async for record in result]
                if not rows:
                    break
                items = [
                    {"id": r["id"], "norm_label": normalize_label(r["label"])} for r in rows
                ]
                await session.run(write, items=items)
                total += len(items)
        if total:
            log.info("Backfilled norm_label on %d nodes", total)

    async def init_vector_index(self):
        """Create a vector index on Entity.embedding for semantic similarity."""
        query = """
        CREATE VECTOR INDEX entity_embedding IF NOT EXISTS
        FOR (n:Entity) ON (n.embedding)
        OPTIONS {
          indexConfig: {
            `vector.dimensions`: 384,
            `vector.similarity_function`: 'cosine'
          }
        }
        """
        async with self.driver.session() as session:
            await session.run(query)
        log.info("Vector index created / already exists")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    async def upsert_node(self, node: dict[str, Any], source_doc: str = ""):
        """Create or update a single entity node."""
        query = """
        MERGE (n:Entity {id: $id})
        SET n.label = $label,
            n.type = $type,
            n.summary = $summary,
            n.norm_label = $norm_label,
            n.source_docs = CASE
              WHEN n.source_docs IS NULL THEN [$source_doc]
              WHEN NOT $source_doc IN n.source_docs THEN n.source_docs + $source_doc
              ELSE n.source_docs END,
            n.updated_at = datetime()
        """
        props = node.get("properties", {})
        label = node.get("label", "")
        async with self.driver.session() as session:
            await session.run(
                query,
                id=node["id"],
                label=label,
                type=node.get("type", "Unknown"),
                summary=props.get("summary", ""),
                norm_label=normalize_label(label),
                source_doc=source_doc,
            )

    async def upsert_edge(self, edge: dict[str, Any], source_doc: str = ""):
        """Create a relationship between two entities."""
        query = """
        MATCH (a:Entity {id: $source})
        MATCH (b:Entity {id: $target})
        MERGE (a)-[r:RELATES_TO {type: $rel_type}]->(b)
        SET r.context = $context,
            r.source_doc = $source_doc,
            r.updated_at = datetime()
        """
        props = edge.get("properties", {})
        async with self.driver.session() as session:
            await session.run(
                query,
                source=edge["source"],
                target=edge["target"],
                rel_type=edge.get("relationship", "RELATES_TO"),
                context=props.get("context", ""),
                source_doc=source_doc,
            )

    async def write_graph(self, graph_data: dict, source_doc: str = ""):
        """Write a complete graph (nodes + edges) from extraction output.

        Uses UNWIND for batch writes — one query per label instead of one per node.
        Both batches run in a single transaction so edges never land without
        their nodes (and a failure leaves the graph untouched).
        """
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        batch_nodes = [
            {
                "id": n["id"],
                "label": n.get("label", ""),
                "type": n.get("type", "Unknown"),
                "summary": n.get("properties", {}).get("summary", ""),
                "norm_label": normalize_label(n.get("label", "")),
            }
            for n in nodes
        ]
        batch_edges = [
            {
                "source": e["source"],
                "target": e["target"],
                "rel_type": e.get("relationship", "RELATES_TO"),
                "context": e.get("properties", {}).get("context", ""),
            }
            for e in edges
        ]

        node_query = """
        UNWIND $nodes AS node
        MERGE (n:Entity {id: node.id})
        SET n.label = node.label,
            n.type = node.type,
            n.summary = node.summary,
            n.norm_label = node.norm_label,
            n.source_docs = CASE
              WHEN n.source_docs IS NULL THEN [$source_doc]
              WHEN NOT $source_doc IN n.source_docs THEN n.source_docs + $source_doc
              ELSE n.source_docs END,
            n.updated_at = datetime()
        """
        edge_query = """
        UNWIND $edges AS edge
        MATCH (a:Entity {id: edge.source})
        MATCH (b:Entity {id: edge.target})
        MERGE (a)-[r:RELATES_TO {type: edge.rel_type}]->(b)
        SET r.context = edge.context,
            r.source_doc = $source_doc,
            r.updated_at = datetime()
        """

        if batch_nodes or batch_edges:
            async with self.driver.session() as session:
                tx = await session.begin_transaction()
                try:
                    if batch_nodes:
                        await tx.run(node_query, nodes=batch_nodes, source_doc=source_doc)
                    if batch_edges:
                        await tx.run(edge_query, edges=batch_edges, source_doc=source_doc)
                    await tx.commit()
                except Exception:
                    await tx.rollback()
                    raise

        log.info("Graph written: %d nodes, %d edges", len(nodes), len(edges))
        return {"nodes_written": len(nodes), "edges_written": len(edges)}

    # ------------------------------------------------------------------
    # Read / query operations
    # ------------------------------------------------------------------
    async def get_node_context(self, label: str, depth: int = 2) -> str:
        """Get a node and its N-hop neighborhood as text context for GraphRAG.

        The center is matched case-insensitively on `label` *or* on any merged
        alias, so questions phrased with a duplicate's wording still resolve.
        """
        # Neo4j requires a literal bound for variable-length patterns.
        # Clamp the caller-provided value before safe interpolation.
        safe_depth = max(1, min(int(depth), 4))
        query = f"""
        MATCH (n:Entity)
        WHERE toLower(n.label) = toLower($label) OR $label IN coalesce(n.aliases, [])
        MATCH path = (n)-[r*1..{safe_depth}]-(m:Entity)
        RETURN n.label AS center, n.type AS center_type, n.summary AS center_summary,
               [rel in r | coalesce(rel.type, type(rel))] AS rel_types,
               m.label AS neighbor, m.type AS neighbor_type, m.summary AS neighbor_summary
        ORDER BY length(path)
        LIMIT 50
        """

        lines = []
        async with self.driver.session() as session:
            result = await session.run(query, label=label)
            async for record in result:
                lines.append(
                    f"{record['center']} ({record['center_type']}) — "
                    f"[{', '.join(record['rel_types'])}] → "
                    f"{record['neighbor']} ({record['neighbor_type']}): "
                    f"{record['neighbor_summary']}"
                )

        return "\n".join(lines) if lines else f"No context found for '{label}'"

    async def search_nodes(self, query_text: str, limit: int = 10) -> list[dict]:
        """Full-text search across entity labels, aliases, and summaries."""
        query = f"""
        MATCH (n:Entity)
        WHERE toLower(n.label) CONTAINS toLower($q)
           OR toLower(n.summary) CONTAINS toLower($q)
           OR any(a IN coalesce(n.aliases, []) WHERE toLower(a) CONTAINS toLower($q))
        RETURN {_NODE_FIELDS}
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, q=query_text, limit=limit)
            return [dict(record) async for record in result]

    async def vector_search(self, embedding: list[float], limit: int = 10) -> list[dict]:
        """Find entities by cosine similarity against a pre-computed embedding vector."""
        query = """
        CALL db.index.vector.queryNodes('entity_embedding', $limit, $embedding)
        YIELD node, score
        RETURN node.id AS id, node.label AS label, node.type AS type,
               node.summary AS summary, node.source_docs AS source_docs,
               node.aliases AS aliases, score
        """
        async with self.driver.session() as session:
            result = await session.run(query, embedding=embedding, limit=int(limit))
            return [dict(record) async for record in result]

    async def get_stats(self) -> dict:
        """Quick graph statistics."""
        query = """
        MATCH (n:Entity)
        OPTIONAL MATCH (n)-[r]->(m)
        RETURN count(DISTINCT n) AS nodes,
               count(DISTINCT r) AS edges,
               collect(DISTINCT n.type) AS entity_types
        """
        async with self.driver.session() as session:
            result = await session.run(query)
            record = await result.single()
            return {
                "nodes": record["nodes"],
                "edges": record["edges"],
                "entity_types": record["entity_types"],
            }

    async def get_analytics(self, top: int = 10) -> dict:
        """Aggregate graph analytics that plain Cypher can always answer.

        Deliberately GDS/APOC-free: neither plugin is installed locally, and
        the deploy target (Aura) ships APOC Core but no GDS. Degree here is
        the undirected degree — a knowledge-graph edge's stored direction is
        only the order the LLM asserted the triple in.

        Structural metrics that Cypher can't express (components, PageRank,
        betweenness, clustering) are computed in Python by
        `DaytonaExecutor.analyze_graph`; this tier is the one that can never
        be unavailable while the graph responds at all.
        """
        # Neo4j takes LIMIT as a literal, so clamp-then-interpolate (as above).
        safe_top = max(1, min(int(top), 50))
        node_types_query = """
        MATCH (n:Entity)
        RETURN n.type AS type, count(*) AS count
        ORDER BY count DESC, type ASC
        """
        edge_types_query = """
        MATCH (:Entity)-[r]->(:Entity)
        RETURN coalesce(r.type, type(r)) AS type, count(*) AS count
        ORDER BY count DESC, type ASC
        """
        top_degree_query = f"""
        MATCH (n:Entity)
        WITH n, COUNT {{ (n)--() }} AS degree
        RETURN n.id AS id, n.label AS label, n.type AS type, degree
        ORDER BY degree DESC, n.id ASC
        LIMIT {safe_top}
        """
        isolated_query = """
        MATCH (n:Entity)
        WHERE COUNT { (n)--() } = 0
        RETURN count(n) AS isolated
        """
        async with self.driver.session() as session:
            node_types_result = await session.run(node_types_query)
            node_types = [dict(record) async for record in node_types_result]

            edge_types_result = await session.run(edge_types_query)
            edge_types = [dict(record) async for record in edge_types_result]

            top_degree_result = await session.run(top_degree_query)
            top_degree = [dict(record) async for record in top_degree_result]

            isolated_result = await session.run(isolated_query)
            isolated_record = await isolated_result.single()

        return {
            "node_types": node_types,
            "edge_types": edge_types,
            "top_degree": top_degree,
            "isolated_nodes": isolated_record["isolated"] if isolated_record else 0,
        }

    async def find_path(self, from_label: str, to_label: str) -> list[dict]:
        """Find shortest path between two entities using Cypher shortestPath.

        Each endpoint is resolved separately (LIMIT 1) to avoid a cartesian
        product, and the path length is bounded to keep the search cheap.
        Endpoints also resolve through merged aliases.
        """
        query = """
        MATCH (a:Entity)
        WHERE toLower(a.label) = toLower($from_label)
           OR $from_label IN coalesce(a.aliases, [])
        WITH a LIMIT 1
        MATCH (b:Entity)
        WHERE toLower(b.label) = toLower($to_label)
           OR $to_label IN coalesce(b.aliases, [])
        WITH a, b LIMIT 1
        MATCH path = shortestPath((a)-[*..6]-(b))
        RETURN [n IN nodes(path) | {
            id: n.id, label: n.label, type: n.type, summary: n.summary
        }] AS path_nodes,
        [r IN relationships(path) | {
            source: startNode(r).id,
            target: endNode(r).id,
            type: coalesce(r.type, type(r)),
            context: r.context
        }] AS path_edges
        LIMIT 1
        """
        async with self.driver.session() as session:
            result = await session.run(query, from_label=from_label, to_label=to_label)
            record = await result.single()
            if not record:
                return []

            path_nodes = record["path_nodes"]
            path_edges = record["path_edges"]

            # Build alternating node/edge list
            path = []
            for i, node in enumerate(path_nodes):
                path.append({"node": node})
                if i < len(path_edges):
                    path.append({"relationship": path_edges[i]})
            return path

    async def get_all_graph_data(self, limit: int | None = 500) -> dict:
        """Get graph data for visualization or full integrity checks.

        HARD INVARIANT: every id appearing in `edges[].source|target` is also
        present in `nodes[].id`. Capping nodes and edges independently breaks
        that — the D3 view calls `forceLink().id(d => d.id)` after clearing the
        SVG, so one dangling endpoint blanks the graph until a page reload. The
        edge query is therefore constrained to the node ids actually returned.

        Truncating EDGES is safe (it only orphans nodes, which D3 renders
        fine), so the edge cap is deliberately generous.

        Totals are counted with the identical MATCH patterns, uncapped, so the
        visualisation badge can never disagree with /graph/stats.
        """
        safe_limit = max(1, min(int(limit), 5000)) if limit is not None else None
        # Neo4j takes LIMIT as a literal, so clamp-then-interpolate (as above).
        node_limit = f"LIMIT {safe_limit}" if safe_limit is not None else ""
        edge_limit = f"LIMIT {safe_limit * 10}" if safe_limit is not None else ""
        nodes_query = f"""
        MATCH (n:Entity)
        WITH n ORDER BY n.updated_at DESC, n.id ASC
        {node_limit}
        RETURN {_NODE_FIELDS}
        """
        edges_query = f"""
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS source, b.id AS target, coalesce(r.type, type(r)) AS type
        {edge_limit}
        """
        async with self.driver.session() as session:
            nodes_result = await session.run(nodes_query)
            nodes = [dict(record) async for record in nodes_result]

            ids = [n["id"] for n in nodes]
            edges_result = await session.run(edges_query, ids=ids)
            edges = [dict(record) async for record in edges_result]

            node_total_result = await session.run(
                "MATCH (n:Entity) RETURN count(n) AS total"
            )
            node_total = await node_total_result.single()
            edge_total_result = await session.run(
                "MATCH (a:Entity)-[r]->(b:Entity) RETURN count(r) AS total"
            )
            edge_total = await edge_total_result.single()

        total_nodes = node_total["total"] if node_total else len(nodes)
        total_edges = edge_total["total"] if edge_total else len(edges)
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": total_nodes > len(nodes) or total_edges > len(edges),
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "limit": safe_limit,
        }

    async def get_nodes_by_source(self, source_doc: str, limit: int = 200) -> list[dict]:
        """List entities that came from a single source document."""
        # Neo4j takes LIMIT as a literal here for the same reason as above:
        # clamp the caller-provided value before safe interpolation.
        safe_limit = max(1, min(int(limit), 5000))
        query = f"""
        MATCH (n:Entity)
        WHERE $source_doc IN coalesce(n.source_docs, [])
        RETURN {_NODE_FIELDS}
        LIMIT {safe_limit}
        """
        async with self.driver.session() as session:
            result = await session.run(query, source_doc=source_doc)
            return [dict(record) async for record in result]

    async def get_graph_data_by_source(self, source_doc: str) -> dict:
        """Get the nodes + edges belonging to one source document (for visualization).

        Boundary nodes (endpoints of this source's edges that are themselves
        members of other sources only — e.g. after a merge) are included so no
        edge can reference a missing node: the D3 view hard-crashes on that.
        """
        nodes_query = f"""
        MATCH (n:Entity)
        WHERE $source_doc IN coalesce(n.source_docs, [])
           OR EXISTS {{ MATCH (n)-[r:RELATES_TO]-() WHERE r.source_doc = $source_doc }}
        RETURN {_NODE_FIELDS}
        """
        edges_query = """
        MATCH (a:Entity)-[r]->(b:Entity)
        WHERE r.source_doc = $source_doc
        RETURN a.id AS source, b.id AS target, coalesce(r.type, type(r)) AS type
        """
        async with self.driver.session() as session:
            nodes_result = await session.run(nodes_query, source_doc=source_doc)
            nodes = [dict(record) async for record in nodes_result]

            edges_result = await session.run(edges_query, source_doc=source_doc)
            edges = [dict(record) async for record in edges_result]

        # Uncapped by construction — the keys mirror get_all_graph_data() so
        # callers can read one shape regardless of which path produced it.
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": False,
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "limit": None,
        }

    async def get_sources(self) -> list[dict]:
        """List ingested source documents with their entity counts."""
        query = """
        MATCH (n:Entity) WHERE n.source_docs IS NOT NULL
        UNWIND n.source_docs AS src
        WITH src AS source_doc, count(*) AS node_count, toString(max(n.updated_at)) AS updated_at
        WHERE source_doc <> ''
        RETURN source_doc, node_count, updated_at
        ORDER BY node_count DESC
        """
        async with self.driver.session() as session:
            result = await session.run(query)
            return [dict(record) async for record in result]

    async def delete_source(self, source_doc: str) -> dict:
        """Remove one source document from the graph.

        Edges are per-document assertions, so they are deleted outright. Nodes
        only lose their membership in `source_docs`; a node is deleted once no
        source claims it any more.
        """
        edges_query = """
        MATCH ()-[r:RELATES_TO]->()
        WHERE r.source_doc = $source_doc
        WITH collect(r) AS rels, count(r) AS deleted
        FOREACH (x IN rels | DELETE x)
        RETURN deleted
        """
        membership_query = """
        MATCH (n:Entity)
        WHERE $source_doc IN coalesce(n.source_docs, [])
        SET n.source_docs = [s IN n.source_docs WHERE s <> $source_doc]
        RETURN count(n) AS removed
        """
        orphans_query = """
        MATCH (n:Entity)
        WHERE n.source_docs = []
        WITH collect(n) AS nodes, count(n) AS deleted
        FOREACH (x IN nodes | DETACH DELETE x)
        RETURN deleted
        """

        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                result = await tx.run(edges_query, source_doc=source_doc)
                record = await result.single()
                deleted_edges = record["deleted"] if record else 0

                result = await tx.run(membership_query, source_doc=source_doc)
                record = await result.single()
                removed_memberships = record["removed"] if record else 0

                result = await tx.run(orphans_query)
                record = await result.single()
                deleted_nodes = record["deleted"] if record else 0

                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

        log.info(
            "Deleted source '%s': %d nodes, %d memberships, %d edges",
            source_doc,
            deleted_nodes,
            removed_memberships,
            deleted_edges,
        )
        return {
            "deleted_nodes": deleted_nodes,
            "removed_memberships": removed_memberships,
            "deleted_edges": deleted_edges,
        }

    async def clear_graph(self) -> dict:
        """Delete every entity in the graph. Counts and deletes in one transaction."""
        query = """
        MATCH (n:Entity)
        WITH collect(n) AS nodes, count(n) AS deleted
        FOREACH (x IN nodes | DETACH DELETE x)
        RETURN deleted
        """
        async with self.driver.session() as session:
            result = await session.run(query)
            record = await result.single()
            deleted = record["deleted"] if record else 0

        log.info("Graph cleared: %d nodes deleted", deleted)
        return {"deleted_nodes": deleted}

    async def set_node_embeddings(self, items: list[dict], method: str = "unknown") -> None:
        """Batch-attach embedding vectors to existing nodes.

        Each item is {"id": str, "embedding": list[float]}. `method` records
        which embedding path produced the vectors ("nosana" vs the local hash
        fallback) — duplicate suggestion only trusts real semantic vectors.
        """
        if not items:
            return
        query = """
        UNWIND $items AS it
        MATCH (n:Entity {id: it.id})
        SET n.embedding = it.embedding,
            n.embedding_method = $method
        """
        async with self.driver.session() as session:
            await session.run(query, items=items, method=method)

    # ------------------------------------------------------------------
    # Deduplication: candidate generation
    # ------------------------------------------------------------------
    async def find_duplicate_groups(
        self, limit: int = 20, source_doc: str | None = None
    ) -> list[dict]:
        """Group entities that share a normalized label.

        Groups whose members all carry the exact same provenance set are
        dropped: those are same-document repeats, not cross-document
        duplicates worth merging. Returns
        ``[{"norm_label": str, "nodes": [{id, label, type, summary, source_docs}]}]``.
        """
        safe_limit = max(1, min(int(limit), 100))
        # Over-fetch: the "not sharing all sources" post-filter runs in Python,
        # so a raw LIMIT would silently starve the result.
        fetch_limit = min(safe_limit * 5, 500)
        query = f"""
        MATCH (n:Entity)
        WHERE n.norm_label IS NOT NULL AND n.norm_label <> ''
        WITH n.norm_label AS norm_label, collect(n {_NODE_PROJECTION}) AS nodes
        WITH norm_label, nodes, size(nodes) AS group_size
        WHERE group_size > 1
        RETURN norm_label, nodes
        ORDER BY group_size DESC
        LIMIT $fetch_limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, fetch_limit=fetch_limit)
            rows = [dict(record) async for record in result]

        groups: list[dict] = []
        for row in rows:
            nodes = row["nodes"] or []
            doc_sets = {frozenset(n.get("source_docs") or []) for n in nodes}
            if len(doc_sets) <= 1:
                # Every member has identical provenance — nothing cross-document.
                continue
            if source_doc is not None and not any(
                source_doc in (n.get("source_docs") or []) for n in nodes
            ):
                continue
            groups.append({"norm_label": row["norm_label"], "nodes": nodes})
            if len(groups) >= safe_limit:
                break

        return groups

    async def suggest_similar(
        self, threshold: float = 0.90, cap: int = 200, limit: int = 20
    ) -> dict:
        """Suggest near-duplicate pairs by embedding similarity.

        Only real (Nosana) embeddings are considered — the deterministic hash
        fallback carries no semantic signal, so suggesting from it would be
        noise. Returns
        ``{"pairs": [{"nodes": [a, b], "similarity": float}], "enabled": bool,
        "reason": str | None}``.
        """
        safe_threshold = max(0.5, min(float(threshold), 1.0))
        safe_cap = max(2, min(int(cap), 500))
        safe_limit = max(1, min(int(limit), 100))

        count_query = """
        MATCH (n:Entity)
        WHERE n.embedding_method = 'nosana' AND n.embedding IS NOT NULL
        RETURN count(n) AS embedded
        """
        pairs_query = f"""
        MATCH (a:Entity) WHERE a.embedding_method = 'nosana' AND a.embedding IS NOT NULL
        WITH a ORDER BY a.updated_at DESC LIMIT $cap
        WITH collect(a) AS ns
        UNWIND range(0, size(ns)-2) AS i
        UNWIND range(i+1, size(ns)-1) AS j
        WITH ns[i] AS a, ns[j] AS b
        WHERE a.norm_label <> b.norm_label
        WITH a, b, vector.similarity.cosine(a.embedding, b.embedding) AS sim
        WHERE sim >= $threshold
        RETURN a {_NODE_PROJECTION} AS a, b {_NODE_PROJECTION} AS b, sim
        ORDER BY sim DESC
        LIMIT $limit
        """

        async with self.driver.session() as session:
            result = await session.run(count_query)
            record = await result.single()
            embedded = record["embedded"] if record else 0
            if embedded < 2:
                return {
                    "pairs": [],
                    "enabled": False,
                    "reason": (
                        "semantic embeddings unavailable (hash fallback or no embedded "
                        "nodes); set NOSANA_API_KEY and NOSANA_EMBEDDING_URL"
                    ),
                }

            result = await session.run(
                pairs_query, cap=safe_cap, threshold=safe_threshold, limit=safe_limit
            )
            pairs = [
                {"nodes": [record["a"], record["b"]], "similarity": float(record["sim"])}
                async for record in result
            ]

        return {"pairs": pairs, "enabled": True, "reason": None}

    # ------------------------------------------------------------------
    # Deduplication: merge
    # ------------------------------------------------------------------
    async def merge_nodes(self, node_ids: list[str], canonical_id: str | None = None) -> dict:
        """Merge duplicate entities into one canonical node, in one transaction.

        Edges of the duplicates are repointed at the canonical node (identical
        (endpoint, type) pairs collapse), provenance is unioned, the
        duplicates' labels/aliases become the canonical node's `aliases`, and
        the longest summary wins. The canonical node's `norm_label` and
        `embedding` are left alone — re-embedding is the agent layer's job.

        Returns on success::

            {"ok": True, "canonical_id": str, "aliases": [str], "merged": int,
             "removed_ids": [str],
             "canonical": {id, label, type, summary, source_docs, aliases},
             "edges": [{"source", "target", "type"}]}

        and ``{"ok": False, "missing": [ids]}`` when any id does not exist.
        """
        # De-duplicate while preserving order; fold in an explicit canonical id
        # even when the caller left it out of node_ids.
        ids: list[str] = []
        for nid in list(node_ids or []) + ([canonical_id] if canonical_id else []):
            if nid and nid not in ids:
                ids.append(nid)
        if not ids:
            return {"ok": False, "missing": []}

        validate_query = "MATCH (n:Entity) WHERE n.id IN $ids RETURN collect(n.id) AS found"
        pick_query = """
        MATCH (n:Entity) WHERE n.id IN $ids
        WITH n ORDER BY size(coalesce(n.label, '')) DESC, n.updated_at DESC, n.id ASC
        RETURN collect(n.id)[0] AS canonical_id
        """
        # `coalesce(r.type, 'RELATES_TO')` rather than a bare `r.type`: MERGE
        # rejects a null property value, and both forms read back identically
        # through `coalesce(r.type, type(r))`.
        repoint_out_query = """
        MATCH (c:Entity {id: $canonical_id})
        UNWIND $dup_ids AS did
        MATCH (d:Entity {id: did})-[r:RELATES_TO]->(m:Entity)
        WHERE m.id <> $canonical_id AND NOT m.id IN $dup_ids
        MERGE (c)-[nr:RELATES_TO {type: coalesce(r.type, 'RELATES_TO')}]->(m)
        ON CREATE SET nr.context = r.context,
                      nr.source_doc = r.source_doc,
                      nr.updated_at = r.updated_at
        DELETE r
        """
        repoint_in_query = """
        MATCH (c:Entity {id: $canonical_id})
        UNWIND $dup_ids AS did
        MATCH (m:Entity)-[r:RELATES_TO]->(d:Entity {id: did})
        WHERE m.id <> $canonical_id AND NOT m.id IN $dup_ids
        MERGE (m)-[nr:RELATES_TO {type: coalesce(r.type, 'RELATES_TO')}]->(c)
        ON CREATE SET nr.context = r.context,
                      nr.source_doc = r.source_doc,
                      nr.updated_at = r.updated_at
        DELETE r
        """
        absorb_query = """
        MATCH (c:Entity {id: $canonical_id})
        OPTIONAL MATCH (d:Entity) WHERE d.id IN $dup_ids
        WITH c, [x IN collect(d) WHERE x IS NOT NULL] AS dups
        WITH c, dups,
             coalesce(c.source_docs, [])
               + reduce(acc = [], d IN dups | acc + coalesce(d.source_docs, [])) AS raw_docs,
             coalesce(c.aliases, [])
               + reduce(acc = [], d IN dups | acc + coalesce(d.aliases, []))
               + [d IN dups WHERE d.label IS NOT NULL | d.label] AS raw_aliases,
             reduce(best = coalesce(c.summary, ''), d IN dups |
               CASE WHEN size(coalesce(d.summary, '')) > size(best)
                    THEN coalesce(d.summary, '') ELSE best END) AS best_summary
        WITH c, dups, best_summary,
             reduce(u = [], s IN raw_docs |
               CASE WHEN s IS NULL OR s IN u THEN u ELSE u + s END) AS source_docs,
             reduce(u = [], s IN raw_aliases |
               CASE WHEN s IS NULL OR s = '' OR s = c.label OR s IN u
                    THEN u ELSE u + s END) AS aliases
        SET c.source_docs = source_docs,
            c.aliases = aliases,
            c.summary = best_summary,
            c.updated_at = datetime()
        WITH c, dups, aliases, size(dups) AS merged
        FOREACH (d IN dups | DETACH DELETE d)
        RETURN c {.id, .label, .type, .summary, .source_docs, .aliases} AS canonical,
               aliases, merged
        """
        edges_query = """
        MATCH (a:Entity {id: $canonical_id})-[r:RELATES_TO]-(b:Entity)
        RETURN startNode(r).id AS source, endNode(r).id AS target,
               coalesce(r.type, type(r)) AS type
        """

        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                result = await tx.run(validate_query, ids=ids)
                record = await result.single()
                found = set(record["found"]) if record else set()
                missing = [nid for nid in ids if nid not in found]
                if missing:
                    await tx.rollback()
                    return {"ok": False, "missing": missing}

                chosen = canonical_id
                if chosen is None:
                    result = await tx.run(pick_query, ids=ids)
                    record = await result.single()
                    chosen = record["canonical_id"] if record else None
                if chosen is None:
                    await tx.rollback()
                    return {"ok": False, "missing": ids}

                dup_ids = [nid for nid in ids if nid != chosen]

                if dup_ids:
                    await tx.run(repoint_out_query, canonical_id=chosen, dup_ids=dup_ids)
                    await tx.run(repoint_in_query, canonical_id=chosen, dup_ids=dup_ids)

                result = await tx.run(absorb_query, canonical_id=chosen, dup_ids=dup_ids)
                record = await result.single()
                canonical = dict(record["canonical"]) if record else {}
                aliases = list(record["aliases"]) if record else []
                merged = int(record["merged"]) if record else 0

                result = await tx.run(edges_query, canonical_id=chosen)
                edges = [dict(r) async for r in result]

                await tx.commit()
            except Exception:
                await tx.rollback()
                raise

        log.info("Merged %d node(s) into '%s'", merged, chosen)
        return {
            "ok": True,
            "canonical_id": chosen,
            "aliases": aliases,
            "merged": merged,
            "removed_ids": dup_ids,
            "canonical": canonical,
            "edges": edges,
        }
