"""Durable commands are bounded inert data, with explicit method dispatch."""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.commands import command_handler, validate_command
from src.ingestion.file_extractor import MAX_UPLOAD_BYTES


@pytest.mark.parametrize("command", [
    None, [], {}, {"version": True, "kind": "url", "url": "https://example.org"},
    {"version": 2, "kind": "url", "url": "https://example.org"},
    {"version": 1, "kind": [], "url": "https://example.org"},
    {"version": 1, "kind": "delete_all"},
    {"version": 1, "kind": "url", "url": "file:///secret"},
    {"version": 1, "kind": "url", "url": "https://user:secret@example.org"},
    {"version": 1, "kind": "url", "url": "https://example.org", "function": "exec"},
    {"version": 1, "kind": "text", "text": "", "source": "manual"},
    {"version": 1, "kind": "text", "text": "x" * 200001, "source": "manual"},
    {"version": 1, "kind": "text", "text": "x", "source": ["manual"]},
])
def test_invalid_commands(command):
    with pytest.raises(ValueError):
        validate_command(command)


def file_command(**overrides):
    return {"version": 1, "kind": "file", "data_b64": "aGk=", "filename": "notes.txt",
            "content_type": "text/plain", "source": None, **overrides}


@pytest.mark.parametrize("overrides", [
    {"data_b64": "not base64"}, {"data_b64": ""}, {"data_b64": "aGl="},
    {"data_b64": base64.b64encode(b"x" * (MAX_UPLOAD_BYTES + 1)).decode()},
    {"filename": "../secret.txt"}, {"content_type": "application/pdf"},
    {"source": "x" * 301}, {"filename": False},
])
def test_invalid_file_commands(overrides):
    with pytest.raises(ValueError):
        validate_command(file_command(**overrides))


@pytest.mark.asyncio
@pytest.mark.parametrize("command,method,expected", [
    ({"version": 1, "kind": "url", "url": "https://example.org"}, "ingest_url",
     ("https://example.org",)),
    ({"version": 1, "kind": "text", "text": "hello", "source": "manual"},
     "ingest_text", ("hello",)),
    (file_command(), "ingest_file", (b"hi", "notes.txt")),
])
async def test_dispatch(command, method, expected):
    graph = SimpleNamespace(get_ingest_operation=AsyncMock(return_value=None))
    agent = SimpleNamespace(neo4j=graph, ingest_url=AsyncMock(), ingest_text=AsyncMock(),
                            ingest_file=AsyncMock())
    progress = AsyncMock()
    await command_handler(agent)(command, progress, "job-1")
    called = getattr(agent, method)
    assert called.await_args.args == expected
    assert called.await_args.kwargs["operation_id"] == "job-1"
    assert called.await_args.kwargs["progress"] is progress
    assert sum(getattr(agent, name).await_count for name in
               ("ingest_url", "ingest_text", "ingest_file")) == 1


@pytest.mark.asyncio
async def test_receipt_prevents_dispatch_and_malformed_command_never_looks_up():
    receipt = {"success": True, "unchanged": True}
    graph = SimpleNamespace(get_ingest_operation=AsyncMock(return_value=receipt))
    agent = SimpleNamespace(neo4j=graph, ingest_url=AsyncMock())
    handle = command_handler(agent)
    assert await handle({"version": 1, "kind": "url", "url": "https://example.org"},
                        None, "job-1") == receipt
    agent.ingest_url.assert_not_awaited()
    graph.get_ingest_operation.reset_mock()
    with pytest.raises(ValueError):
        await handle({"version": 1, "kind": "exec"}, None, "job-1")
    graph.get_ingest_operation.assert_not_awaited()
