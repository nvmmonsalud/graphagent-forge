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

    #: Max concurrent embedding calls issued while embedding a document's nodes.
    EMBED_CONCURRENCY = 4

    def __init__(self, neo4j: Neo4jClient):
        self.neo4j = neo4j
        self.nosana = NosanaClient()
        # Share a single NosanaClient so `last_embedding_method` has one
        # source of truth across ingestion and query paths.
        self.graphrag = GraphRAGEngine(neo4j, nosana=self.nosana)
        self.daytona = DaytonaExecutor()
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

    async def _broadcast_new_nodes(self, source_doc: str):
        """Fetch this document's nodes/edges and broadcast them to WebSocket clients."""
        try:
            data = await self.neo4j.get_graph_data_by_source(source_doc)
            await self._broadcast_graph_update(
                data.get("nodes", []),
                data.get("edges", []),
            )
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

        return await self._post_ingest(
            result,
            source_doc=extraction.get("title") or url,
            content=extraction["content"],
        )

    async def ingest_text(self, text: str, source: str = "manual") -> dict[str, Any]:
        """Ingest raw text: extract → graph → store."""
        extraction = extract_from_text(text, source)

        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=source,
            )

        return await self._post_ingest(
            result,
            source_doc=source,
            content=extraction["content"],
        )

    async def _post_ingest(self, result: dict, source_doc: str, content: str) -> dict:
        """Shared ingest tail: embed entities → broadcast → verify.

        Mutates and returns `result` so callers keep whatever extraction
        metadata they already attached.
        """
        log.debug("Post-ingest for '%s' (%d chars of source content)", source_doc, len(content))

        if not result.get("success"):
            # Nothing new landed in the graph — skip embedding/broadcast/verify.
            result.setdefault("embedding_stored", False)
            return result

        # F2: per-entity embeddings so the vector index holds distinct vectors
        try:
            stored = await self._embed_source_nodes(source_doc)
            result["embedding_stored"] = stored > 0
            result["embedding_method"] = self.nosana.last_embedding_method
        except Exception as e:
            log.warning("Embedding generation failed: %s", e)
            result["embedding_stored"] = False

        # Broadcast live graph update via WebSocket
        await self._broadcast_new_nodes(source_doc)

        # F1: verify integrity of just this document's subgraph in a sandbox
        graph_data = await self.neo4j.get_graph_data_by_source(source_doc)
        result["verification"] = await self.daytona.verify_graph(graph_data)

        return result

    async def _embed_source_nodes(self, source_doc: str) -> int:
        """Embed each entity of `source_doc` individually and persist the vectors.

        Returns the number of node embeddings written.
        """
        nodes = await self.neo4j.get_nodes_by_source(source_doc)
        if not nodes:
            log.info("No nodes found for '%s' — nothing to embed", source_doc)
            return 0

        semaphore = asyncio.Semaphore(self.EMBED_CONCURRENCY)

        async def embed_one(node: dict) -> dict | None:
            node_id = node.get("id")
            if not node_id:
                return None
            label = (node.get("label") or "").strip()
            summary = (node.get("summary") or "").strip()
            text = f"{label}: {summary}" if (label and summary) else (label or summary)
            if not text:
                return None
            async with semaphore:
                try:
                    embedding = await self.nosana.get_embedding(text)
                except Exception as exc:
                    log.warning("Embedding failed for node %s: %s", node_id, exc)
                    return None
            if not embedding:
                return None
            return {"id": node_id, "embedding": embedding}

        results = await asyncio.gather(*(embed_one(n) for n in nodes))
        items = [item for item in results if item]

        if items:
            await self.neo4j.set_node_embeddings(items)
            log.info("Stored %d/%d node embeddings for '%s'", len(items), len(nodes), source_doc)

        return len(items)

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
