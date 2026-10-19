"""Graph writer — orchestrates ingestion into Neo4j."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from src.graph.neo4j_client import Neo4jClient
from src.ingestion.chunks import MAX_DOCUMENT_CHARS, plan_chunks
from src.ingestion.entity_parser import extract_entities

log = logging.getLogger(__name__)


# Bounds on what one LLM extraction may write. The model is steerable by the
# document it reads, so its JSON is treated like any other untrusted input.
MAX_NODES = 500
MAX_EDGES = 1000
MAX_ID_LEN = 100
MAX_LABEL_LEN = 200
MAX_TYPE_LEN = 60
MAX_SUMMARY_LEN = 1000
MAX_RELATIONSHIP_LEN = 60
MAX_CONTEXT_LEN = 500


def _clip(value: Any, limit: int) -> str | None:
    """Return `value` as a stripped string clipped to `limit`, or None if it
    isn't a plain string/number (lists, dicts, None, bools are rejected)."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def sanitize_graph(graph_data: dict) -> tuple[dict, int, int]:
    """Coerce an LLM extraction into the shape `write_graph` expects.

    Drops nodes/edges that aren't objects or lack a usable id/label/endpoint,
    clips every string field, drops duplicate node ids, and caps the counts at
    MAX_NODES / MAX_EDGES. A malformed item can no longer crash the Neo4j write
    (turning a business failure into a `failed` job), and an injected document
    can't make one ingest write an unbounded graph.

    Returns ``(graph_data, dropped_nodes, dropped_edges)``.
    """
    raw_nodes = graph_data.get("nodes")
    raw_edges = graph_data.get("edges")
    raw_nodes = raw_nodes if isinstance(raw_nodes, list) else []
    raw_edges = raw_edges if isinstance(raw_edges, list) else []

    nodes: list[dict] = []
    seen: set[str] = set()
    for item in raw_nodes:
        if len(nodes) >= MAX_NODES or not isinstance(item, dict):
            continue
        node_id = _clip(item.get("id"), MAX_ID_LEN)
        label = _clip(item.get("label"), MAX_LABEL_LEN)
        if not node_id or not label or node_id in seen:
            continue
        seen.add(node_id)
        props = item.get("properties")
        summary = _clip(props.get("summary"), MAX_SUMMARY_LEN) if isinstance(props, dict) else None
        nodes.append({
            "id": node_id,
            "label": label,
            "type": _clip(item.get("type"), MAX_TYPE_LEN) or "Unknown",
            "properties": {"summary": summary or ""},
        })

    edges: list[dict] = []
    for item in raw_edges:
        if len(edges) >= MAX_EDGES or not isinstance(item, dict):
            continue
        source = _clip(item.get("source"), MAX_ID_LEN)
        target = _clip(item.get("target"), MAX_ID_LEN)
        if not source or not target:
            continue
        props = item.get("properties")
        context = _clip(props.get("context"), MAX_CONTEXT_LEN) if isinstance(props, dict) else None
        edges.append({
            "source": source,
            "target": target,
            "relationship": _clip(item.get("relationship"), MAX_RELATIONSHIP_LEN) or "RELATES_TO",
            "properties": {"context": context or ""},
        })

    dropped_nodes = len(raw_nodes) - len(nodes)
    dropped_edges = len(raw_edges) - len(edges)
    if dropped_nodes or dropped_edges:
        log.warning(
            "Sanitizer dropped %d node(s) and %d edge(s) from the LLM extraction",
            dropped_nodes,
            dropped_edges,
        )
    graph_data["nodes"] = nodes
    graph_data["edges"] = edges
    return graph_data, dropped_nodes, dropped_edges


def prefix_and_validate(
    graph_data: dict, source_doc: str, version: str | None = None,
) -> tuple[dict, int]:
    """Namespace document IDs and drop edges referencing undeclared IDs.

    New versioned ingestion uses the full SHA-256 of source ID and version.
    Without a version, retain historical seeded IDs for backward compatibility.

    Node ids become ``<md5(source_doc)[:8]>_<raw id>`` so two documents that
    happen to reuse the same local id never collide, and edge endpoints are
    remapped to match. Any edge still pointing at an id that was never declared
    as a node (an LLM hallucination, or a fixture typo) is dropped.

    `graph_data` is mutated in place and returned alongside the dropped count,
    so both the live ingest path and the offline seeder share one definition of
    "what a document's ids look like".

    Returns ``(graph_data, dropped_edges)``.
    """
    doc_hash = (
        hashlib.sha256(f"{source_doc}\0{version}".encode()).hexdigest()
        if version is not None else hashlib.md5(source_doc.encode()).hexdigest()[:8]
    )

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []

    # Prefix node IDs with doc hash
    id_map = {}
    for node in nodes:
        old_id = node.get("id", "")
        new_id = f"{doc_hash}_{old_id}"
        id_map[old_id] = new_id
        node["id"] = new_id

    # Remap edge source/target references
    for edge in edges:
        if edge.get("source") in id_map:
            edge["source"] = id_map[edge["source"]]
        if edge.get("target") in id_map:
            edge["target"] = id_map[edge["target"]]

    # Drop hallucinated edges pointing at ids that were never declared as nodes
    known_ids = set(id_map.values())
    valid_edges = [
        edge
        for edge in edges
        if edge.get("source") in known_ids and edge.get("target") in known_ids
    ]
    dropped_edges = len(edges) - len(valid_edges)
    if dropped_edges:
        log.warning(
            "Dropped %d edge(s) referencing unknown entities in '%s'",
            dropped_edges,
            source_doc,
        )

    graph_data["nodes"] = nodes
    graph_data["edges"] = valid_edges
    return graph_data, dropped_edges


async def ingest_to_graph(
    neo4j: Neo4jClient,
    content: str,
    source_doc: str = "",
    model: str | None = None,
    *,
    source: dict | None = None,
) -> dict[str, Any]:
    """Full pipeline: extract entities → write to Neo4j."""
    log.info("Extracting entities from '%s' (%d chars)", source_doc, len(content))

    if source is not None:
        return await _ingest_source(neo4j, content, source, model)

    # Step 1: Extract entities and relationships via Kimi
    graph_data = await extract_entities(content, model=model)

    if not isinstance(graph_data, dict):
        return {
            "success": False,
            "error": "LLM returned a non-object payload",
            "nodes": 0,
            "edges": 0,
        }

    # Surface extraction failures instead of masking them as "no entities"
    extraction_error = graph_data.get("error")
    if extraction_error:
        log.error("Entity extraction failed for '%s': %s", source_doc, extraction_error)
        return {
            "success": False,
            "error": extraction_error,
            "nodes": 0,
            "edges": 0,
        }

    # Prefix IDs with a doc hash to prevent cross-document collisions, and drop
    # edges the LLM invented endpoints for. Shared with `scripts.seed_graph` so
    # seeded ids are byte-identical to a live ingest of the same document.
    graph_data, _dropped_nodes, invalid_edges = sanitize_graph(graph_data)
    graph_data, dropped_edges = prefix_and_validate(
        graph_data, source_doc,
    )
    dropped_edges += invalid_edges
    nodes = graph_data["nodes"]
    valid_edges = graph_data["edges"]

    if not nodes:
        return {
            "success": False,
            "error": "No entities extracted — check LLM response or input content",
            "nodes": 0,
            "edges": 0,
        }

    # Step 2: Write legacy document-scoped graph.
    result = await neo4j.write_graph(graph_data, source_doc=source_doc)

    log.info(
        "Ingestion complete: %d nodes, %d edges from '%s'",
        result["nodes_written"],
        result["edges_written"],
        source_doc,
    )

    return {
        "success": True,
        "source": source_doc,
        "nodes": result["nodes_written"],
        "edges": result["edges_written"],
        "extracted_nodes": len(nodes),
        "extracted_edges": len(valid_edges),
        "dropped_edges": dropped_edges,
    }


async def _ingest_source(
    neo4j: Neo4jClient, content: str, source: dict, model: str | None,
) -> dict[str, Any]:
    """Extract every planned excerpt, then atomically publish a complete version."""
    source = dict(source)
    current = await neo4j.get_source(source["id"])
    expected_version = current.get("version") if current else None
    if expected_version == source["version"]:
        return {
            "success": True, "source": source["id"], "source_metadata": current,
            "nodes": 0, "edges": 0, "unchanged": True, "version": source["version"],
            "coverage": current.get("coverage", {}),
        }
    captured = min(len(content), MAX_DOCUMENT_CHARS)
    chunks = plan_chunks(content, source["id"], source["version"])
    gate = asyncio.Semaphore(2)

    async def extract(chunk: dict) -> dict:
        async with gate:
            try:
                data = await extract_entities(chunk["text"], model=model)
                if not isinstance(data, dict):
                    return {"error": "LLM returned a non-object payload"}
                if not data.get("error") and (
                    not isinstance(data.get("nodes"), list)
                    or not isinstance(data.get("edges"), list)
                ):
                    return {"error": "LLM returned invalid graph collections"}
                return data
            except Exception:
                log.exception("Chunk extraction failed for source %s", source["id"])
                return {"error": "Source chunk extraction failed"}

    results = await asyncio.gather(*(extract(chunk) for chunk in chunks))
    failed = sum(bool(data.get("error")) for data in results)
    processed = chunks[-1]["end"] if chunks else 0
    coverage = {
        "captured_chars": captured, "processed_chars": processed,
        "skipped_chars": captured - processed, "planned_chunks": len(chunks),
        "completed_chunks": len(chunks) - failed, "failed_chunks": failed,
        "input_truncated": bool(source.get("input_truncated")) or len(content) > captured,
        "partial": (processed < captured or len(content) > captured
                    or bool(source.get("input_truncated"))),
        "graph_capped": False,
    }
    if failed:
        covered = 0
        previous_end = 0
        for chunk, data in zip(chunks, results, strict=True):
            if not data.get("error"):
                covered += chunk["end"] - max(chunk["start"], previous_end)
                previous_end = chunk["end"]
        coverage["processed_chars"] = covered
        coverage["skipped_chars"] = captured - covered
        coverage["partial"] = True
        return {
            "success": False, "source": source["id"], "nodes": 0, "edges": 0,
            "error": "Source chunk extraction failed; previous version preserved",
            "coverage": coverage,
        }
    nodes, edges = [], []
    dropped_edges = 0
    for chunk, data in zip(chunks, results, strict=True):
        raw_nodes = data.get("nodes")
        raw_edges = data.get("edges")
        coverage["graph_capped"] |= (
            isinstance(raw_nodes, list) and len(raw_nodes) > MAX_NODES
        ) or (isinstance(raw_edges, list) and len(raw_edges) > MAX_EDGES)
        data, _, invalid = sanitize_graph(data)
        dropped_edges += invalid
        # Model-provided IDs are local to one extraction, even when repeated
        # across chunks. Never accept model-supplied evidence provenance.
        namespace = f"c{chunk['ordinal']}_"
        for node in data["nodes"]:
            node["id"] = namespace + node["id"]
            node["evidence_ids"] = [chunk["id"]]
        for edge in data["edges"]:
            edge["source"] = namespace + edge["source"]
            edge["target"] = namespace + edge["target"]
            edge["evidence_ids"] = [chunk["id"]]
        nodes.extend(data["nodes"])
        edges.extend(data["edges"])
    coverage["graph_capped"] |= len(nodes) > MAX_NODES or len(edges) > MAX_EDGES
    nodes = nodes[:MAX_NODES]
    known = {node["id"] for node in nodes}
    valid_edges = [e for e in edges if e["source"] in known and e["target"] in known]
    dropped_edges += len(edges) - min(len(valid_edges), MAX_EDGES)
    graph_data = {"nodes": nodes, "edges": valid_edges[:MAX_EDGES], "chunks": chunks}
    graph_data, invalid = prefix_and_validate(graph_data, source["id"], source["version"])
    dropped_edges += invalid
    source["coverage"] = coverage
    result = await neo4j.publish_source_graph(
        graph_data, source=source, expected_version=expected_version,
    )
    if result.get("conflict"):
        return {
            "success": False, "source": source["id"], "conflict": True,
            "error": "Source changed during extraction; retry ingestion", "nodes": 0, "edges": 0,
            "coverage": coverage,
        }
    return {
        "success": True, "source": source["id"], "source_metadata": source,
        "version": source["version"], "unchanged": result.get("unchanged", False),
        "nodes": result["nodes_written"], "edges": result["edges_written"],
        "extracted_nodes": len(nodes), "extracted_edges": len(graph_data["edges"]),
        "dropped_edges": dropped_edges, "coverage": coverage,
    }
