"""GraphRAG query engine — combines graph traversal with LLM reasoning."""
from __future__ import annotations

import asyncio
import logging

from src.agent.nosana_client import NosanaClient, _is_finite_embedding
from src.graph.embedding_space import configured_embedding_space
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.entity_parser import LLM_UNAVAILABLE_PREFIX, answer_query

log = logging.getLogger(__name__)


class GraphRAGEngine:
    """Graph-enhanced RAG: retrieve from graph, reason with LLM."""

    def __init__(self, neo4j: Neo4jClient, nosana: NosanaClient | None = None):
        self.neo4j = neo4j
        # Reuse the application client; retrieval relies on per-call metadata.
        self.nosana = nosana or NosanaClient()

    async def query(self, question: str) -> dict:
        """Full GraphRAG pipeline: vector search → keyword search → traverse → reason → answer."""
        seen_ids: set[str] = set()
        candidates: list[dict] = []

        # Step 0: Vector search first (F3) — semantic similarity
        try:
            result = await self.nosana.get_embedding_result(question)
            space = configured_embedding_space()
            if (
                result["method"] == "nosana"
                and all(result.get(key) == value for key, value in space.items())
                and _is_finite_embedding(result["embedding"])
            ):
                vec_results = await self.neo4j.vector_search(
                    result["embedding"], limit=10,
                    model=result["model"], version=result["version"],
                )
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
                "status": "no_context",
                "evidence": [],
            }

        # Step 2: Get neighborhood context for top candidates, fetched in parallel
        selected = candidates[:5]
        contexts = await asyncio.gather(
            *[self.neo4j.get_entity_context(candidate["id"], depth=2) for candidate in selected]
        )
        evidence = [ctx for ctx in contexts if ctx.get("text")]
        if not evidence:
            return {
                "answer": "No relevant entity context found in the knowledge graph.",
                "context_nodes": [], "sources": [], "source_docs": [],
                "status": "no_context", "evidence": [],
            }
        context = "\n\n".join(ctx["text"] for ctx in evidence)
        # Preserve the historical label list for display, but never use labels
        # as graph identities: two selected entities may share the same label.
        labels = list(dict.fromkeys(candidate["label"] for candidate in selected))
        source_docs = list(dict.fromkeys(
            source for ctx in evidence for source in ctx.get("source_docs", []) if source
        ))

        # Step 3: Reason with LLM using graph context
        answer = await answer_query(question, context)

        return {
            "answer": answer,
            "context_nodes": labels,
            "sources": candidates[:5],
            "source_docs": source_docs,
            "evidence": evidence,
            # `answer_query` returns a plain string; a failure notice is flagged by
            # its fixed prefix so an "LLM unavailable" message is never presented
            # as a real answer.
            "status": (
                "llm_unavailable"
                if answer.startswith(LLM_UNAVAILABLE_PREFIX)
                else "answered"
            ),
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
