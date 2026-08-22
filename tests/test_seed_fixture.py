"""Offline structural tests for the seed/graph.json fixture (C5).

Shape: {version: 1, generated_by, documents: [{source_doc, nodes: [...],
edges: [...]}]}. Node/edge ids inside the fixture are UNPREFIXED — the md5
doc-hash prefix is applied at write time (by `prefix_and_validate`, shared
with the live ingest path), not baked into the fixture.

This file only reads the fixture — it does not touch Neo4j and does not
import the seed script.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from src.graph.normalize import normalize_label

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "seed" / "graph.json"


@pytest.fixture(scope="module")
def fixture() -> dict:
    if not FIXTURE_PATH.exists():
        pytest.fail(
            f"{FIXTURE_PATH} does not exist yet — seed/graph.json (C5) has not "
            "landed from the parallel work this test gates."
        )
    with FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_version_is_1(fixture: dict) -> None:
    assert fixture.get("version") == 1


def test_has_generated_by(fixture: dict) -> None:
    assert fixture.get("generated_by")


def test_exactly_three_documents(fixture: dict) -> None:
    documents = fixture.get("documents", [])
    assert len(documents) == 3


def test_ids_unique_within_each_document(fixture: dict) -> None:
    for doc in fixture["documents"]:
        ids = [n["id"] for n in doc.get("nodes", [])]
        assert len(ids) == len(set(ids)), (
            f"duplicate node id within document {doc.get('source_doc')!r}"
        )


def test_every_edge_endpoint_declared_in_same_document(fixture: dict) -> None:
    for doc in fixture["documents"]:
        node_ids = {n["id"] for n in doc.get("nodes", [])}
        for edge in doc.get("edges", []):
            assert edge["source"] in node_ids, (
                f"edge source {edge['source']!r} not declared as a node in "
                f"document {doc.get('source_doc')!r}"
            )
            assert edge["target"] in node_ids, (
                f"edge target {edge['target']!r} not declared as a node in "
                f"document {doc.get('source_doc')!r}"
            )


def test_total_node_rows_in_expected_range(fixture: dict) -> None:
    total = sum(len(doc.get("nodes", [])) for doc in fixture["documents"])
    assert 52 <= total <= 58, f"expected 52..58 total node rows, got {total}"


def test_flagship_demo_question_has_enough_sponsor_provide_summaries(fixture: dict) -> None:
    """Guards the keyless flagship demo question: 'Who are the sponsors and
    what do they provide?' needs enough grounded context to answer even when
    KIMI_API_KEY / Nosana aren't configured (keyword search only)."""
    hits = 0
    for doc in fixture["documents"]:
        for node in doc.get("nodes", []):
            summary = (node.get("properties", {}).get("summary") or "").lower()
            if "sponsor" in summary and "provide" in summary:
                hits += 1
    assert hits >= 4, f"expected >=4 sponsor+provide summaries, got {hits}"


def test_exactly_three_cross_document_normalize_label_collisions(fixture: dict) -> None:
    """A "collision" here is a norm_label shared by nodes in two different
    documents (same underlying entity, spelled slightly differently) — the
    exact-tier duplicate-detection candidates `find_duplicate_groups` returns."""
    docs_by_norm_label: dict[str, set[str]] = defaultdict(set)
    for doc in fixture["documents"]:
        source_doc = doc.get("source_doc")
        for node in doc.get("nodes", []):
            norm = normalize_label(node.get("label", ""))
            if norm:
                docs_by_norm_label[norm].add(source_doc)

    collisions = {
        norm: docs for norm, docs in docs_by_norm_label.items() if len(docs) >= 2
    }

    assert len(collisions) == 3, (
        f"expected exactly 3 cross-document normalize_label collisions, "
        f"got {len(collisions)}: {collisions}"
    )
    for norm, docs in collisions.items():
        assert len(docs) == 2, (
            f"collision {norm!r} spans {len(docs)} documents, expected exactly 2: {docs}"
        )
