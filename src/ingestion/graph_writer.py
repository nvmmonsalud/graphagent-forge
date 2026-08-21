"""Graph writer — orchestrates ingestion into Neo4j."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from src.graph.neo4j_client import Neo4jClient
from src.ingestion.entity_parser import extract_entities

log = logging.getLogger(__name__)


async def ingest_to_graph(
    neo4j: Neo4jClient,
    content: str,
    source_doc: str = "",
    model: str = "kimi-k2.7-code-highspeed",
) -> dict[str, Any]:
    """Full pipeline: extract entities → write to Neo4j."""
    log.info("Extracting entities from '%s' (%d chars)", source_doc, len(content))

    # Step 1: Extract entities and relationships via Kimi
    graph_data = await extract_entities(content, model=model)

    # Prefix IDs with a doc hash to prevent cross-document collisions
    doc_hash = hashlib.md5(source_doc.encode()).hexdigest()[:8]

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

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
        "extracted_edges": len(edges),
    }
