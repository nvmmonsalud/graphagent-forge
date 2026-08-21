"""Core agent loop — orchestrates the full pipeline."""
from __future__ import annotations

import logging
from typing import Any

from src.graph.neo4j_client import Neo4jClient
from src.graph.graphrag import GraphRAGEngine
from src.ingestion.extractor import extract_from_url, extract_from_text
from src.ingestion.graph_writer import ingest_to_graph
from src.agent.daytona_exec import DaytonaExecutor
from src.agent.nosana_client import NosanaClient

log = logging.getLogger(__name__)


class GraphAgent:
    """Main agent — ingests data, builds graphs, answers questions."""

    def __init__(self, neo4j: Neo4jClient):
        self.neo4j = neo4j
        self.graphrag = GraphRAGEngine(neo4j)
        self.daytona = DaytonaExecutor()
        self.nosana = NosanaClient()

    async def ingest_url(self, url: str) -> dict[str, Any]:
        """Ingest a URL: extract → graph → store."""
        log.info("Ingesting URL: %s", url)

        # Extract content
        extraction = await extract_from_url(url)
        if extraction.get("error"):
            return {"success": False, "error": extraction["error"]}

        # Write to graph
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
        return result

    async def ingest_text(self, text: str, source: str = "manual") -> dict[str, Any]:
        """Ingest raw text: extract → graph → store."""
        extraction = extract_from_text(text, source)

        result = await ingest_to_graph(
            self.neo4j,
            extraction["content"],
            source_doc=source,
        )
        return result

    async def ask(self, question: str) -> dict[str, Any]:
        """Answer a question using GraphRAG."""
        return await self.graphrag.query(question)

    async def get_graph_stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self.neo4j.get_stats()

    async def search_graph(self, query: str) -> list[dict]:
        """Search entities in the graph."""
        return await self.neo4j.search_nodes(query)
