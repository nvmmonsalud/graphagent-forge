"""Plan or apply semantic re-embedding; dry-run by default, no LLM extraction."""
from __future__ import annotations

import argparse
import asyncio
import json
import os

from dotenv import load_dotenv

from src.agent.nosana_client import NosanaClient
from src.graph.embedding_space import configured_embedding_space
from src.graph.neo4j_client import Neo4jClient


async def reembed(client, provider, *, source_doc=None, apply=False) -> dict:
    """Bound one operator run to 5,000 entities, with content-guarded writes.

    A source can contain merged boundary entities; deduplicate by entity ID.
    Do not replace existing vectors with hash fallbacks if the provider fails.
    """
    if source_doc:
        nodes = await client.get_nodes_by_source(source_doc, limit=5000)
        capped = len(nodes) == 5000
    else:
        data = await client.get_all_graph_data(limit=5000)
        nodes = data["nodes"]
        capped = data.get("total_nodes", len(nodes)) > len(nodes)
    nodes = list({node["id"]: node for node in nodes}.values())
    report = {"apply": apply, "planned": len(nodes), "stored": 0,
              "unavailable": 0, "rejected": 0, "capped": capped,
              "space": configured_embedding_space()}
    if not apply:
        return report
    for node in nodes:
        label, summary = node.get("label") or "", node.get("summary") or ""
        text = f"{label}: {summary}" if label and summary else label or summary
        result = await provider.get_embedding_result(text)
        if result["method"] != "nosana":
            report["unavailable"] += 1
            continue
        stored = await client.set_node_embeddings([{
            "id": node["id"], **result,
            "expected_label": label, "expected_summary": summary,
        }])
        report["stored"] += stored
        report["rejected"] += 1 - stored
    return report


async def run(args) -> int:
    load_dotenv()
    provider = NosanaClient()
    if args.apply and not (provider.api_key and provider.embedding_url):
        print("Configure NOSANA_API_KEY and NOSANA_EMBEDDING_URL before --apply.")
        return 2
    client = Neo4jClient(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                         os.environ["NEO4J_PASSWORD"])
    try:
        await client.connect()
        # Inspection must not quarantine or rewrite an operator's existing data.
        if args.apply:
            await client.init_schema()
        report = await reembed(client, provider, source_doc=args.source, apply=args.apply)
        print(json.dumps(report, indent=2))
        return 1 if report["unavailable"] or report["rejected"] or report["capped"] else 0
    finally:
        await provider.aclose()
        await client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Calls configured embedding provider")
    mode.add_argument("--dry-run", action="store_true", help="Inspect only (default)")
    parser.add_argument("--source", help="Optional source_doc key; run separately for large graphs")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
