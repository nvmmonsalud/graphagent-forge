"""Graph writer — orchestrates ingestion into Neo4j."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from src.graph.neo4j_client import Neo4jClient
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


def prefix_and_validate(graph_data: dict, source_doc: str) -> tuple[dict, int]:
    """Apply the md5 doc-id prefix and drop edges referencing undeclared ids.

    Node ids become ``<md5(source_doc)[:8]>_<raw id>`` so two documents that
    happen to reuse the same local id never collide, and edge endpoints are
    remapped to match. Any edge still pointing at an id that was never declared
    as a node (an LLM hallucination, or a fixture typo) is dropped.

    `graph_data` is mutated in place and returned alongside the dropped count,
    so both the live ingest path and the offline seeder share one definition of
    "what a document's ids look like".

    Returns ``(graph_data, dropped_edges)``.
    """
    doc_hash = hashlib.md5(source_doc.encode()).hexdigest()[:8]

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
) -> dict[str, Any]:
    """Full pipeline: extract entities → write to Neo4j."""
    log.info("Extracting entities from '%s' (%d chars)", source_doc, len(content))

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
    graph_data, dropped_edges = prefix_and_validate(graph_data, source_doc)
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

    # Step 2: Write to Neo4j
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
