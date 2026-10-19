"""GraphRAG query engine — combines graph traversal with LLM reasoning."""
from __future__ import annotations

import asyncio
import logging

from src.agent.nosana_client import NosanaClient, _is_finite_embedding
from src.graph.citations import (
    bounded_evidence,
    citation_metadata,
    evidence_unchanged,
    validate_answer,
)
from src.graph.embedding_space import configured_embedding_space
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.entity_parser import LLM_UNAVAILABLE_PREFIX, answer_with_citations

log = logging.getLogger(__name__)


class GraphRAGEngine:
    """Graph-enhanced RAG: retrieve from graph, reason with LLM."""

    def __init__(self, neo4j: Neo4jClient, nosana: NosanaClient | None = None):
        self.neo4j = neo4j
        # Reuse the application client; retrieval relies on per-call metadata.
        self.nosana = nosana or NosanaClient()

    async def query(self, question: str, source_id: str | None = None) -> dict:
        """Full GraphRAG pipeline: vector search → keyword search → traverse → reason → answer."""
        seen_ids: set[str] = set()
        candidates: list[dict] = []
        semantic_method = "source_filter" if source_id is not None else "unavailable"

        # Source-specific questions retrieve only original excerpts from that source.
        # Avoid global entity summaries whose merged provenance can span sources.
        if source_id is None:
            # Step 0: Vector search first (F3) — semantic similarity
            try:
                result = await self.nosana.get_embedding_result(question)
                semantic_method = result.get("method", "unavailable")
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

        # Excerpt search must run even when there are no matching entities.
        selected = candidates[:5]
        contexts = await asyncio.gather(
            *[self.neo4j.get_entity_context(candidate["id"], depth=2) for candidate in selected]
        )
        graph_evidence = [ctx for ctx in contexts if ctx.get("text")]
        labels = list(dict.fromkeys(candidate["label"] for candidate in selected))
        options = {"entity_ids": [candidate["id"] for candidate in selected], "limit": 6}
        if source_id is not None:
            options["source_id"] = source_id
        retrieved = await self.neo4j.search_evidence(question, **options)
        # Defense in depth: even a regressed storage adapter cannot mix source
        # excerpts into the provider prompt for an explicitly filtered question.
        chunks = bounded_evidence([
            chunk for chunk in retrieved
            if source_id is None or chunk.get("source_id") == source_id
        ])
        diagnostics = dict(getattr(retrieved, "diagnostics", {}))
        diagnostics["semantic_method"] = semantic_method
        diagnostics["ranks"] = [
            {"id": chunk["id"], **chunk.get("retrieval", {})} for chunk in chunks
        ]
        source_docs = list(dict.fromkeys(
            [source for ctx in graph_evidence for source in ctx.get("source_docs", []) if source]
            + [chunk["source_id"] for chunk in chunks]
        ))
        base = {
            "context_nodes": labels, "sources": selected, "source_docs": source_docs,
            "retrieval_diagnostics": diagnostics,
            "evidence": graph_evidence, "citations": [], "conflicts": [],
            "retrieved_evidence": [citation_metadata(chunk) for chunk in chunks],
        }
        insufficient = {
            **base, "status": "insufficient_evidence",
            "answer": "Insufficient original source evidence to answer this question.",
        }
        if not chunks:
            if not graph_evidence:
                return {
                    **base, "status": "no_context",
                    "answer": "No relevant original source evidence found. "
                              "Try ingesting data first.",
                }
            return insufficient

        generated = await answer_with_citations(question, chunks)
        if isinstance(generated, dict) and generated.get("error"):
            return {
                **base, "status": "llm_unavailable",
                "answer": f"{LLM_UNAVAILABLE_PREFIX} The answer provider is unavailable.",
            }
        if not validate_answer(generated, chunks):
            return insufficient
        # Revalidate the complete supplied set after generation: existence alone
        # cannot authorize evidence from a superseded or deleted source version.
        current = await self.neo4j.validate_evidence_ids([chunk["id"] for chunk in chunks])
        if not evidence_unchanged(chunks, current):
            return insufficient
        if generated["insufficient_evidence"]:
            return {**insufficient, "conflicts": generated["conflicts"]}
        by_id = {chunk["id"]: chunk for chunk in chunks}
        return {
            **base, "answer": generated["answer"], "status": "answered",
            "citations": [citation_metadata(by_id[key]) for key in generated["citations"]],
            "conflicts": generated["conflicts"],
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
