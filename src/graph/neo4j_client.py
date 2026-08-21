"""Neo4j graph client — connection, schema, and queries."""
from __future__ import annotations

import logging
from typing import Any

from neo4j import AsyncGraphDatabase

log = logging.getLogger(__name__)


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
            "CREATE INDEX IF NOT EXISTS FOR (n:Entity) ON (n.source_doc)",
        ]
        async with self.driver.session() as session:
            for q in queries:
                await session.run(q)
        await self.init_vector_index()
        log.info("Neo4j schema initialized")

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
            n.source_doc = $source_doc,
            n.updated_at = datetime()
        """
        props = node.get("properties", {})
        async with self.driver.session() as session:
            await session.run(
                query,
                id=node["id"],
                label=node.get("label", ""),
                type=node.get("type", "Unknown"),
                summary=props.get("summary", ""),
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
        """
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        if nodes:
            batch_nodes = [
                {
                    "id": n["id"],
                    "label": n.get("label", ""),
                    "type": n.get("type", "Unknown"),
                    "summary": n.get("properties", {}).get("summary", ""),
                }
                for n in nodes
            ]
            node_query = """
            UNWIND $nodes AS node
            MERGE (n:Entity {id: node.id})
            SET n.label = node.label,
                n.type = node.type,
                n.summary = node.summary,
                n.source_doc = $source_doc,
                n.updated_at = datetime()
            """
            async with self.driver.session() as session:
                await session.run(node_query, nodes=batch_nodes, source_doc=source_doc)

        if edges:
            batch_edges = [
                {
                    "source": e["source"],
                    "target": e["target"],
                    "rel_type": e.get("relationship", "RELATES_TO"),
                    "context": e.get("properties", {}).get("context", ""),
                }
                for e in edges
            ]
            edge_query = """
            UNWIND $edges AS edge
            MATCH (a:Entity {id: edge.source})
            MATCH (b:Entity {id: edge.target})
            MERGE (a)-[r:RELATES_TO {type: edge.rel_type}]->(b)
            SET r.context = edge.context,
                r.source_doc = $source_doc,
                r.updated_at = datetime()
            """
            async with self.driver.session() as session:
                await session.run(edge_query, edges=batch_edges, source_doc=source_doc)

        log.info("Graph written: %d nodes, %d edges", len(nodes), len(edges))
        return {"nodes_written": len(nodes), "edges_written": len(edges)}

    # ------------------------------------------------------------------
    # Read / query operations
    # ------------------------------------------------------------------
    async def get_node_context(self, label: str, depth: int = 2) -> str:
        """Get a node and its N-hop neighborhood as text context for GraphRAG."""
        # Neo4j requires a literal bound for variable-length patterns.
        # Clamp the caller-provided value before safe interpolation.
        safe_depth = max(1, min(int(depth), 4))
        query = f"""
        MATCH (n:Entity {{label: $label}})
        MATCH path = (n)-[r*1..{safe_depth}]-(m:Entity)
        RETURN n.label AS center, n.type AS center_type, n.summary AS center_summary,
               [rel in r | type(rel)] AS rel_types,
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
        """Full-text search across entity labels and summaries."""
        query = """
        MATCH (n:Entity)
        WHERE toLower(n.label) CONTAINS toLower($q)
           OR toLower(n.summary) CONTAINS toLower($q)
        RETURN n.id AS id, n.label AS label, n.type AS type,
               n.summary AS summary, n.source_doc AS source_doc
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, q=query_text, limit=limit)
            return [dict(record) async for record in result]

    async def vector_search(self, embedding: list[float], limit: int = 10) -> list[dict]:
        """Find entities by cosine similarity against a pre-computed embedding vector."""
        query = """
        MATCH (n:Entity)
        SEARCH n IN (
            VECTOR INDEX entity_embedding
            FOR $embedding
            LIMIT $limit
        ) SCORE AS score
        RETURN n.id AS id, n.label AS label, n.type AS type,
               n.summary AS summary, n.source_doc AS source_doc, score
        ORDER BY score DESC
        """
        async with self.driver.session() as session:
            result = await session.run(query, embedding=embedding, limit=limit)
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

    async def find_path(self, from_label: str, to_label: str) -> list[dict]:
        """Find shortest path between two entities using Cypher shortestPath."""
        query = """
        MATCH (a:Entity), (b:Entity)
        WHERE toLower(a.label) = toLower($from_label)
          AND toLower(b.label) = toLower($to_label)
        MATCH path = shortestPath((a)-[*]-(b))
        RETURN [n IN nodes(path) | {
            id: n.id, label: n.label, type: n.type, summary: n.summary
        }] AS path_nodes,
        [r IN relationships(path) | {
            source: startNode(r).id,
            target: endNode(r).id,
            type: type(r),
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
        """Get graph data for visualization or full integrity checks."""
        safe_limit = max(1, min(int(limit), 5000)) if limit is not None else None
        node_limit = f"LIMIT {safe_limit}" if safe_limit is not None else ""
        edge_limit = f"LIMIT {safe_limit * 2}" if safe_limit is not None else ""
        nodes_query = f"""
        MATCH (n:Entity)
        RETURN n.id AS id, n.label AS label, n.type AS type,
               n.summary AS summary, n.source_doc AS source_doc
        {node_limit}
        """
        edges_query = f"""
        MATCH (a:Entity)-[r]->(b:Entity)
        RETURN a.id AS source, b.id AS target, type(r) AS type
        {edge_limit}
        """
        async with self.driver.session() as session:
            nodes_result = await session.run(nodes_query)
            nodes = [dict(record) async for record in nodes_result]

            edges_result = await session.run(edges_query)
            edges = [dict(record) async for record in edges_result]

        return {"nodes": nodes, "edges": edges}
