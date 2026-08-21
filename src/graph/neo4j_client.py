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
        log.info("Neo4j schema initialized")

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
        """Write a complete graph (nodes + edges) from extraction output."""
        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        for node in nodes:
            await self.upsert_node(node, source_doc)

        for edge in edges:
            await self.upsert_edge(edge, source_doc)

        log.info("Graph written: %d nodes, %d edges", len(nodes), len(edges))
        return {"nodes_written": len(nodes), "edges_written": len(edges)}

    # ------------------------------------------------------------------
    # Read / query operations
    # ------------------------------------------------------------------
    async def get_node_context(self, label: str, depth: int = 2) -> str:
        """Get a node and its N-hop neighborhood as text context for GraphRAG."""
        query = """
        MATCH (n:Entity {label: $label})
        MATCH path = (n)-[r*1..%d]-(m:Entity)
        RETURN n.label AS center, n.type AS center_type, n.summary AS center_summary,
               [rel in r | type(rel)] AS rel_types,
               m.label AS neighbor, m.type AS neighbor_type, m.summary AS neighbor_summary
        ORDER BY length(path)
        LIMIT 50
        """ % depth

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
        RETURN n.id AS id, n.label AS label, n.type AS type, n.summary AS summary
        LIMIT $limit
        """
        async with self.driver.session() as session:
            result = await session.run(query, q=query_text, limit=limit)
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

    async def get_all_graph_data(self) -> dict:
        """Get all nodes and edges for visualization."""
        nodes_query = """
        MATCH (n:Entity)
        RETURN n.id AS id, n.label AS label, n.type AS type, n.summary AS summary
        LIMIT 500
        """
        edges_query = """
        MATCH (a:Entity)-[r]->(b:Entity)
        RETURN a.id AS source, b.id AS target, type(r) AS type
        LIMIT 1000
        """
        async with self.driver.session() as session:
            nodes_result = await session.run(nodes_query)
            nodes = [dict(record) async for record in nodes_result]

            edges_result = await session.run(edges_query)
            edges = [dict(record) async for record in edges_result]

        return {"nodes": nodes, "edges": edges}
