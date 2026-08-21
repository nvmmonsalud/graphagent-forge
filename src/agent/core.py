"""Core agent loop — orchestrates the full pipeline."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.agent.daytona_exec import DaytonaExecutor
from src.agent.nosana_client import NosanaClient
from src.graph.graphrag import GraphRAGEngine
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.extractor import extract_from_text, extract_from_url
from src.ingestion.graph_writer import ingest_to_graph

log = logging.getLogger(__name__)


class GraphAgent:
    """Main agent — ingests data, builds graphs, answers questions."""

    def __init__(self, neo4j: Neo4jClient):
        self.neo4j = neo4j
        self.graphrag = GraphRAGEngine(neo4j)
        self.daytona = DaytonaExecutor()
        self.nosana = NosanaClient()
        self._ingest_semaphore = asyncio.Semaphore(2)

    def _get_ws_manager(self):
        """Get the WebSocket connection manager from app state (late-binding)."""
        # Lazy import to avoid circular dependency
        try:
            from src.main import ws_manager
            return ws_manager
        except ImportError:
            return None

    async def _broadcast_graph_update(self, nodes: list[dict], edges: list[dict]):
        """Broadcast new graph nodes/edges to connected WebSocket clients."""
        manager = self._get_ws_manager()
        if manager:
            await manager.broadcast({
                "type": "graph_update",
                "nodes": nodes,
                "edges": edges,
            })

    async def _broadcast_new_nodes(self, result: dict, source_doc: str):
        """Fetch the new nodes/edges and broadcast them to WebSocket clients."""
        try:
            new_nodes = await self.neo4j.search_nodes(source_doc, limit=100)
            node_ids = [n["id"] for n in new_nodes]
            if node_ids:
                all_data = await self.neo4j.get_all_graph_data()
                new_edges = [
                    e for e in all_data.get("edges", [])
                    if e.get("source") in node_ids or e.get("target") in node_ids
                ]
            else:
                new_edges = []

            await self._broadcast_graph_update(new_nodes, new_edges)
        except Exception as exc:
            log.warning("Failed to broadcast graph update: %s", exc)

    async def ingest_url(self, url: str) -> dict[str, Any]:
        """Ingest a URL: extract → graph → store."""
        log.info("Ingesting URL: %s", url)

        # Extract content
        extraction = await extract_from_url(url)
        if extraction.get("error"):
            return {"success": False, "error": extraction["error"]}

        # Write to graph
        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=extraction.get("title") or url,
            )

        result["extraction"] = {
            "title": extraction.get("title", ""),
            "domain": extraction.get("domain", ""),
            "char_count": extraction.get("char_count", 0),
        }

        # F2: Generate embedding from source content and store on entities
        try:
            embedding = await self.nosana.get_embedding(extraction["content"])
            await self._store_embedding(extraction.get("title") or url, embedding)
            result["embedding_stored"] = True
            result["embedding_method"] = self.nosana.last_embedding_method
        except Exception as e:
            log.warning("Embedding generation failed: %s", e)
            result["embedding_stored"] = False

        # Broadcast live graph update via WebSocket
        if result.get("success"):
            await self._broadcast_new_nodes(result, extraction.get("title") or url)

        # F1: verify graph integrity in a Daytona sandbox
        if result.get("success"):
            graph_data = await self.neo4j.get_all_graph_data(limit=None)
            result["verification"] = await self.daytona.verify_graph(graph_data)

        return result

    async def ingest_text(self, text: str, source: str = "manual") -> dict[str, Any]:
        """Ingest raw text: extract → graph → store."""
        extraction = extract_from_text(text, source)

        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=source,
            )

        # F2: Generate embedding and store on entities
        try:
            embedding = await self.nosana.get_embedding(extraction["content"])
            await self._store_embedding(source, embedding)
            result["embedding_stored"] = True
            result["embedding_method"] = self.nosana.last_embedding_method
        except Exception as e:
            log.warning("Embedding generation failed: %s", e)
            result["embedding_stored"] = False

        # Broadcast live graph update via WebSocket
        if result.get("success"):
            await self._broadcast_new_nodes(result, source)

        # F1: verify graph integrity in a Daytona sandbox
        if result.get("success"):
            graph_data = await self.neo4j.get_all_graph_data(limit=None)
            result["verification"] = await self.daytona.verify_graph(graph_data)

        return result

    async def _store_embedding(self, source_doc: str, embedding: list[float]):
        """Store a content-level embedding on entities matching source_doc."""
        query = """
        MATCH (n:Entity {source_doc: $source_doc})
        SET n.embedding = $embedding
        """
        async with self.neo4j.driver.session() as session:
            await session.run(query, source_doc=source_doc, embedding=embedding)

    async def ask(self, question: str) -> dict[str, Any]:
        """Answer a question using GraphRAG."""
        return await self.graphrag.query(question)

    async def get_graph_stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self.neo4j.get_stats()

    async def search_graph(self, query: str) -> list[dict]:
        """Search entities in the graph."""
        return await self.neo4j.search_nodes(query)

    async def find_path(self, from_label: str, to_label: str) -> list[dict]:
        """Find shortest path between two entities."""
        return await self.neo4j.find_path(from_label, to_label)
