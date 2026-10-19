"""Publication is authoritative even when the durable job loses its result."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.agent.core as core
import src.ingestion.graph_writer as writer
from src.agent.commands import command_handler
from src.agent.core import GraphAgent
from src.ingestion.sources import build_source


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["url", "text", "file"])
async def test_core_receipt_preflight_prevents_fetch_parse_and_paid_extraction(monkeypatch, kind):
    receipt = {"success": True, "source": "source", "version": "old", "nodes": 1,
               "edges": 0, "unchanged": True}
    graph = SimpleNamespace(get_ingest_operation=AsyncMock(return_value=receipt))
    agent = GraphAgent(graph)
    fetch = AsyncMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(core, "extract_from_url", fetch)
    monkeypatch.setattr(core, "extract_from_file", fetch)
    monkeypatch.setattr(core, "extract_from_text", lambda *_a, **_kw: pytest.fail("must not parse"))
    kwargs = {"operation_id": "old-job"}
    if kind == "url":
        result = await agent.ingest_url("https://example.org", **kwargs)
    elif kind == "file":
        result = await agent.ingest_file(b"data", "note.txt", **kwargs)
    else:
        result = await agent.ingest_text("data", **kwargs)
    assert result == receipt
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unchanged_source_still_records_atomic_receipt(monkeypatch):
    source = build_source("text", "manual", "manual", "original")
    receipt = {"success": True, "source": source["id"], "version": source["version"],
               "nodes": 0, "edges": 0, "unchanged": True}
    graph = SimpleNamespace(get_ingest_operation=AsyncMock(return_value=None),
                            get_source=AsyncMock(return_value=source),
                            publish_source_graph=AsyncMock(return_value=receipt))
    extract = AsyncMock()
    monkeypatch.setattr(writer, "extract_entities", extract)
    assert await writer.ingest_to_graph(graph, "original", source=source,
                                        operation_id="job-1") == receipt
    assert graph.publish_source_graph.await_args.kwargs["operation_id"] == "job-1"
    extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_crash_after_publication_refresh_then_replay_skips_paid_work(monkeypatch):
    """No SQLite final result survives, but the original publication receipt does."""
    receipts = {}
    source = build_source("text", "manual", "manual", "original")
    current = None

    async def get_receipt(operation):
        return receipts.get(operation)

    async def publish(data, *, source, expected_version, operation_id):
        nonlocal current
        if operation_id in receipts:
            return receipts[operation_id]
        current = dict(source)
        receipts[operation_id] = {"success": True, "source": source["id"],
                                  "version": source["version"], "nodes": len(data["nodes"]),
                                  "edges": 0, "unchanged": True}
        return {"nodes_written": len(data["nodes"]), "edges_written": 0}

    graph = SimpleNamespace(get_ingest_operation=AsyncMock(side_effect=get_receipt),
                            get_source=AsyncMock(return_value=None),
                            publish_source_graph=AsyncMock(side_effect=publish))
    extract = AsyncMock(return_value={"nodes": [{"id": "n", "label": "Original"}], "edges": []})
    monkeypatch.setattr(writer, "extract_entities", extract)
    agent = GraphAgent(graph)

    async def crash(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent, "_post_ingest", crash)
    handle = command_handler(agent)
    command = {"version": 1, "kind": "text", "text": "original", "source": "manual"}
    with pytest.raises(asyncio.CancelledError):
        await handle(command, None, "old-job")
    assert receipts["old-job"]["version"] == source["version"]
    # Another completed operation replaces the source before the old job recovers.
    current = build_source("text", "manual", "manual", "newer source")
    refreshed_version = current["version"]
    result = await handle(command, None, "old-job")
    assert result == receipts["old-job"]
    assert current["version"] == refreshed_version
    assert extract.await_count == 1
    assert graph.publish_source_graph.await_count == 1


@pytest.mark.asyncio
async def test_progress_cancellation_propagates():
    agent = GraphAgent(SimpleNamespace())

    async def cancelled(_stage):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await agent._report(cancelled, "extracting")


@pytest.mark.asyncio
@pytest.mark.parametrize("unchanged_before_extraction", [True, False])
async def test_receipt_discovered_in_publish_keeps_original_identity(
    monkeypatch, unchanged_before_extraction,
):
    source = build_source("text", "manual", "manual", "new content")
    graph = SimpleNamespace(
        get_ingest_operation=AsyncMock(return_value=None),
        get_source=AsyncMock(return_value=source if unchanged_before_extraction else None),
        publish_source_graph=AsyncMock(return_value={
            "nodes_written": 7, "edges_written": 2, "unchanged": True,
            "source": "original-source", "version": "original-version", "conflict": False,
        }),
    )
    monkeypatch.setattr(writer, "extract_entities", AsyncMock(return_value={
        "nodes": [], "edges": [],
    }))
    result = await writer.ingest_to_graph(graph, "new content", source=source, operation_id="job")
    assert result == {"success": True, "source": "original-source", "version": "original-version",
                      "nodes": 7, "edges": 2, "unchanged": True}
