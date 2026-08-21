"""GraphRAG query engine — combines graph traversal with LLM reasoning."""
from __future__ import annotations

import logging
from src.ingestion.entity_parser import answer_query
from src.graph.neo4j_client import Neo4jClient

log = logging.getLogger(__name__)


class GraphRAGEngine:
    """Graph-enhanced RAG: retrieve from graph, reason with LLM."""

    def __init__(self, neo4j: Neo4jClient):
        self.neo4j = neo4j

    async def query(self, question: str) -> dict:
        """Full GraphRAG pipeline: search → traverse → reason → answer."""
        # Step 1: Find relevant starting nodes
        search_terms = self._extract_search_terms(question)
        candidates = []
        for term in search_terms:
            results = await self.neo4j.search_nodes(term, limit=5)
            candidates.extend(results)

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

        # Step 3: Reason with LLM using graph context
        answer = await answer_query(question, context)

        return {
            "answer": answer,
            "context_nodes": list(seen_labels),
            "sources": candidates[:5],
        }

    def _extract_search_terms(self, question: str) -> list[str]:
        """Simple keyword extraction from question."""
        # For hackathon: split on common words, keep meaningful tokens
        stopwords = {
            "what", "who", "where", "when", "how", "why", "is", "are", "was",
            "were", "does", "do", "did", "has", "have", "had", "can", "could",
            "would", "should", "the", "a", "an", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "and", "or", "not", "this", "that",
            "these", "those", "it", "its", "about", "tell", "me", "know",
        }
        words = question.lower().split()
        return [w for w in words if w not in stopwords and len(w) > 2]
