"""Offline, mocked-Neo4j tests for scripts/seed_graph.py (C4).

ASSUMED ENTRYPOINT (documented ambiguity — scripts/seed_graph.py does not
exist yet at the time this test was written, per the WP6 brief): an async
``main(argv: list[str] | None = None) -> int`` function, with the module
importing ``Neo4jClient`` at module scope as ``scripts.seed_graph.Neo4jClient``
so it can be monkeypatched here. If the landed script uses a different
entrypoint name/shape, these tests will fail with an AttributeError/TypeError
pointing at the mismatch rather than a silent false pass — update the
`_run` helper below to match once the real interface is known.

CLI surface under test: `python -m scripts.seed_graph [--fixture PATH]
[--reset] [--clear] [--dry-run] [--embed]`.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# Deliberately a plain top-level import, not `pytest.importorskip`: until
# scripts/seed_graph.py (C4) lands from the parallel work, this whole module
# should fail to collect (a visible red signal) rather than skip quietly.
import scripts.seed_graph as seed_graph


def _prefix(source_doc: str, raw_id: str) -> str:
    """Independent re-implementation of the md5 doc-prefix — NOT imported
    from `src.ingestion.graph_writer.prefix_and_validate`, so this test does
    not simply assert the implementation agrees with itself."""
    doc_hash = hashlib.md5(source_doc.encode()).hexdigest()[:8]
    return f"{doc_hash}_{raw_id}"


def _fixture_dict() -> dict:
    return {
        "version": 1,
        "generated_by": "test_seed_script",
        "documents": [
            {
                "source_doc": "doc-one",
                "nodes": [
                    {"id": "a", "label": "Alpha", "type": "Concept",
                     "properties": {"summary": "first"}},
                    {"id": "b", "label": "Beta", "type": "Concept",
                     "properties": {"summary": "second"}},
                ],
                "edges": [
                    {"source": "a", "target": "b", "relationship": "RELATES_TO",
                     "properties": {"context": "a knows b"}},
                ],
            },
            {
                "source_doc": "doc-two",
                "nodes": [
                    {"id": "c", "label": "Gamma", "type": "Concept",
                     "properties": {"summary": "third"}},
                ],
                "edges": [],
            },
        ],
    }


@pytest.fixture
def fixture_path(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(_fixture_dict()), encoding="utf-8")
    return path


@pytest.fixture
def mock_neo4j_instance(monkeypatch):
    """Patch `scripts.seed_graph.Neo4jClient` so every construction returns
    the same AsyncMock instance, regardless of whether the script constructs
    zero, one, or several clients."""
    instance = AsyncMock()
    instance.write_graph.return_value = {"nodes_written": 1, "edges_written": 1}
    instance.delete_source.return_value = {
        "deleted_nodes": 0, "removed_memberships": 0, "deleted_edges": 0,
    }
    instance.clear_graph.return_value = {"deleted_nodes": 0}
    instance.get_stats.return_value = {"nodes": 0, "edges": 0, "entity_types": []}

    client_cls = MagicMock(return_value=instance)
    monkeypatch.setattr(seed_graph, "Neo4jClient", client_cls, raising=False)
    monkeypatch.setenv("NEO4J_URI", "bolt://mock:7687")
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "mock-password")
    return instance


async def _run(argv: list[str]) -> int:
    return await seed_graph.main(argv)


@pytest.mark.asyncio
async def test_dry_run_opens_no_connection_and_writes_nothing(
    mock_neo4j_instance, fixture_path
) -> None:
    exit_code = await _run(["--fixture", str(fixture_path), "--dry-run"])

    assert exit_code == 0
    assert mock_neo4j_instance.connect.await_count == 0
    assert mock_neo4j_instance.write_graph.await_count == 0


@pytest.mark.asyncio
async def test_one_write_graph_call_per_document(mock_neo4j_instance, fixture_path) -> None:
    exit_code = await _run(["--fixture", str(fixture_path)])

    assert exit_code == 0
    assert mock_neo4j_instance.write_graph.await_count == 2


@pytest.mark.asyncio
async def test_written_ids_carry_the_md5_doc_prefix(mock_neo4j_instance, fixture_path) -> None:
    await _run(["--fixture", str(fixture_path)])

    written_docs = [
        call.args[0] if call.args else call.kwargs["graph_data"]
        for call in mock_neo4j_instance.write_graph.await_args_list
    ]
    all_node_ids = {node["id"] for doc in written_docs for node in doc.get("nodes", [])}

    assert _prefix("doc-one", "a") in all_node_ids
    assert _prefix("doc-one", "b") in all_node_ids
    assert _prefix("doc-two", "c") in all_node_ids
    # Raw (unprefixed) ids must not survive to the write.
    assert "a" not in all_node_ids
    assert "b" not in all_node_ids
    assert "c" not in all_node_ids


@pytest.mark.asyncio
async def test_reset_calls_delete_source_once_per_doc_before_any_write(
    mock_neo4j_instance, fixture_path
) -> None:
    exit_code = await _run(["--fixture", str(fixture_path), "--reset"])

    assert exit_code == 0
    assert mock_neo4j_instance.delete_source.await_count == 2
    deleted_docs = {
        call.args[0] if call.args else call.kwargs.get("source_doc")
        for call in mock_neo4j_instance.delete_source.await_args_list
    }
    assert deleted_docs == {"doc-one", "doc-two"}

    # Every delete_source call happened before every write_graph call.
    method_order = [c[0] for c in mock_neo4j_instance.mock_calls if c[0] in
                     ("delete_source", "write_graph")]
    last_delete = max(i for i, m in enumerate(method_order) if m == "delete_source")
    first_write = min(i for i, m in enumerate(method_order) if m == "write_graph")
    assert last_delete < first_write

    assert mock_neo4j_instance.clear_graph.await_count == 0


@pytest.mark.asyncio
async def test_clear_writes_nothing_and_never_calls_clear_graph(
    mock_neo4j_instance, fixture_path
) -> None:
    exit_code = await _run(["--fixture", str(fixture_path), "--clear"])

    assert exit_code == 0
    assert mock_neo4j_instance.write_graph.await_count == 0
    assert mock_neo4j_instance.clear_graph.await_count == 0


@pytest.mark.asyncio
async def test_invalid_fixture_json_exits_1(mock_neo4j_instance, tmp_path) -> None:
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")

    exit_code = await _run(["--fixture", str(bad_path)])

    assert exit_code == 1
    assert mock_neo4j_instance.write_graph.await_count == 0
