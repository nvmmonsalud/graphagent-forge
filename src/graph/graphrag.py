"""GraphRAG query engine — combines graph traversal with LLM reasoning."""
from __future__ import annotations

import asyncio
import logging

from src.agent.nosana_client import NosanaClient
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.entity_parser import answer_query

log = logging.getLogger(__name__)


class GraphRAGEngine:
    """Graph-enhanced RAG: retrieve from graph, reason with LLM."""

    def __init__(self, neo4j: Neo4jClient, nosana: NosanaClient | None = None):
        self.neo4j = neo4j
        # Share the agent's client when one is passed so `last_embedding_method`
        # has a single source of truth; otherwise stand up our own.
        self.nosana = nosana or NosanaClient()

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

        # Step 1: Keyword search for remaining gaps — all terms fan out in parallel
        search_terms = self._extract_search_terms(question)
        if search_terms:
            term_results = await asyncio.gather(
                *[self.neo4j.search_nodes(t, limit=8) for t in search_terms]
            )
            for results in term_results:
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
                "source_docs": [],
            }

        # Step 2: Get neighborhood context for top candidates, fetched in parallel
        seen_labels: set[str] = set()
        labels: list[str] = []
        for candidate in candidates[:5]:  # Top 5 unique
            label = candidate["label"]
            if label in seen_labels:
                continue
            seen_labels.add(label)
            labels.append(label)

        contexts = await asyncio.gather(
            *[self.neo4j.get_node_context(label, depth=2) for label in labels]
        )
        context = "\n\n".join(ctx for ctx in contexts if ctx)

        # Collect provenance from candidates — each carries a list of source docs
        # (merged canonicals span several documents).
        source_docs: list[str] = []
        for candidate in candidates[:5]:
            for src in candidate.get("source_docs") or []:
                if src and src not in source_docs:
                    source_docs.append(src)

        # Step 3: Reason with LLM using graph context
        answer = await answer_query(question, context)

        return {
            "answer": answer,
            "context_nodes": labels,
            "sources": candidates[:5],
            "source_docs": source_docs,
        }

    def _extract_search_terms(self, question: str) -> list[str]:
        """Keyword extraction from a question — stopwords dropped, capped at 6 terms."""
        stopwords = {
            "what", "who", "where", "when", "how", "why", "is", "are", "was",
            "were", "does", "do", "did", "has", "have", "had", "can", "could",
            "would", "should", "the", "a", "an", "in", "on", "at", "to", "for",
            "of", "with", "by", "from", "and", "or", "not", "this", "that",
            "these", "those", "it", "its", "about", "tell", "me", "know",
        }
        words = [w.strip(".,!?;:\"'()[]") for w in question.lower().split()]
        terms: list[str] = []
        for w in words:
            # The full question is deliberately NOT included — a CONTAINS match
            # against it can never hit a label or summary.
            if w in stopwords or len(w) <= 2 or w in terms:
                continue
            terms.append(w)
            if len(terms) == 6:
                break
        return terms
