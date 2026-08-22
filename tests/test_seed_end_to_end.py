"""End-to-end integration test for the seed fixture + seed script (C4 + C5).

Skips unless NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD are set — same pattern as
tests/test_integration_neo4j.py. Additionally requires seed/graph.json and
scripts/seed_graph.py to exist; both are part of the parallel work this
module gates, so a plain top-level import/open is used (not `importorskip`)
so a missing dependency shows as a collection/setup FAILURE, not a skip.

The CLI is invoked via `python -m scripts.seed_graph` as a real subprocess —
deliberately avoiding any assumption about the script's internal Python
interface (unlike tests/test_seed_script.py, which does need that and
documents the interface it assumes). Only the fixture's on-disk contract
(C5) and the documented CLI flags (C4) are assumed here.

ASSUMED FORMAT for seed/README.md (documented ambiguity — the file does not
exist yet at the time this test was written): two lines of the form

    Pre-merge pair: `<Label A>` -> `<Label B>`
    Post-merge pair: `<Label C>` -> `<Label D>`

naming two entity labels that already resolve a path via `find_path` before
any merge happens (general connectivity sanity-check), and two entity labels
that do NOT resolve a path until after the cross-document duplicate group is
merged (the dedup payoff: merging joins two previously-disconnected
sub-graphs through the new canonical node). If seed/README.md ships in a
different format, update PRE_MERGE_RE / POST_MERGE_RE below.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.graph.neo4j_client import Neo4jClient

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "seed" / "graph.json"
README_PATH = REPO_ROOT / "seed" / "README.md"

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USER")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")

PRE_MERGE_RE = re.compile(r"Pre-merge pair:\s*`([^`]+)`\s*(?:->|→|and)\s*`([^`]+)`", re.I)
POST_MERGE_RE = re.compile(r"Post-merge pair:\s*`([^`]+)`\s*(?:->|→|and)\s*`([^`]+)`", re.I)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (NEO4J_URI and NEO4J_USER and NEO4J_PASSWORD),
        reason="NEO4J_URI/NEO4J_USER/NEO4J_PASSWORD not set — no live Neo4j to test against",
    ),
]


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scripts.seed_graph", *args],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def fixture_data() -> dict:
    if not FIXTURE_PATH.exists():
        pytest.fail(f"{FIXTURE_PATH} does not exist — C5 has not landed yet")
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def readme_pairs() -> tuple[tuple[str, str], tuple[str, str]]:
    if not README_PATH.exists():
        pytest.fail(f"{README_PATH} does not exist — C4/C5 docs have not landed yet")
    text = README_PATH.read_text(encoding="utf-8")

    pre = PRE_MERGE_RE.search(text)
    post = POST_MERGE_RE.search(text)
    assert pre, (
        "seed/README.md has no 'Pre-merge pair: `A` -> `B`' line matching the "
        "format this test assumes (see module docstring) — update the fixture "
        "README or PRE_MERGE_RE"
    )
    assert post, (
        "seed/README.md has no 'Post-merge pair: `C` -> `D`' line matching the "
        "format this test assumes (see module docstring) — update the fixture "
        "README or POST_MERGE_RE"
    )
    return (pre.group(1), pre.group(2)), (post.group(1), post.group(2))


@pytest.fixture
async def neo4j():
    client = Neo4jClient(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)
    await client.connect()
    await client.init_schema()
    try:
        yield client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_seed_end_to_end_flow(neo4j, fixture_data, readme_pairs) -> None:
    source_docs = [doc["source_doc"] for doc in fixture_data["documents"]]
    expected_nodes = sum(len(doc.get("nodes", [])) for doc in fixture_data["documents"])
    (pre_a, pre_b), (post_c, post_d) = readme_pairs
    merge_result: dict | None = None

    try:
        # --- seed ---
        seeded = _run_cli("--fixture", str(FIXTURE_PATH))
        assert seeded.returncode == 0, seeded.stderr

        stats = await neo4j.get_stats()
        assert stats["nodes"] == expected_nodes

        # --- seed again: idempotent ---
        seeded_again = _run_cli("--fixture", str(FIXTURE_PATH))
        assert seeded_again.returncode == 0, seeded_again.stderr

        stats_after = await neo4j.get_stats()
        assert stats_after["nodes"] == stats["nodes"]
        assert stats_after["edges"] == stats["edges"]

        # --- duplicate groups: exactly 3, per the fixture's cross-doc collisions ---
        groups = await neo4j.find_duplicate_groups(limit=20)
        assert len(groups) == 3

        # --- pre-merge pair already resolves ---
        pre_path = await neo4j.find_path(pre_a, pre_b)
        assert pre_path, f"expected a path between {pre_a!r} and {pre_b!r} before any merge"

        # --- merge the Nosana-themed duplicate group ---
        nosana_groups = [g for g in groups if "nosana" in g["norm_label"]]
        assert nosana_groups, f"no duplicate group with norm_label containing 'nosana': {groups}"
        target_group = nosana_groups[0]
        node_ids = [n["id"] for n in target_group["nodes"]]
        assert len(node_ids) >= 2
        merge_result = await neo4j.merge_nodes(node_ids)
        assert merge_result["ok"] is True

        # --- post-merge pair now resolves ---
        post_path = await neo4j.find_path(post_c, post_d)
        assert post_path, (
            f"expected a path between {post_c!r} and {post_d!r} after merging "
            f"the Nosana duplicate group"
        )

        # --- clear: fixture nodes gone ---
        cleared = _run_cli("--fixture", str(FIXTURE_PATH), "--clear")
        assert cleared.returncode == 0, cleared.stderr

        for source_doc in source_docs:
            remaining = await neo4j.get_graph_data_by_source(source_doc)
            assert remaining["nodes"] == []
    finally:
        # Deleting every fixture source_doc's membership empties the merged
        # canonical node's source_docs too (it carries the union), so this
        # alone is enough to fully clean up post-merge state as well.
        for source_doc in source_docs:
            await neo4j.delete_source(source_doc)
