"""Tests for GraphAgent.ingest_file — src.agent.core, offline.

Builds a real `GraphAgent` around an `AsyncMock` Neo4j client (mirrors how
`ingest_url`/`ingest_text` would be exercised), then neuters the two
collaborators `_post_ingest` would otherwise drive for real:

- `agent.nosana` / `agent.daytona` -> AsyncMocks. Without this, the verify
  stage's Daytona fallback spawns a REAL local subprocess (see
  `src/agent/daytona_exec.py`) — offline tests must never shell out.
- `agent._get_ws_manager` -> patched to return None, since the WebSocket
  manager lives in `src.main` and is resolved via a lazy import.

`extract_from_file` and `ingest_to_graph` are monkeypatched at their
`src.agent.core` call sites so this module tests `ingest_file`'s
orchestration contract (stages, source_doc fallback, extraction metadata,
error short-circuit) independent of the parallel `file_extractor`/
`graph_writer` work landing.

Importing `src.agent.core` currently fails (`ImportError: cannot import
name 'extract_from_file'`) because `src/ingestion/file_extractor.py` is
still a placeholder stub from a parallel work package — this whole module
will show as a collection error until that lands, which is the expected
"awaiting integration" shape, not a bug in these tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import src.agent.core as core
from src.agent.core import GraphAgent

EXPECTED_STAGES = ["parsing", "extracting", "embedding", "broadcasting", "verifying"]


def _extraction_envelope(**overrides) -> dict:
    envelope = {
        "url": None,
        "title": "doc-title",
        "domain": "doc-title",
        "content": "Ada Lovelace worked on the Analytical Engine.",
        "char_count": 46,
        "error": None,
        "filename": "notes.txt",
        "format": ".txt",
        "body_truncated": False,
        "pages": None,
    }
    envelope.update(overrides)
    return envelope


@pytest.fixture
def agent(monkeypatch):
    graph_agent = GraphAgent(AsyncMock())
    graph_agent.nosana = AsyncMock()
    graph_agent.daytona = AsyncMock()
    graph_agent.daytona.verify_graph.return_value = {"ok": True}
    # Empty node list short-circuits `_embed_source_nodes` before it needs to
    # iterate a mocked (non-iterable) Neo4j result.
    graph_agent.neo4j.get_nodes_by_source.return_value = []
    graph_agent.neo4j.get_graph_data_by_source.return_value = {"nodes": [], "edges": []}
    monkeypatch.setattr(graph_agent, "_get_ws_manager", lambda: None)
    return graph_agent


async def _collect_stages():
    stages: list[str] = []

    async def progress_cb(stage: str) -> None:
        stages.append(stage)

    return stages, progress_cb


# ------------------------------------------------------------------
# Happy path: stage order, source_doc fallback, extraction metadata
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_happy_path_stage_order(agent, monkeypatch) -> None:
    monkeypatch.setattr(
        core, "extract_from_file", AsyncMock(return_value=_extraction_envelope())
    )
    fake_ingest_to_graph = AsyncMock(
        return_value={"success": True, "nodes_created": 2, "edges_created": 1}
    )
    monkeypatch.setattr(core, "ingest_to_graph", fake_ingest_to_graph)

    stages, progress_cb = await _collect_stages()
    result = await agent.ingest_file(b"irrelevant bytes", "notes.txt", progress=progress_cb)

    assert stages == EXPECTED_STAGES
    assert result["success"] is True
    fake_ingest_to_graph.assert_awaited_once()


@pytest.mark.asyncio
async def test_ingest_file_source_doc_falls_back_to_filename(agent, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "extract_from_file",
        AsyncMock(return_value=_extraction_envelope(filename="notes.txt")),
    )
    fake_ingest_to_graph = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(core, "ingest_to_graph", fake_ingest_to_graph)

    await agent.ingest_file(b"irrelevant bytes", "notes.txt", source=None)

    fake_ingest_to_graph.assert_awaited_once()
    assert fake_ingest_to_graph.await_args.kwargs["source_doc"] == "notes.txt"


@pytest.mark.asyncio
async def test_ingest_file_source_doc_uses_explicit_source(agent, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "extract_from_file",
        AsyncMock(return_value=_extraction_envelope(filename="notes.txt")),
    )
    fake_ingest_to_graph = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(core, "ingest_to_graph", fake_ingest_to_graph)

    await agent.ingest_file(b"irrelevant bytes", "notes.txt", source="my-custom-doc")

    fake_ingest_to_graph.assert_awaited_once()
    assert fake_ingest_to_graph.await_args.kwargs["source_doc"] == "my-custom-doc"


@pytest.mark.asyncio
async def test_ingest_file_result_extraction_metadata(agent, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "extract_from_file",
        AsyncMock(
            return_value=_extraction_envelope(
                title="Ada Lovelace Notes",
                domain="Ada Lovelace Notes",
                char_count=123,
                pages=3,
                format=".pdf",
            )
        ),
    )
    fake_ingest_to_graph = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(core, "ingest_to_graph", fake_ingest_to_graph)

    result = await agent.ingest_file(b"irrelevant bytes", "doc.pdf")

    assert result["extraction"]["title"] == "Ada Lovelace Notes"
    assert result["extraction"]["domain"] == "Ada Lovelace Notes"
    assert result["extraction"]["char_count"] == 123
    assert result["extraction"]["pages"] == 3


# ------------------------------------------------------------------
# Envelope error -> early return, ingest_to_graph never awaited
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ingest_file_envelope_error_short_circuits(agent, monkeypatch) -> None:
    monkeypatch.setattr(
        core,
        "extract_from_file",
        AsyncMock(return_value=_extraction_envelope(error="Blocked: unsupported file type")),
    )
    fake_ingest_to_graph = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(core, "ingest_to_graph", fake_ingest_to_graph)

    stages, progress_cb = await _collect_stages()
    result = await agent.ingest_file(b"irrelevant bytes", "evil.docx", progress=progress_cb)

    assert result == {"success": False, "error": "Blocked: unsupported file type"}
    fake_ingest_to_graph.assert_not_awaited()
    # Only the "parsing" prologue ran before the short-circuit.
    assert stages == ["parsing"]
