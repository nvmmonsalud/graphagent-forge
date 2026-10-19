"""Neo4j graph client — connection, schema, and queries."""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from typing import Any

from neo4j import AsyncGraphDatabase

from src.graph.embedding_space import configured_embedding_space
from src.graph.normalize import normalize_label
from src.ingestion.chunks import CHUNK_SIZE, MAX_CHUNKS, MAX_DOCUMENT_CHARS, chunk_id

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
_EVIDENCE_STOPWORDS = frozenset(
    "what which when where why how is are was were does do did the an and or "
    "of to in for from with about please tell me".split()
)


class EvidenceResults(list):
    """List-compatible internal results with per-call retrieval diagnostics."""

    def __init__(self, chunks=(), *, diagnostics=None):
        super().__init__(chunks)
        self.diagnostics = diagnostics or {}


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
            "CREATE CONSTRAINT source_id IF NOT EXISTS FOR (s:Source) REQUIRE s.id IS UNIQUE",
            "CREATE CONSTRAINT evidence_id IF NOT EXISTS "
            "FOR (c:EvidenceChunk) REQUIRE c.id IS UNIQUE",
            "CREATE CONSTRAINT ingest_operation_id IF NOT EXISTS "
            "FOR (o:IngestOperation) REQUIRE o.id IS UNIQUE",
            "CREATE INDEX ingest_operation_created IF NOT EXISTS "
            "FOR (o:IngestOperation) ON (o.created_at)",
            "CREATE CONSTRAINT graph_state_id IF NOT EXISTS "
            "FOR (s:GraphState) REQUIRE s.id IS UNIQUE",
            "CREATE INDEX evidence_source_id IF NOT EXISTS "
            "FOR (c:EvidenceChunk) ON (c.source_id)",
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
            await self._prune_ingest_operations(session)

        await self._migrate_source_docs()
        await self._backfill_norm_labels()
        await self.init_vector_index()
        # Synchronous index updates preserve current-source publication visibility.
        # English stemming is deliberately evaluated on the small M5 fixture.
        try:
            async with self.driver.session() as session:
                await session.run("""
                    CREATE FULLTEXT INDEX evidence_fulltext IF NOT EXISTS
                    FOR (c:EvidenceChunk) ON EACH [c.text]
                    OPTIONS {indexConfig: {`fulltext.analyzer`: 'english'}}
                    """)
        except Exception:
            log.warning("Evidence fulltext index unavailable; lexical fallback enabled")
        log.info("Neo4j schema initialized")

    async def _migrate_source_docs(self) -> None:
        """Fold the legacy scalar `n.source_doc` into the `n.source_docs` list.

        Idempotent (nodes lose `source_doc` as they are migrated, so a re-run
        matches nothing) and batched. MUST run as an auto-commit query:
        `CALL {} IN TRANSACTIONS` is rejected inside an explicit transaction.

        The `(n)` variable scope clause is required, not optional style: without
        it, `CALL { WITH n ... }` is only a deprecation warning today (Neo4j
        5.26), but the implicit-import form is slated for removal in a future
        major and this query would start failing outright rather than warning.
        """
        query = f"""
        MATCH (n:Entity) WHERE n.source_doc IS NOT NULL
        CALL (n) {{
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
        """Switch active space under the mutation lock, quarantining old vectors."""
        space = configured_embedding_space()
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                await tx.run("""
                    MATCH (n:Entity)
                    WHERE n:SemanticEntity OR n.semantic_embedding IS NOT NULL
                    WITH n WHERE coalesce(n.embedding_method, '') <> 'nosana'
                      OR coalesce(n.embedding_model, '') <> $model
                      OR coalesce(n.embedding_version, '') <> $version
                      OR size(coalesce(n.semantic_embedding, [])) <> $dimension
                    REMOVE n:SemanticEntity, n.semantic_embedding
                    """, **space)
                await tx.run("""
                    MATCH (g:GraphState {id: 'sources'})
                    SET g.embedding_model = $model, g.embedding_version = $version,
                        g.embedding_dimension = $dimension
                    """, **space)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
            await session.run("""
                CREATE VECTOR INDEX entity_semantic_embedding IF NOT EXISTS
                FOR (n:SemanticEntity) ON (n.semantic_embedding)
                OPTIONS {indexConfig: {`vector.dimensions`: 384,
                         `vector.similarity_function`: 'cosine'}}
                """)

    @staticmethod
    async def _space_is_active(runner, space):
        result = await runner.run("""
            MATCH (g:GraphState {id:'sources'})
            RETURN g.embedding_model = $model AND g.embedding_version = $version
               AND g.embedding_dimension = $dimension AS active
            """, **space)
        row = await result.single()
        return bool(row and row["active"])

    @staticmethod
    async def _lock_graph(tx):
        # The lock also serializes source membership mutations across clear/merge.
        await tx.run("MERGE (g:GraphState {id: 'sources'}) "
                     "SET g.revision = coalesce(g.revision, 0) + 1")

    async def _run_mutation(self, query, **params):
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                if params.get("source_doc"):
                    await self._touch_legacy_source(tx, params["source_doc"])
                await tx.run(query, **params)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise

    @staticmethod
    async def _touch_legacy_source(tx, source_doc):
        # Legacy seeders retain their historical graph IDs. Revive existing
        # reservations/tombstones and invalidate any extraction started before
        # this direct write. New legacy IDs remain eligible for explicit migration.
        await Neo4jClient._invalidate_source_evidence(tx, source_doc)
        await tx.run("""
            MATCH (s:Source {id: $id})
            SET s = {id: $id, title: $id, kind: 'legacy', version: $version,
                     updated_at: datetime()}
            """, id=source_doc, version="legacy_" + uuid.uuid4().hex)

    async def get_source(self, source_id: str) -> dict | None:
        # Register missing identities so clear/delete can invalidate extraction
        # that started before the deletion, including first-ever publications.
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                result = await tx.run("""
                    MERGE (s:Source {id: $id})
                    ON CREATE SET s._placeholder = true
                    RETURN properties(s) AS source
                    """, id=source_id)
                row = await result.single()
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        source = dict(row["source"]) if row else {}
        if source.get("_placeholder"):
            return None
        source = {k: v for k, v in source.items() if not k.startswith("_")}
        if "coverage" in source:
            source["coverage"] = json.loads(source["coverage"])
        return source

    @staticmethod
    def _prepare_chunks(graph_data: dict, source: dict) -> list[dict]:
        """Validate server-assigned identities and Unicode offsets before any mutation."""
        chunks = graph_data.get("chunks", [])
        if not isinstance(chunks, list) or len(chunks) > MAX_CHUNKS:
            raise ValueError("Invalid evidence chunk count")
        prepared = []
        seen = set()
        for ordinal, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError("Invalid evidence chunk")
            text = chunk.get("text")
            start, end = chunk.get("start"), chunk.get("end")
            if (not isinstance(text, str) or not 0 < len(text) <= CHUNK_SIZE
                    or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= MAX_DOCUMENT_CHARS
                    or end - start != len(text)
                    or type(chunk.get("ordinal")) is not int or chunk["ordinal"] != ordinal
                    or chunk.get("source_id") != source["id"]
                    or chunk.get("version") != source["version"]):
                raise ValueError("Invalid evidence chunk offsets or source version")
            expected = chunk_id(source["id"], source["version"], ordinal, text)
            if chunk.get("id") != expected or expected in seen:
                raise ValueError("Invalid evidence chunk identity")
            seen.add(expected)
            prepared.append({key: chunk[key] for key in
                             ("id", "source_id", "version", "ordinal", "start", "end", "text")})
        return prepared

    @staticmethod
    async def _invalidate_source_evidence(tx, source_id):
        # Strip references even on entities retained by another source's assertion.
        # Keep deletion and reference cleanup in the same mutation transaction.
        result = await tx.run("""
            MATCH (c:EvidenceChunk {source_id: $source_id})
            RETURN collect(c.id) AS ids
            """, source_id=source_id)
        row = await result.single()
        ids = row["ids"] if row else []
        if ids:
            await tx.run("""
                MATCH (n:Entity) WHERE any(id IN coalesce(n.evidence_ids, []) WHERE id IN $ids)
                SET n.evidence_ids = [id IN n.evidence_ids WHERE NOT id IN $ids]
                """, ids=ids)
            await tx.run("""
                MATCH ()-[r:RELATES_TO]->()
                WHERE any(id IN coalesce(r.evidence_ids, []) WHERE id IN $ids)
                SET r.evidence_ids = [id IN r.evidence_ids WHERE NOT id IN $ids]
                """, ids=ids)
        await tx.run("MATCH (c:EvidenceChunk {source_id: $source_id}) DETACH DELETE c",
                     source_id=source_id)

    async def _read_evidence(self, query: str, **params) -> list[dict]:
        # Coordinate even read-only excerpts with refresh/delete/clear: Neo4j's
        # read-committed isolation alone does not guarantee repeatable reads.
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                result = await tx.run(query, **params)
                rows = [dict(row["chunk"]) async for row in result]
                await tx.commit()
                return rows
            except BaseException:
                await tx.rollback()
                raise

    async def get_evidence(self, evidence_id: str) -> dict | None:
        rows = await self.validate_evidence_ids([evidence_id])
        return rows[0] if rows else None

    async def validate_evidence_ids(self, ids: list[str]) -> list[dict]:
        """Return exact, currently published excerpts, bounded independently of caller."""
        if (not isinstance(ids, list) or len(ids) > 36
                or any(not isinstance(value, str) or len(value) > 80 for value in ids)):
            raise ValueError("Invalid evidence IDs")
        if not ids:
            return []
        return await self._read_evidence("""
            UNWIND $ids AS id
            MATCH (s:Source)-[:HAS_CHUNK]->(c:EvidenceChunk {id: id})
            WHERE c.source_id = s.id AND c.version = s.version
              AND NOT coalesce(s._deleted, false) AND NOT coalesce(s._placeholder, false)
            RETURN DISTINCT c {.*, title: s.title, uri: s.uri} AS chunk
            LIMIT 36
            """, ids=list(dict.fromkeys(ids)))

    async def get_source_evidence(self, source_id: str) -> list[dict]:
        return await self._read_evidence("""
            MATCH (s:Source {id: $id})-[:HAS_CHUNK]->(c:EvidenceChunk)
            WHERE c.source_id = s.id AND c.version = s.version
              AND NOT coalesce(s._deleted, false) AND NOT coalesce(s._placeholder, false)
            RETURN c {.*, title: s.title, uri: s.uri} AS chunk
            ORDER BY c.ordinal LIMIT 12
            """, id=source_id)

    async def search_evidence(self, question: str, entity_ids=None, limit: int = 6,
                              source_id: str | None = None) -> list[dict]:
        """Fuse bounded indexed lexical and entity-linked current excerpt ranks.

        Source filtering precedes ranking and LIMIT in both candidate queries.
        Lucene syntax is built exclusively from bounded alphanumeric terms.
        Only bounded candidate excerpts cross the database/Python boundary.
        """
        terms = list(dict.fromkeys(
            term[:64] for term in re.findall(r"[^\W_]{2,}", question[:4000].lower())
            if term not in _EVIDENCE_STOPWORDS
        ))[:12]
        selected = list(dict.fromkeys(entity_ids or []))[:1000]
        limit = max(1, min(int(limit), 12))
        current = """
            MATCH (s:Source)-[:HAS_CHUNK]->(c:EvidenceChunk)
            WHERE c.source_id = s.id AND c.version = s.version
              AND NOT coalesce(s._deleted, false) AND NOT coalesce(s._placeholder, false)
              AND ($source_id IS NULL OR s.id = $source_id)
            """
        lexical = []
        fallback = False
        if terms:
            try:
                lexical = await self._read_evidence("""
                    CALL db.index.fulltext.queryNodes('evidence_fulltext', $lucene)
                    YIELD node, score
                    WITH node AS c, score
                    """ + current + """
                    WITH s, c, score ORDER BY score DESC, s.id, c.ordinal LIMIT 40
                    RETURN c {.*, title: s.title, uri: s.uri} AS chunk
                    """, lucene=' OR '.join('"' + term + '"' for term in terms),
                    source_id=source_id)
            except Exception:
                fallback = True
                log.info("Using bounded lexical evidence fallback")
                lexical = await self._read_evidence(current + """
                    WITH s, c ORDER BY s.updated_at DESC, s.id, c.ordinal LIMIT 1000
                    WITH s, c, reduce(score = 0, term IN $terms |
                        score + CASE WHEN toLower(c.text) CONTAINS term THEN 1 ELSE 0 END) AS score
                    WHERE score > 0
                    WITH s, c, score ORDER BY score DESC, s.id, c.ordinal LIMIT 40
                    RETURN c {.*, title: s.title, uri: s.uri} AS chunk
                    """, terms=terms, source_id=source_id)
        linked = []
        if selected:
            linked = await self._read_evidence("""
                UNWIND $entity_ids AS entity_id
                MATCH (n:Entity {id: entity_id})
                UNWIND coalesce(n.evidence_ids, []) AS evidence_id
                WITH DISTINCT evidence_id
                MATCH (c:EvidenceChunk {id: evidence_id})
                """ + current + """
                AND EXISTS { MATCH (n:Entity) WHERE n.id IN $entity_ids
                    AND c.id IN coalesce(n.evidence_ids, []) }
                WITH s, c ORDER BY s.id, c.ordinal LIMIT 40
                RETURN c {.*, title: s.title, uri: s.uri} AS chunk
                """, entity_ids=selected, source_id=source_id)
        by_id = {}
        for name, chunks in [('lexical_rank', lexical), ('entity_rank', linked)]:
            for rank, chunk in enumerate(chunks, 1):
                item = by_id.setdefault(chunk['id'], {**chunk, 'retrieval': {}})
                item['retrieval'][name] = rank
        method = 'lexical_fallback' if fallback else 'fulltext_rrf'
        for item in by_id.values():
            ranks = item['retrieval']
            ranks['rrf_score'] = sum(1 / (60 + ranks[key]) for key in (
                'lexical_rank', 'entity_rank') if key in ranks)
            ranks['method'] = method
        ranked = sorted(by_id.values(), key=lambda item: (
            -item['retrieval']['rrf_score'], item['source_id'], item['ordinal'], item['id']))
        return EvidenceResults(ranked[:limit], diagnostics={
            'method': method, 'fallback': fallback, 'rrf_k': 60,
            'candidate_count': len(by_id), 'lexical_candidates': len(lexical),
            'entity_candidates': len(linked),
            **({'fallback_candidate_limit': 1000} if fallback else {}),
        })

    async def browse_graph(self, after_id=None, limit=100, source_id=None):
        """Deterministic bounded node pages with edges closed over each page."""
        safe_limit = max(1, min(int(limit), 100))
        async with self.driver.session() as session:
            result = await session.run("""
                MATCH (n:Entity)
                WHERE ($after_id IS NULL OR n.id > $after_id)
                  AND ($source_id IS NULL OR $source_id IN coalesce(n.source_docs, []))
                WITH n ORDER BY n.id LIMIT $fetch_limit
                RETURN """ + _NODE_FIELDS,
                after_id=after_id, source_id=source_id, fetch_limit=safe_limit + 1)
            rows = [dict(record) async for record in result]
            nodes = rows[:safe_limit]
            result = await session.run("""
                MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
                WHERE a.id IN $ids AND b.id IN $ids
                  AND ($source_id IS NULL OR r.source_doc = $source_id)
                RETURN a.id AS source, b.id AS target, coalesce(r.type, type(r)) AS type
                ORDER BY source, target, type LIMIT 301
                """, ids=[node['id'] for node in nodes], source_id=source_id)
            edges = [dict(record) async for record in result]
        return {'nodes': nodes, 'edges': edges[:300], 'limit': safe_limit,
                'next_id': nodes[-1]['id'] if len(rows) > safe_limit else None,
                'truncated': len(rows) > safe_limit or len(edges) > 300}

    @staticmethod
    def _validate_operation_id(operation_id):
        if (not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128
                or any(not 33 <= ord(c) <= 126 for c in operation_id)):
            raise ValueError("Invalid ingestion operation ID")

    @staticmethod
    async def _prune_ingest_operations(runner):
        await runner.run("MATCH (o:IngestOperation) "
                         "WHERE o.created_at <= datetime() - duration('P7D') DELETE o")

    @staticmethod
    async def _read_ingest_operation(runner, operation_id):
        result = await runner.run("""
            MATCH (o:IngestOperation {id: $operation_id})
            WHERE o.created_at > datetime() - duration('P7D')
            RETURN o.source AS source, o.version AS version,
                   o.nodes AS nodes, o.edges AS edges
            """, operation_id=operation_id)
        row = await result.single()
        return {"success": True, **dict(row), "unchanged": True} if row else None

    async def get_ingest_operation(self, operation_id: str) -> dict | None:
        """Private publication checkpoint; independent of current source existence."""
        self._validate_operation_id(operation_id)
        async with self.driver.session() as session:
            await self._prune_ingest_operations(session)
            return await self._read_ingest_operation(session, operation_id)

    @staticmethod
    async def _write_ingest_operation(tx, operation_id, source, nodes, edges):
        await tx.run("""
            CREATE (o:IngestOperation {id: $operation_id, source: $source,
              version: $version, nodes: $nodes, edges: $edges, created_at: datetime()})
            """, operation_id=operation_id, source=source["id"], version=source["version"],
                     nodes=nodes, edges=edges)

    async def publish_source_graph(self, graph_data: dict, *, source: dict,
                                   expected_version: str | None,
                                   operation_id: str | None = None) -> dict:
        """Publish a complete source refresh atomically with optimistic concurrency."""
        if operation_id is not None:
            self._validate_operation_id(operation_id)
        sid = source["id"]
        chunks = self._prepare_chunks(graph_data, source)
        valid_evidence = {c["id"] for c in chunks}

        def evidence_ids(item):
            refs = item.get("evidence_ids", [])
            if (not isinstance(refs, list) or len(refs) > MAX_CHUNKS
                    or any(not isinstance(ref, str) or ref not in valid_evidence for ref in refs)):
                raise ValueError("Graph contains invalid evidence references")
            return list(dict.fromkeys(refs))

        nodes = [{"id": n["id"], "label": n.get("label", ""),
                  "type": n.get("type", "Unknown"),
                  "summary": n.get("properties", {}).get("summary", ""),
                  "norm_label": normalize_label(n.get("label", "")),
                  "evidence_ids": evidence_ids(n)}
                 for n in graph_data.get("nodes", [])]
        edges = [{"source": e["source"], "target": e["target"],
                  "type": e.get("relationship", "RELATES_TO"),
                  "context": e.get("properties", {}).get("context", ""),
                  "evidence_ids": evidence_ids(e)}
                 for e in graph_data.get("edges", [])]
        ids = {n["id"] for n in nodes}
        if any(e["source"] not in ids or e["target"] not in ids for e in edges):
            raise ValueError("Source graph contains missing edge endpoints")
        metadata = {k: source[k] for k in ("id", "title", "kind", "uri", "content_hash",
                                          "version", "extraction_version") if k in source}
        metadata["coverage"] = (json.dumps(source["coverage"], allow_nan=False)
                                if "coverage" in source else None)
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                if operation_id is not None:
                    await self._prune_ingest_operations(tx)
                    prior = await self._read_ingest_operation(tx, operation_id)
                    if prior is not None:
                        await tx.commit()
                        return dict(nodes_written=prior["nodes"], edges_written=prior["edges"],
                                    source=prior["source"], version=prior["version"],
                                    unchanged=True, conflict=False)
                result = await tx.run("""
                    MERGE (s:Source {id: $id})
                    SET s._lock = coalesce(s._lock, 0) + 1
                    RETURN s.version AS version
                    """, id=sid)
                row = await result.single()
                current = row["version"] if row else None
                if current == source["version"]:
                    if operation_id is not None:
                        await self._write_ingest_operation(tx, operation_id, source, 0, 0)
                        await tx.commit()
                        return dict(nodes_written=0, edges_written=0, unchanged=True,
                                    conflict=False, source=sid, version=current)
                    await tx.rollback()
                    return dict(nodes_written=0, edges_written=0, unchanged=True, conflict=False)
                if current != expected_version:
                    await tx.rollback()
                    return dict(nodes_written=0, edges_written=0, unchanged=False, conflict=True)
                await self._remove_source_graph(tx, sid)
                await tx.run("""
                    UNWIND $nodes AS node
                    MERGE (n:Entity {id: node.id})
                    SET n.label = node.label, n.type = node.type, n.summary = node.summary,
                        n.norm_label = node.norm_label,
                        n.evidence_ids = node.evidence_ids,
                        n.source_docs = CASE WHEN $sid IN coalesce(n.source_docs, [])
                          THEN n.source_docs ELSE coalesce(n.source_docs, []) + $sid END,
                        n.updated_at = datetime()
                    """, nodes=nodes, sid=sid)
                await tx.run("""
                    UNWIND $edges AS edge
                    MATCH (a:Entity {id: edge.source}), (b:Entity {id: edge.target})
                    MERGE (a)-[r:RELATES_TO {type: edge.type, source_doc: $sid}]->(b)
                    SET r.context = edge.context,
                        r.evidence_ids = reduce(u = coalesce(r.evidence_ids, []),
                          id IN edge.evidence_ids | CASE WHEN id IN u THEN u ELSE u + id END),
                        r.updated_at = datetime()
                    """, edges=edges, sid=sid)
                await tx.run("""
                    MATCH (s:Source {id: $sid})
                    UNWIND $chunks AS chunk
                    CREATE (c:EvidenceChunk) SET c = chunk
                    CREATE (s)-[:HAS_CHUNK]->(c)
                    """, sid=sid, chunks=chunks)
                await tx.run("MATCH (s:Source {id: $sid}) SET s += $metadata, "
                             "s.updated_at = datetime() "
                             "REMOVE s._placeholder, s._deleted, s._migration",
                             sid=sid, metadata=metadata)
                if operation_id is not None:
                    await self._write_ingest_operation(tx, operation_id, source,
                                                       len(nodes), len(edges))
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        result = dict(nodes_written=len(nodes), edges_written=len(edges),
                      unchanged=False, conflict=False)
        if operation_id is not None:
            result.update(source=sid, version=source["version"])
        return result

    @staticmethod
    async def _remove_source_graph(tx, source_doc):
        await Neo4jClient._invalidate_source_evidence(tx, source_doc)
        result = await tx.run("""
            MATCH ()-[r:RELATES_TO]->() WHERE r.source_doc = $source_doc
            WITH collect(r) AS rels, count(r) AS deleted,
                 collect(DISTINCT startNode(r).id) + collect(DISTINCT endNode(r).id) AS endpoints
            FOREACH (r IN rels | DELETE r) RETURN deleted, endpoints
            """, source_doc=source_doc)
        row = await result.single()
        deleted_edges = row["deleted"] if row else 0
        endpoints = row["endpoints"] if row else []
        result = await tx.run("""
            MATCH (n:Entity) WHERE $source_doc IN coalesce(n.source_docs, [])
            SET n.source_docs = [s IN n.source_docs WHERE s <> $source_doc],
                n.summary = '', n.aliases = []
            REMOVE n:SemanticEntity, n.semantic_embedding, n.embedding,
                   n.embedding_method, n.embedding_model, n.embedding_version,
                   n.embedding_dimension
            WITH collect(n) AS members, count(n) AS removed
            WITH removed, [n IN members WHERE size(n.source_docs) = 0
                 AND NOT EXISTS { MATCH (n)-[r:RELATES_TO]-()
                                  WHERE r.source_doc <> $source_doc }] AS orphans
            FOREACH (n IN orphans | DETACH DELETE n)
            RETURN removed, size(orphans) AS deleted
            """, source_doc=source_doc)
        row = await result.single()
        counts = {"deleted_edges": deleted_edges,
                  "removed_memberships": row["removed"] if row else 0,
                  "deleted_nodes": row["deleted"] if row else 0}
        # A boundary entity retained for another source's assertion may have
        # lost its last edge now, despite having no direct source membership.
        result = await tx.run("""
            MATCH (n:Entity) WHERE n.id IN $endpoints
              AND coalesce(n.source_docs, []) = [] AND n.source_doc IS NULL
              AND NOT EXISTS { MATCH (n)-[:RELATES_TO]-() }
            WITH collect(n) AS nodes, count(n) AS deleted
            FOREACH (n IN nodes | DETACH DELETE n) RETURN deleted
            """, endpoints=endpoints)
        row = await result.single()
        counts["deleted_nodes"] += row["deleted"] if row else 0
        return counts

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
        REMOVE n:SemanticEntity, n.semantic_embedding, n.embedding,
               n.embedding_method, n.embedding_model, n.embedding_version,
               n.embedding_dimension
        """
        props = node.get("properties", {})
        label = node.get("label", "")
        await self._run_mutation(
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
        MERGE (a)-[r:RELATES_TO {type: $rel_type, source_doc: $source_doc}]->(b)
        SET r.context = $context,
            r.source_doc = $source_doc,
            r.updated_at = datetime()
        """
        props = edge.get("properties", {})
        await self._run_mutation(
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
        REMOVE n:SemanticEntity, n.semantic_embedding, n.embedding,
               n.embedding_method, n.embedding_model, n.embedding_version,
               n.embedding_dimension
        """
        edge_query = """
        UNWIND $edges AS edge
        MATCH (a:Entity {id: edge.source})
        MATCH (b:Entity {id: edge.target})
        MERGE (a)-[r:RELATES_TO {type: edge.rel_type, source_doc: $source_doc}]->(b)
        SET r.context = edge.context,
            r.source_doc = $source_doc,
            r.updated_at = datetime()
        """

        if batch_nodes or batch_edges:
            async with self.driver.session() as session:
                tx = await session.begin_transaction()
                try:
                    await self._lock_graph(tx)
                    if source_doc:
                        await self._touch_legacy_source(tx, source_doc)
                    if batch_nodes:
                        await tx.run(node_query, nodes=batch_nodes, source_doc=source_doc)
                    if batch_edges:
                        await tx.run(edge_query, edges=batch_edges, source_doc=source_doc)
                    await tx.commit()
                except BaseException:
                    await tx.rollback()
                    raise

        log.info("Graph written: %d nodes, %d edges", len(nodes), len(edges))
        return {"nodes_written": len(nodes), "edges_written": len(edges)}

    # ------------------------------------------------------------------
    # Read / query operations
    # ------------------------------------------------------------------
    async def get_node_context(self, label: str, depth: int = 2) -> str:
        """Legacy label-based context helper; GraphRAG uses get_entity_context by ID.

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
        """Substring lexical search across entity labels, aliases, and summaries."""
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

    async def vector_search(self, embedding: list[float], limit: int = 10,
                            model=None, version=None) -> list[dict]:
        space = configured_embedding_space()
        if ((model is not None and model != space["model"])
                or (version is not None and version != space["version"])
                or not self._valid_vector(embedding, space["dimension"])):
            return []
        query = """
        CALL db.index.vector.queryNodes('entity_semantic_embedding', $limit, $embedding)
        YIELD node, score
        WHERE node.embedding_method = 'nosana' AND node.embedding_model = $model
          AND node.embedding_version = $version
          AND size(node.semantic_embedding) = $dimension
        RETURN node.id AS id, node.label AS label, node.type AS type,
               node.summary AS summary, node.source_docs AS source_docs,
               node.aliases AS aliases, score
        """
        async with self.driver.session() as session:
            if not await self._space_is_active(session, space):
                return []
            result = await session.run(query, embedding=embedding,
                                       limit=max(1, min(int(limit), 100)), **space)
            return [dict(record) async for record in result]

    async def get_entity_context(self, entity_id: str, depth: int = 2) -> dict:
        """Expand a bounded frontier, never enumerate all paths through a dense graph."""
        safe_depth = max(1, min(int(depth), 4))
        nodes, edges, seen_edges, sources = {}, [], [], set()
        projection = "{.id, .label, .type, .summary, .source_docs}"
        async with self.driver.session() as session:
            result = await session.run(
                f"MATCH (n:Entity {{id: $id}}) RETURN n {projection} AS node", id=entity_id)
            row = await result.single()
            if row:
                nodes[entity_id] = dict(row["node"])
            frontier = list(nodes)
            for _ in range(safe_depth):
                if not frontier or len(edges) >= 50:
                    break
                result = await session.run(f"""
                    MATCH (a:Entity) WHERE a.id IN $frontier
                    MATCH (a)-[r:RELATES_TO]-(b:Entity)
                    WHERE NOT elementId(r) IN $seen
                    WITH a, r, b LIMIT $remaining
                    RETURN a {projection} AS a, b {projection} AS b,
                      elementId(r) AS edge_id,
                      {{source: startNode(r).id, target: endNode(r).id,
                        type: coalesce(r.type, type(r)), context: r.context,
                        source_doc: r.source_doc}} AS edge
                    """, frontier=frontier, seen=seen_edges, remaining=50 - len(edges))
                next_frontier = []
                async for row in result:
                    for node in (row["a"], row["b"]):
                        if node["id"] not in nodes:
                            next_frontier.append(node["id"])
                        nodes[node["id"]] = dict(node)
                    if row["edge_id"] not in seen_edges:
                        seen_edges.append(row["edge_id"])
                        edges.append(dict(row["edge"]))
                frontier = list(dict.fromkeys(next_frontier))
        for node in nodes.values():
            sources.update(node.get("source_docs") or [])
        sources.update(e["source_doc"] for e in edges if e.get("source_doc"))
        lines = [f"{n['label']} ({n['type']}): {n.get('summary') or ''}"
                 for n in nodes.values()]
        lines += [f"{nodes[e['source']]['label']} — [{e['type']}] → "
                  f"{nodes[e['target']]['label']}: {e.get('context') or ''}" for e in edges]
        return {"text": "\n".join(lines), "source_docs": sorted(sources - {""}),
                "entity_ids": list(nodes), "nodes": list(nodes.values()), "edges": edges}

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
        """List both explicit metadata and unmigrated legacy source memberships."""
        query = """
        CALL () {
          MATCH (n:Entity) UNWIND coalesce(n.source_docs, []) AS id RETURN id
          UNION MATCH (s:Source) WHERE NOT coalesce(s._deleted, false)
            AND NOT coalesce(s._placeholder, false) RETURN s.id AS id
        }
        WITH DISTINCT id WHERE id <> ''
        OPTIONAL MATCH (n:Entity) WHERE id IN coalesce(n.source_docs, [])
        WITH id, count(n) AS node_count, toString(max(n.updated_at)) AS updated_at
        OPTIONAL MATCH (s:Source {id: id})
        RETURN id AS source_doc, node_count, updated_at,
               coalesce(s.title, id) AS title, coalesce(s.kind, 'legacy') AS kind,
               s.uri AS uri, s.version AS version, s.coverage AS coverage
        ORDER BY node_count DESC, source_doc
        """
        async with self.driver.session() as session:
            result = await session.run(query)
            sources = [dict(record) async for record in result]
            for source in sources:
                if source.get("coverage") is not None:
                    source["coverage"] = json.loads(source["coverage"])
            return sources

    async def delete_source(self, source_doc: str) -> dict:
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                await tx.run("MERGE (s:Source {id: $id}) "
                             "SET s._lock = coalesce(s._lock, 0) + 1", id=source_doc)
                counts = await self._remove_source_graph(tx, source_doc)
                await tx.run("MATCH (s:Source {id: $id}) "
                             "SET s = {id: $id, version: $version, _deleted: true}",
                             id=source_doc, version="deleted_" + uuid.uuid4().hex)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        return counts

    async def clear_graph(self) -> dict:
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                result = await tx.run("""
                    MATCH (n:Entity) WITH collect(n) AS nodes, count(n) AS deleted
                    FOREACH (n IN nodes | DETACH DELETE n) RETURN deleted
                    """)
                row = await result.single()
                await tx.run("MATCH (c:EvidenceChunk) DETACH DELETE c")
                await tx.run("MATCH (s:Source) "
                             "SET s = {id: s.id, version: $version, _deleted: true}",
                             version="deleted_" + uuid.uuid4().hex)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        return {"deleted_nodes": row["deleted"] if row else 0}

    @staticmethod
    def _valid_vector(vector, dimension):
        try:
            return (isinstance(vector, list) and len(vector) == dimension
                    and all(isinstance(v, (float, int)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in vector)
                    and any(v != 0 for v in vector))
        except (OverflowError, ValueError, TypeError):
            return False

    async def set_node_embeddings(self, items: list[dict], method: str = "unknown") -> int:
        """Quarantine fallback/unknown vectors; index only explicit active-space results."""
        if not items:
            return 0
        space = configured_embedding_space()
        prepared = []
        for item in items:
            item_method = item.get("method", method)
            model, version = item.get("model"), item.get("version")
            vector = item.get("embedding")
            semantic = (item_method == "nosana" and model == space["model"]
                        and version == space["version"]
                        and item.get("dimension") == space["dimension"]
                        and self._valid_vector(vector, space["dimension"]))
            prepared.append({"id": item["id"], "embedding": vector,
                             "method": item_method, "model": model, "version": version,
                             "dimension": item.get("dimension"), "semantic": semantic,
                             "guard_summary": "expected_summary" in item,
                             "guard_label": "expected_label" in item,
                             "expected_summary": item.get("expected_summary"),
                             "expected_label": item.get("expected_label")})
        query = """
        UNWIND $items AS it MATCH (n:Entity {id: it.id})
        WHERE (NOT it.guard_summary OR coalesce(n.summary, '') = it.expected_summary)
          AND (NOT it.guard_label OR coalesce(n.label, '') = it.expected_label)
        SET n.embedding = it.embedding, n.embedding_method = it.method,
            n.embedding_model = it.model, n.embedding_version = it.version,
            n.embedding_dimension = it.dimension
        REMOVE n:SemanticEntity, n.semantic_embedding
        FOREACH (_ IN CASE WHEN it.semantic THEN [1] ELSE [] END |
          SET n:SemanticEntity, n.semantic_embedding = it.embedding)
        RETURN count(n) AS stored
        """
        async with self.driver.session() as session:
            tx = await session.begin_transaction()
            try:
                await self._lock_graph(tx)
                if not await self._space_is_active(tx, space):
                    await tx.rollback()
                    return 0
                result = await tx.run(query, items=prepared)
                row = await result.single()
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        return row["stored"] if row else 0

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
        MATCH (n:SemanticEntity)
        WHERE n.embedding_method = 'nosana' AND n.embedding_model = $model
          AND n.embedding_version = $version AND size(n.semantic_embedding) = $dimension
        RETURN count(n) AS embedded
        """
        pairs_query = f"""
        MATCH (a:SemanticEntity) WHERE a.embedding_method = 'nosana'
          AND a.embedding_model = $model AND a.embedding_version = $version
          AND size(a.semantic_embedding) = $dimension
        WITH a ORDER BY a.updated_at DESC LIMIT $cap
        WITH collect(a) AS ns
        UNWIND range(0, size(ns)-2) AS i
        UNWIND range(i+1, size(ns)-1) AS j
        WITH ns[i] AS a, ns[j] AS b
        WHERE a.norm_label <> b.norm_label
        WITH a, b, vector.similarity.cosine(a.semantic_embedding, b.semantic_embedding) AS sim
        WHERE sim >= $threshold
        RETURN a {_NODE_PROJECTION} AS a, b {_NODE_PROJECTION} AS b, sim
        ORDER BY sim DESC
        LIMIT $limit
        """

        async with self.driver.session() as session:
            if not await self._space_is_active(session, configured_embedding_space()):
                return {"pairs": [], "enabled": False, "reason": "embedding space changed"}
            result = await session.run(count_query, **configured_embedding_space())
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
                pairs_query, cap=safe_cap, threshold=safe_threshold, limit=safe_limit,
                **configured_embedding_space()
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
        (endpoint, type, source_doc) triples collapse), provenance is unioned, the
        duplicates' labels/aliases become the canonical node's `aliases`, and
        the longest summary wins. The canonical node's `norm_label` is preserved;
        embeddings are invalidated transactionally before agent re-embedding.

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
        MERGE (c)-[nr:RELATES_TO {type: coalesce(r.type, 'RELATES_TO'),
                                      source_doc: coalesce(r.source_doc, '')}]->(m)
        ON CREATE SET nr.context = r.context,
                      nr.source_doc = r.source_doc,
                      nr.updated_at = r.updated_at
        SET nr.evidence_ids = reduce(u = coalesce(nr.evidence_ids, []),
            id IN coalesce(r.evidence_ids, []) | CASE WHEN id IN u THEN u ELSE u + id END)
        DELETE r
        """
        repoint_in_query = """
        MATCH (c:Entity {id: $canonical_id})
        UNWIND $dup_ids AS did
        MATCH (m:Entity)-[r:RELATES_TO]->(d:Entity {id: did})
        WHERE m.id <> $canonical_id AND NOT m.id IN $dup_ids
        MERGE (m)-[nr:RELATES_TO {type: coalesce(r.type, 'RELATES_TO'),
                                      source_doc: coalesce(r.source_doc, '')}]->(c)
        ON CREATE SET nr.context = r.context,
                      nr.source_doc = r.source_doc,
                      nr.updated_at = r.updated_at
        SET nr.evidence_ids = reduce(u = coalesce(nr.evidence_ids, []),
            id IN coalesce(r.evidence_ids, []) | CASE WHEN id IN u THEN u ELSE u + id END)
        DELETE r
        """
        absorb_query = """
        MATCH (c:Entity {id: $canonical_id})
        OPTIONAL MATCH (d:Entity) WHERE d.id IN $dup_ids
        WITH c, [x IN collect(d) WHERE x IS NOT NULL] AS dups
        WITH c, dups,
             coalesce(c.evidence_ids, [])
               + reduce(acc = [], d IN dups | acc + coalesce(d.evidence_ids, [])) AS raw_evidence,
             coalesce(c.source_docs, [])
               + reduce(acc = [], d IN dups | acc + coalesce(d.source_docs, [])) AS raw_docs,
             coalesce(c.aliases, [])
               + reduce(acc = [], d IN dups | acc + coalesce(d.aliases, []))
               + [d IN dups WHERE d.label IS NOT NULL | d.label] AS raw_aliases,
             reduce(best = coalesce(c.summary, ''), d IN dups |
               CASE WHEN size(coalesce(d.summary, '')) > size(best)
                    THEN coalesce(d.summary, '') ELSE best END) AS best_summary
        WITH c, dups, best_summary,
             reduce(u = [], id IN raw_evidence |
               CASE WHEN id IN u THEN u ELSE u + id END) AS evidence_ids,
             reduce(u = [], s IN raw_docs |
               CASE WHEN s IS NULL OR s IN u THEN u ELSE u + s END) AS source_docs,
             reduce(u = [], s IN raw_aliases |
               CASE WHEN s IS NULL OR s = '' OR s = c.label OR s IN u
                    THEN u ELSE u + s END) AS aliases
        SET c.evidence_ids = evidence_ids,
            c.source_docs = source_docs,
            c.aliases = aliases,
            c.summary = best_summary,
            c.updated_at = datetime()
        FOREACH (_ IN CASE WHEN size(dups) > 0 THEN [1] ELSE [] END |
          REMOVE c:SemanticEntity, c.semantic_embedding, c.embedding,
                 c.embedding_method, c.embedding_model, c.embedding_version,
                 c.embedding_dimension)
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
                await self._lock_graph(tx)
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
            except BaseException:
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
