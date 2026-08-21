"""Core agent loop — orchestrates the full pipeline."""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from src.agent.daytona_exec import DaytonaExecutor
from src.agent.nosana_client import NosanaClient
from src.graph.graphrag import GraphRAGEngine
from src.graph.neo4j_client import Neo4jClient
from src.ingestion.extractor import extract_from_text, extract_from_url
from src.ingestion.graph_writer import ingest_to_graph

log = logging.getLogger(__name__)

#: Optional async callback invoked with a stage name as ingestion progresses.
ProgressCb = Callable[[str], Awaitable[None]]


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

    async def _broadcast_graph_merge(self, res: dict):
        """Broadcast a completed entity merge to connected WebSocket clients."""
        manager = self._get_ws_manager()
        if manager:
            await manager.broadcast({
                "type": "graph_merge",
                "removed_ids": res["removed_ids"],
                "canonical": res["canonical"],
                "edges": res["edges"],
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

    async def _report(self, progress: ProgressCb | None, stage: str) -> None:
        """Notify an optional progress callback; a broken callback never fails an ingest."""
        if progress is None:
            return
        try:
            await progress(stage)
        except Exception as exc:
            log.warning("Progress callback failed for stage %s: %s", stage, exc)

    async def ingest_url(self, url: str, progress: ProgressCb | None = None) -> dict[str, Any]:
        """Ingest a URL: extract → graph → store."""
        log.info("Ingesting URL: %s", url)

        # Extract content
        await self._report(progress, "fetching")
        extraction = await extract_from_url(url)
        if extraction.get("error"):
            return {"success": False, "error": extraction["error"]}

        # Write to graph
        await self._report(progress, "extracting")
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
            progress=progress,
        )

    async def ingest_text(
        self, text: str, source: str = "manual", progress: ProgressCb | None = None
    ) -> dict[str, Any]:
        """Ingest raw text: extract → graph → store."""
        extraction = extract_from_text(text, source)

        await self._report(progress, "extracting")
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
            progress=progress,
        )

    async def _post_ingest(
        self,
        result: dict,
        source_doc: str,
        content: str,
        progress: ProgressCb | None = None,
    ) -> dict:
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
        await self._report(progress, "embedding")
        try:
            stored = await self._embed_source_nodes(source_doc)
            result["embedding_stored"] = stored > 0
            result["embedding_method"] = self.nosana.last_embedding_method
        except Exception as e:
            log.warning("Embedding generation failed: %s", e)
            result["embedding_stored"] = False

        # Optional exact-label auto-merge, before broadcast/verification so both
        # observe the post-merge graph. The canonical keeps this document's
        # membership through the source_docs union, so the source-scoped reads
        # below still return it.
        if os.getenv("AUTO_MERGE", "").lower() == "exact":
            await self._report(progress, "merging")
            try:
                groups = await self.neo4j.find_duplicate_groups(limit=50, source_doc=source_doc)
                merged = []
                for g in groups:
                    r = await self.merge_entities([n["id"] for n in g["nodes"]])
                    if "error" not in r:
                        merged.append({"canonical_id": r["canonical_id"],
                                       "label": r["canonical"]["label"], "merged": r["merged"]})
                if merged:
                    result["merged_entities"] = merged
            except Exception as exc:
                log.warning("Auto-merge failed: %s", exc)

        # Broadcast live graph update via WebSocket
        await self._report(progress, "broadcasting")
        await self._broadcast_new_nodes(source_doc)

        # F1: verify integrity of just this document's subgraph in a sandbox
        await self._report(progress, "verifying")
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
            await self.neo4j.set_node_embeddings(
                items, method=self.nosana.last_embedding_method
            )
            log.info("Stored %d/%d node embeddings for '%s'", len(items), len(nodes), source_doc)

        return len(items)

    async def merge_entities(
        self, node_ids: list[str], canonical_id: str | None = None
    ) -> dict[str, Any]:
        """Merge duplicate entities into one canonical node, then re-embed and broadcast."""
        ids = list(dict.fromkeys(node_ids))

        res = await self.neo4j.merge_nodes(ids, canonical_id)
        if not res["ok"]:
            return {"error": "not_found", "missing": res["missing"]}

        # The canonical's summary may have changed — refresh its embedding.
        # A failure here must never fail the merge (mirrors `_post_ingest`).
        try:
            canonical = res["canonical"]
            label = (canonical.get("label") or "").strip()
            summary = (canonical.get("summary") or "").strip()
            text = f"{label}: {summary}" if (label and summary) else (label or summary)
            if text:
                embedding = await self.nosana.get_embedding(text)
                if embedding:
                    await self.neo4j.set_node_embeddings(
                        [{"id": canonical["id"], "embedding": embedding}],
                        method=self.nosana.last_embedding_method,
                    )
        except Exception as exc:
            log.warning("Re-embedding merged entity failed: %s", exc)

        await self._broadcast_graph_merge(res)

        return {
            "merged": res["merged"],
            "canonical_id": res["canonical_id"],
            "aliases": res["aliases"],
            "removed_ids": res["removed_ids"],
            "canonical": res["canonical"],
        }

    async def find_duplicates(self, limit: int = 20) -> dict[str, Any]:
        """Suggest merge candidates: tier 1 = exact normalized label, tier 2 = vector similarity."""
        groups = [{"norm_label": g["norm_label"], "tier": 1, "nodes": g["nodes"]}
                  for g in await self.neo4j.find_duplicate_groups(limit=limit)]
        sim = await self.neo4j.suggest_similar()
        groups += [{"norm_label": None, "tier": 2, "similarity": p["similarity"],
                    "nodes": p["nodes"]}
                   for p in sim["pairs"]]
        return {"groups": groups, "tier2_reason": sim.get("reason")}

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
