"""Seed the knowledge graph from a checked-in fixture.

Why this exists: the demo has to be rehearsable — and survivable on stage —
with zero API keys. Ingestion needs Kimi; seeding does not. The fixture is a
pre-extracted stand-in for what ``extract_entities`` would have returned, so a
judge can open the app and see a populated graph, ask the scripted question,
and watch a merge bridge two documents without a single credential configured.

Node ids are produced by ``src.ingestion.graph_writer.prefix_and_validate`` —
the same function the live ingest path uses — so a seeded document and a live
re-ingest of that same document write *byte-identical* ids and therefore
collide (MERGE) instead of duplicating.

Usage::

    python -m scripts.seed_graph [--fixture seed/graph.json] [--reset]
                                 [--clear] [--dry-run] [--embed] [-v]

Exit codes: 0 ok · 1 fixture invalid · 2 Neo4j unreachable.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Mirrors src/main.py: .env must be loaded *before* the app modules are
# imported so import-time env reads see it. Every app import in this file is
# therefore deliberately function-local.
load_dotenv()

# Imported after `load_dotenv()` (same ordering rule as src/main.py) so any
# import-time env read sees .env. `Neo4jClient` is bound at module scope on
# purpose: it is the single seam tests patch to run this script offline.
from neo4j.exceptions import Neo4jError, ServiceUnavailable  # noqa: E402

from src.agent.nosana_client import NosanaClient  # noqa: E402
from src.graph.neo4j_client import Neo4jClient  # noqa: E402
from src.graph.normalize import normalize_label  # noqa: E402
from src.ingestion.graph_writer import prefix_and_validate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "seed" / "graph.json"

#: The vocabulary the extraction prompt uses. Anything else renders grey in the
#: frontend, so the fixture is held to the same list.
ENTITY_TYPES = {"Person", "Org", "Concept", "Event", "Place", "Technology"}

EXIT_OK = 0
EXIT_BAD_FIXTURE = 1
EXIT_NO_NEO4J = 2

log = logging.getLogger("seed_graph")


# ----------------------------------------------------------------------
# Fixture loading + validation
# ----------------------------------------------------------------------
class FixtureError(Exception):
    """Raised when the fixture violates a rule the seeder depends on."""


def load_fixture(path: Path) -> dict[str, Any]:
    """Read and fully validate the fixture. Raises FixtureError with the rule."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FixtureError(f"fixture not found: {path}") from exc
    except OSError as exc:
        raise FixtureError(f"fixture unreadable: {path} ({exc})") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FixtureError(
            f"fixture is not valid JSON: {path} (line {exc.lineno}: {exc.msg})"
        ) from exc

    _validate(data, path)
    return data


def _validate(data: Any, path: Path) -> None:
    def bad(rule: str) -> None:
        raise FixtureError(f"{path}: {rule}")

    if not isinstance(data, dict):
        bad("top level must be a JSON object")
    if data.get("version") != 1:
        bad(f"rule 'version == 1' failed (got {data.get('version')!r})")

    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        bad("rule 'documents must be a non-empty list' failed")

    seen_sources: set[str] = set()
    for i, doc in enumerate(documents):
        where = f"documents[{i}]"
        if not isinstance(doc, dict):
            bad(f"rule '{where} must be an object' failed")

        source_doc = doc.get("source_doc")
        if not isinstance(source_doc, str) or not source_doc.strip():
            bad(f"rule '{where}.source_doc must be a non-empty string' failed")
        if source_doc in seen_sources:
            bad(f"rule 'source_doc must be unique' failed (duplicate {source_doc!r})")
        seen_sources.add(source_doc)
        where = f"document {source_doc!r}"

        nodes = doc.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            bad(f"rule '{where}.nodes must be a non-empty list' failed")
        edges = doc.get("edges", [])
        if not isinstance(edges, list):
            bad(f"rule '{where}.edges must be a list' failed")

        ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict):
                bad(f"rule '{where}: every node must be an object' failed")
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id.strip():
                bad(f"rule '{where}: every node needs a non-empty string id' failed")
            if node_id in ids:
                bad(f"rule '{where}: node ids must be unique' failed (duplicate {node_id!r})")
            ids.add(node_id)
            if not isinstance(node.get("label"), str) or not node["label"].strip():
                bad(f"rule '{where}: node {node_id!r} needs a non-empty label' failed")
            node_type = node.get("type")
            if node_type not in ENTITY_TYPES:
                bad(
                    f"rule '{where}: node {node_id!r} type must be one of "
                    f"{'|'.join(sorted(ENTITY_TYPES))}' failed (got {node_type!r})"
                )
            props = node.get("properties", {})
            if not isinstance(props, dict):
                bad(f"rule '{where}: node {node_id!r} properties must be an object' failed")

        for edge in edges:
            if not isinstance(edge, dict):
                bad(f"rule '{where}: every edge must be an object' failed")
            src, dst = edge.get("source"), edge.get("target")
            if not isinstance(edge.get("relationship"), str) or not edge["relationship"].strip():
                bad(f"rule '{where}: edge {src!r}->{dst!r} needs a relationship' failed")
            # The seeder must never rely on prefix_and_validate silently dropping
            # edges: a dangling endpoint in a hand-authored fixture is a typo.
            for role, ref in (("source", src), ("target", dst)):
                if ref not in ids:
                    bad(
                        f"rule 'every edge endpoint must be declared as a node in the "
                        f"same document' failed ({where}: edge {role} {ref!r})"
                    )


# ----------------------------------------------------------------------
# Plan construction (pure — no Neo4j)
# ----------------------------------------------------------------------
def build_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn the fixture into the exact per-document payloads that will be written.

    Uses the production ``prefix_and_validate`` so seeded ids match a live
    ingest of the same source_doc exactly.
    """
    plan: list[dict[str, Any]] = []
    for doc in data["documents"]:
        source_doc = doc["source_doc"]
        # Deep-copy: prefix_and_validate mutates in place, and the caller may
        # still want the untouched fixture (e.g. for a second pass).
        payload = json.loads(json.dumps({"nodes": doc["nodes"], "edges": doc.get("edges", [])}))
        payload, dropped = prefix_and_validate(payload, source_doc)
        plan.append(
            {
                "source_doc": source_doc,
                "graph_data": payload,
                "dropped_edges": dropped,
                "prefix": payload["nodes"][0]["id"].split("_", 1)[0] if payload["nodes"] else "",
            }
        )
    return plan


def fixture_duplicate_groups(data: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Tier-1 groups the fixture itself creates: one normalized label, 2+ documents.

    Mirrors `Neo4jClient.find_duplicate_groups`, which drops any group whose
    members all share an identical provenance set.
    """
    buckets: dict[str, list[tuple[str, str]]] = {}
    for doc in data["documents"]:
        for node in doc["nodes"]:
            buckets.setdefault(normalize_label(node["label"]), []).append(
                (doc["source_doc"], node["label"])
            )

    groups: list[tuple[str, list[str]]] = []
    for norm, members in sorted(buckets.items()):
        if len({src for src, _ in members}) < 2:
            continue
        groups.append((norm, [f"{label!r} [{src}]" for src, label in members]))
    return groups


# ----------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------
def print_plan(data: dict[str, Any], plan: list[dict[str, Any]], dry_run: bool, verbose: bool):
    header = "write plan (dry run — no Neo4j connection opened)" if dry_run else "write plan"
    print(f"\n{header}\n")
    for i, item in enumerate(plan, 1):
        nodes = item["graph_data"]["nodes"]
        edges = item["graph_data"]["edges"]
        types: dict[str, int] = {}
        for node in nodes:
            types[node["type"]] = types.get(node["type"], 0) + 1
        type_summary = ", ".join(f"{t} {c}" for t, c in sorted(types.items()))
        print(f"  [{i}/{len(plan)}] {item['source_doc']}")
        print(f"          id prefix   {item['prefix']}_")
        print(f"          nodes       {len(nodes)}  ({type_summary})")
        print(f"          edges       {len(edges)}")
        if item["dropped_edges"]:
            print(f"          DROPPED     {item['dropped_edges']} edge(s) with unknown endpoints")
        if verbose:
            for node in nodes:
                print(f"            · {node['id']}  {node['label']} ({node['type']})")
            for edge in edges:
                print(
                    f"            → {edge['source']} -[{edge['relationship']}]-> "
                    f"{edge['target']}"
                )
        print()

    groups = fixture_duplicate_groups(data)
    print(f"  tier-1 duplicate groups the fixture creates: {len(groups)}")
    for norm, members in groups:
        print(f"          {norm!r}: {' · '.join(members)}")
    print()


def print_footer(documents: int, nodes: int, edges: int, dup_groups: int, embedded: bool) -> None:
    print(
        f"Seeded {documents} documents · {nodes} nodes · {edges} edges · "
        f"{dup_groups} tier-1 duplicate groups pending review"
    )
    if not embedded:
        print(
            "Tier-2 (semantic) duplicates stay disabled without NOSANA_EMBEDDING_URL "
            "— this is expected."
        )


# ----------------------------------------------------------------------
# Neo4j-backed work
# ----------------------------------------------------------------------
async def _connect():
    """Connect + init schema. Returns the client, or raises for exit code 2."""
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    client = Neo4jClient(
        uri=uri,
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", ""),
    )
    await client.connect()
    # The seeder may well run before the server ever has: create the schema
    # ourselves rather than assuming the lifespan hook did it.
    await client.init_schema()
    return client


async def _embed_documents(client, source_docs: list[str], verbose: bool) -> str:
    """Attach embeddings to the seeded nodes, exactly as ingest would."""
    nosana = NosanaClient()
    method = "unknown"
    try:
        for source_doc in source_docs:
            nodes = await client.get_nodes_by_source(source_doc)
            items = []
            for node in nodes:
                label = (node.get("label") or "").strip()
                summary = (node.get("summary") or "").strip()
                text = f"{label}: {summary}" if (label and summary) else (label or summary)
                if not text:
                    continue
                embedding = await nosana.get_embedding(text)
                if embedding:
                    items.append({"id": node["id"], "embedding": embedding})
            method = nosana.last_embedding_method
            if items:
                await client.set_node_embeddings(items, method=method)
            if verbose:
                print(f"  embedded {len(items)} node(s) for {source_doc!r} via {method}")
    finally:
        await nosana.aclose()
    return method


async def _count_pending_duplicates(client, source_docs: list[str]) -> int:
    """Tier-1 groups touching any of the fixture's sources, as the API would report."""
    try:
        groups = await client.find_duplicate_groups(limit=100)
    except Exception as exc:  # pragma: no cover - defensive, never fails the seed
        log.warning("Could not read duplicate groups: %s", exc)
        return 0
    wanted = set(source_docs)
    return sum(
        1
        for g in groups
        if any(wanted & set(n.get("source_docs") or []) for n in g.get("nodes", []))
    )


async def run(args: argparse.Namespace, data: dict[str, Any], plan: list[dict[str, Any]]) -> int:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    source_docs = [item["source_doc"] for item in plan]

    try:
        client = await _connect()
    except (ServiceUnavailable, Neo4jError, OSError, ValueError) as exc:
        print(f"Neo4j unreachable at {uri}: {exc}", file=sys.stderr)
        print(
            "Set NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD, or re-run with --dry-run "
            "to validate the fixture offline.",
            file=sys.stderr,
        )
        return EXIT_NO_NEO4J

    try:
        if args.clear:
            # Scoped to the fixture's own sources. clear_graph() is never called
            # here: a judge's live-ingested data has to survive.
            removed_nodes = removed_edges = 0
            for source_doc in source_docs:
                res = await client.delete_source(source_doc)
                removed_nodes += res.get("deleted_nodes", 0)
                removed_edges += res.get("deleted_edges", 0)
                print(f"  cleared {source_doc!r}")
            print(f"\nRemoved {removed_nodes} node(s) and {removed_edges} edge(s).")
            print_footer(0, 0, 0, 0, embedded=args.embed)
            return EXIT_OK

        if args.reset:
            for source_doc in source_docs:
                await client.delete_source(source_doc)
                if args.verbose:
                    print(f"  reset: dropped prior copy of {source_doc!r}")

        total_nodes = total_edges = 0
        for item in plan:
            result = await client.write_graph(item["graph_data"], source_doc=item["source_doc"])
            total_nodes += result["nodes_written"]
            total_edges += result["edges_written"]
            print(
                f"  wrote {result['nodes_written']:>3} nodes / "
                f"{result['edges_written']:>3} edges  ←  {item['source_doc']}"
            )

        method = ""
        if args.embed:
            print("\n  embedding seeded entities…")
            method = await _embed_documents(client, source_docs, args.verbose)
            print(f"  embedding method: {method}")

        dup_groups = await _count_pending_duplicates(client, source_docs)
        print()
        print_footer(
            len(plan),
            total_nodes,
            total_edges,
            dup_groups,
            embedded=args.embed and method == "nosana",
        )
        return EXIT_OK
    finally:
        await client.close()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_graph",
        description="Seed the knowledge graph from a checked-in fixture (no API keys needed).",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="path to the fixture JSON (default: seed/graph.json)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete each fixture source_doc first, then seed (never clears the whole graph)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="delete the fixture's sources and exit without seeding",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the fixture and print the write plan; opens no Neo4j connection",
    )
    parser.add_argument(
        "--embed",
        action="store_true",
        help="also run the embedding pass (off by default so a keyless seed writes no "
             "hash pseudo-embeddings)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="per-node/edge detail")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.clear and args.reset:
        print("--clear and --reset are mutually exclusive.", file=sys.stderr)
        return EXIT_BAD_FIXTURE

    try:
        data = load_fixture(args.fixture)
    except FixtureError as exc:
        print(f"Fixture invalid — {exc}", file=sys.stderr)
        return EXIT_BAD_FIXTURE

    plan = build_plan(data)
    total_nodes = sum(len(p["graph_data"]["nodes"]) for p in plan)
    total_edges = sum(len(p["graph_data"]["edges"]) for p in plan)

    print(f"Fixture: {args.fixture} (version {data['version']})")
    print(f"Provenance: {data.get('generated_by', 'unstated')}")
    print(f"Documents: {len(plan)} · {total_nodes} nodes · {total_edges} edges")

    if args.dry_run:
        print_plan(data, plan, dry_run=True, verbose=args.verbose)
        print_footer(
            len(plan),
            total_nodes,
            total_edges,
            len(fixture_duplicate_groups(data)),
            embedded=args.embed,
        )
        print("(dry run — nothing was written)")
        return EXIT_OK

    if args.verbose:
        print_plan(data, plan, dry_run=False, verbose=True)
    else:
        print()

    return await run(args, data, plan)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
