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
from src.ingestion.chunks import MAX_DOCUMENT_CHARS
from src.ingestion.entity_parser import _resolve_model
from src.ingestion.extractor import extract_from_text, extract_from_url
from src.ingestion.file_extractor import extract_from_file
from src.ingestion.graph_writer import ingest_to_graph
from src.ingestion.sources import build_source

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
        # Reuse connections; embedding metadata is returned per call.
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

    async def _broadcast_graph_reset(self, source_doc: str):
        """Publication replaces a source graph, so clients must refetch it."""
        try:
            manager = self._get_ws_manager()
            if manager:
                await manager.broadcast({"type": "graph_reset", "source": source_doc})
        except Exception as exc:
            log.warning("Failed to broadcast graph reset: %s", exc)

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
        log.info("Ingesting URL")

        # Extract content
        await self._report(progress, "fetching")
        extraction = await extract_from_url(url, max_chars=MAX_DOCUMENT_CHARS)
        if extraction.get("error"):
            return {"success": False, "error": extraction["error"]}

        model = _resolve_model(None)
        source = build_source(
            "url", url, extraction.get("title") or url, extraction["content"], model=model,
        )
        source["input_truncated"] = extraction.get("input_truncated", False)
        # Write to graph
        await self._report(progress, "extracting")
        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=source["id"],
                source=source,
                model=model,
            )

        result["extraction"] = {
            "title": extraction.get("title", ""),
            "domain": extraction.get("domain", ""),
            "char_count": extraction.get("char_count", 0),
        }

        return await self._post_ingest(
            result,
            source_doc=source["id"],
            content=extraction["content"],
            progress=progress,
        )

    async def ingest_text(
        self, text: str, source: str = "manual", progress: ProgressCb | None = None
    ) -> dict[str, Any]:
        """Ingest raw text: extract → graph → store."""
        extraction = extract_from_text(text, source, max_chars=MAX_DOCUMENT_CHARS)
        model = _resolve_model(None)
        source_metadata = build_source("text", source, source, extraction["content"], model=model)

        source_metadata["input_truncated"] = extraction.get("input_truncated", False)
        await self._report(progress, "extracting")
        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=source_metadata["id"],
                source=source_metadata,
                model=model,
            )

        return await self._post_ingest(
            result,
            source_doc=source_metadata["id"],
            content=extraction["content"],
            progress=progress,
        )

    async def ingest_file(
        self,
        data: bytes,
        filename: str,
        content_type: str | None = None,
        source: str | None = None,
        progress: ProgressCb | None = None,
    ) -> dict[str, Any]:
        """Ingest an uploaded file: parse → extract → graph → store.

        Mirrors `ingest_text` with a `"parsing"` prologue: the uploaded bytes are
        parsed into plain text first (and rejected here on any parse failure), and
        only then does the shared extract → graph → `_post_ingest` tail run.
        """
        await self._report(progress, "parsing")
        extraction = await extract_from_file(
            data, filename, content_type, label=source, max_chars=MAX_DOCUMENT_CHARS,
        )
        if extraction.get("error"):
            return {"success": False, "error": extraction["error"]}

        model = _resolve_model(None)
        source_name = source or extraction.get("filename") or filename
        source_metadata = build_source(
            "file", source_name, extraction.get("title") or source_name, extraction["content"],
            model=model,
        )
        source_metadata["input_truncated"] = extraction.get("input_truncated", False)
        source_doc = source_metadata["id"]

        await self._report(progress, "extracting")
        async with self._ingest_semaphore:
            result = await ingest_to_graph(
                self.neo4j,
                extraction["content"],
                source_doc=source_doc,
                source=source_metadata,
                model=model,
            )

        result["extraction"] = {
            "title": extraction.get("title", ""),
            "domain": extraction.get("domain", ""),
            "char_count": extraction.get("char_count", 0),
            "pages": extraction.get("pages"),
        }

        return await self._post_ingest(
            result,
            source_doc=source_doc,
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

        if not result.get("success") or result.get("unchanged"):
            # Nothing new landed in the graph — skip embedding/broadcast/verify.
            result.setdefault("embedding_stored", False)
            return result

        # F2: per-entity embeddings so the vector index holds distinct vectors
        await self._report(progress, "embedding")
        try:
            methods: set[str] = set()
            stored = await self._embed_source_nodes(source_doc, methods=methods)
            result["embedding_stored"] = stored > 0
            result["embedding_method"] = (
                next(iter(methods)) if len(methods) == 1 else "mixed" if methods else None
            )
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
        if result.get("source_metadata"):
            await self._broadcast_graph_reset(source_doc)
        else:
            await self._broadcast_new_nodes(source_doc)

        # F1: verify integrity of just this document's subgraph in a sandbox
        await self._report(progress, "verifying")
        graph_data = await self.neo4j.get_graph_data_by_source(source_doc)
        result["verification"] = await self.daytona.verify_graph(graph_data)

        return result

    async def _embed_source_nodes(
        self, source_doc: str, *, methods: set[str] | None = None,
    ) -> int:
        """Embed each entity of `source_doc` individually and persist the vectors.

        Returns the number of node embeddings written.
        """
        nodes = await self.neo4j.get_nodes_by_source(source_doc, limit=5000)
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
                    embedding_result = await self.nosana.get_embedding_result(text)
                except Exception as exc:
                    log.warning("Embedding failed for node %s: %s", node_id, exc)
                    return None
            if not embedding_result.get("embedding"):
                return None
            return {
                **embedding_result, "id": node_id,
                "expected_label": node.get("label") or "",
                "expected_summary": node.get("summary") or "",
            }

        results = await asyncio.gather(*(embed_one(n) for n in nodes))
        items = [item for item in results if item]

        stored = 0
        if items:
            stored = await self.neo4j.set_node_embeddings(items)
            if methods is not None:
                methods.update(item["method"] for item in items)
            log.info("Stored %d/%d node embeddings for '%s'", stored, len(nodes), source_doc)

        return stored

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
                embedding_result = await self.nosana.get_embedding_result(text)
                if embedding_result.get("embedding"):
                    await self.neo4j.set_node_embeddings(
                        [{
                            **embedding_result, "id": canonical["id"],
                            "expected_label": canonical.get("label") or "",
                            "expected_summary": canonical.get("summary") or "",
                        }],
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

    async def get_analytics(self, top: int = 10) -> dict[str, Any]:
        """Graph analytics: an always-available Cypher tier plus a structural tier.

        The Cypher tier (type histograms, top degree, isolated count) is plain
        Cypher — no GDS, no APOC — so `totals`/`node_types`/`edge_types`/
        `top_degree` are always present. The structural tier (components,
        PageRank, betweenness, clustering) runs in the Daytona sandbox (or its
        local fallback) and keeps that runner's two-shape envelope: on failure
        `structure` is `{"ok": False, "error": ..., ...}` with no metric keys,
        mirroring how `/graph/duplicates` degrades via `tier2_reason`.
        """
        cypher = await self.neo4j.get_analytics(top=top)
        # Uncapped, like /graph/verify: structural metrics on a truncated graph
        # would report components that are artifacts of the cap.
        graph_data = await self.neo4j.get_all_graph_data(limit=None)
        structure = await self.daytona.analyze_graph(graph_data)

        nodes = graph_data.get("total_nodes", len(graph_data.get("nodes", [])))
        edges = graph_data.get("total_edges", len(graph_data.get("edges", [])))
        density = round(2 * edges / (nodes * (nodes - 1)), 5) if nodes > 1 else 0.0

        if structure.get("ok"):
            shaped = {
                "ok": True,
                "method": structure.get("method"),
                "duration_ms": structure.get("duration_ms"),
                "components": structure.get("components"),
                # The script emits a generous top-N; trim to what was asked.
                "top_pagerank": (structure.get("top_pagerank") or [])[:top],
                "top_betweenness": (structure.get("top_betweenness") or [])[:top],
                "avg_clustering": structure.get("avg_clustering"),
                "betweenness_reason": structure.get("betweenness_reason"),
            }
            if structure.get("skipped"):
                shaped["skipped"] = True
                shaped["reason"] = structure.get("reason")
            structure = shaped

        return {
            "totals": {
                "nodes": nodes,
                "edges": edges,
                "density": density,
                "isolated_nodes": cypher["isolated_nodes"],
            },
            "node_types": cypher["node_types"],
            "edge_types": cypher["edge_types"],
            "top_degree": cypher["top_degree"],
            "structure": structure,
        }

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
