"""GraphRAG query engine — combines graph traversal with LLM reasoning."""
from __future__ import annotations

import logging

from src.agent.nosana_client import NosanaClient
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.entity_parser import answer_query

log = logging.getLogger(__name__)


class GraphRAGEngine:
    """Graph-enhanced RAG: retrieve from graph, reason with LLM."""

    def __init__(self, neo4j: Neo4jClient):
        self.neo4j = neo4j
        self.nosana = NosanaClient()

    async def query(self, question: str) -> dict:
        """Full GraphRAG pipeline: vector search → keyword search → traverse → reason → answer."""
        seen_ids: set[str] = set()
        candidates: list[dict] = []

        # Step 0: Vector search first (F3) — semantic similarity
        try:
            embedding = await self.nosana.get_embedding(question)
            if self.nosana.last_embedding_method == "nosana":
                vec_results = await self.neo4j.vector_search(embedding, limit=10)
                for r in vec_results:
                    seen_ids.add(r["id"])
                    candidates.append(r)
                if vec_results:
                    log.info("Vector search returned %d candidates", len(vec_results))
            else:
                log.info("Skipping vector retrieval: semantic Nosana embedding unavailable")
        except Exception as e:
            log.warning("Vector search unavailable, falling back to keyword only: %s", e)

        # Step 1: Keyword search for remaining gaps
        search_terms = self._extract_search_terms(question)
        for term in search_terms:
            results = await self.neo4j.search_nodes(term, limit=8)
            for r in results:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    candidates.append(r)

        if not candidates:
            return {
                "answer": "No relevant entities found in the knowledge graph. "
                          "Try ingesting some data first!",
                "context_nodes": [],
                "sources": [],
            }

        # Step 2: Get neighborhood context for top candidates
        seen_labels = set()
        context_parts = []
        for candidate in candidates[:5]:  # Top 5 unique
            label = candidate["label"]
            if label in seen_labels:
                continue
            seen_labels.add(label)
            ctx = await self.neo4j.get_node_context(label, depth=2)
            if ctx:
                context_parts.append(ctx)

        context = "\n\n".join(context_parts)

        # Collect source_doc from candidates for provenance display
        source_docs = []
        for candidate in candidates[:5]:
            src = candidate.get("source_doc", "")
            if src and src not in source_docs:
                source_docs.append(src)

        # Step 3: Reason with LLM using graph context
        answer = await answer_query(question, context)

        return {
            "answer": answer,
            "context_nodes": list(seen_labels),
            "sources": candidates[:5],
            "source_docs": source_docs,
        }

    def _extract_search_terms(self, question: str) -> list[str]:
        """Keyword extraction from question, with the full question included."""
        stopwords = {
            "what", "who", "where", "when", "how", "why", "is", "are", "was",
            "were", "does", "do", "did", "has", "have", "had", "can", "could",
            "would", "should", "the", "a", "an", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "and", "or", "not", "this", "that",
            "these", "those", "it", "its", "about", "tell", "me", "know",
        }
        words = question.lower().split()
        terms = [w for w in words if w not in stopwords and len(w) > 2]
        # Always include the full question as a search term for better recall
        full = question.strip()
        if full and full not in terms:
            terms.insert(0, full)
        return terms
