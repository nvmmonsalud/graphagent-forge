"""Offline regression checks for bounded source excerpts and atomic extraction."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.ingestion.chunks import MAX_CHUNKS, chunk_id, plan_chunks
from src.ingestion.extractor import extract_from_text
from src.ingestion.graph_writer import ingest_to_graph
from src.ingestion.sources import build_source


def test_offsets_unicode_determinism_and_bounds():
    text = "🦊é" * 100000
    chunks = plan_chunks(text, "src", "v")
    assert len(chunks) == MAX_CHUNKS
    for chunk in chunks:
        assert chunk["text"] == text[chunk["start"]:chunk["end"]]
        assert len(chunk["text"]) == chunk["end"] - chunk["start"]
        assert chunk["id"] == chunk_id("src", "v", chunk["ordinal"], chunk["text"])
    assert chunks[-1]["end"] == 91600
    assert chunks == plan_chunks(text, "src", "v")
    assert chunk_id("src", "v", 0, "a") != chunk_id("src", "v", 1, "a")


def test_explicit_text_cap_has_no_synthetic_text():
    old = extract_from_text("x" * 210000)
    new = extract_from_text("x" * 210000, max_chars=200000)
    assert old["content"].endswith("[...truncated...]")
    assert new["content"] == "x" * 200000
    assert new["input_truncated"] is True


@pytest.mark.asyncio
async def test_chunks_publish_empty_graph_and_preserve_late_fact(monkeypatch):
    text = "a" * 60000 + "late evidence"
    source = build_source("text", "book", "book", text)
    db = AsyncMock()
    db.get_source.return_value = None
    db.publish_source_graph.return_value = {"nodes_written": 0, "edges_written": 0}
    extract = AsyncMock(return_value={"nodes": [], "edges": []})
    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities", extract)
    result = await ingest_to_graph(db, text, source=source)
    assert result["success"]
    graph = db.publish_source_graph.call_args.args[0]
    assert any("late evidence" in chunk["text"] for chunk in graph["chunks"])
    assert result["coverage"]["processed_chars"] == len(text)
    assert "late evidence" not in str(result)


@pytest.mark.asyncio
async def test_failed_chunk_never_publishes(monkeypatch):
    source = build_source("text", "book", "book", "a" * 18000)
    db = AsyncMock()
    db.get_source.return_value = {"version": "old"}
    extract = AsyncMock(side_effect=[{"nodes": [], "edges": []}, RuntimeError("secret"),
                                    {"nodes": [], "edges": []}])
    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities", extract)
    result = await ingest_to_graph(db, "a" * 18000, source=source)
    assert not result["success"]
    assert result["coverage"]["failed_chunks"] == 1
    assert "secret" not in str(result)
    db.publish_source_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_namespacing_and_two_call_concurrency(monkeypatch):
    text = "x" * 200000
    source = build_source("text", "large", "large", text)
    db = AsyncMock()
    db.get_source.return_value = None
    db.publish_source_graph.return_value = {"nodes_written": 12, "edges_written": 12}
    active = peak = calls = 0

    async def extract(content, model=None):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1
        return {"nodes": [{"id": "n", "label": "N", "evidence_ids": ["forged"]}],
                "edges": [{"source": "n", "target": "n"}]}

    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities", extract)
    result = await ingest_to_graph(db, text, source=source)
    graph = db.publish_source_graph.call_args.args[0]
    assert calls == 12 and peak == 2
    assert len({n["id"] for n in graph["nodes"]}) == 12
    assert all(n["evidence_ids"] == [c["id"]]
               for n, c in zip(graph["nodes"], graph["chunks"], strict=True))
    assert result["coverage"]["processed_chars"] == 91600
    assert result["coverage"]["skipped_chars"] == 108400
    assert result["coverage"]["partial"]


@pytest.mark.asyncio
async def test_unchanged_source_skips_provider(monkeypatch):
    source = build_source("text", "name", "title", "abc")
    db = AsyncMock()
    db.get_source.return_value = {**source, "coverage": {"partial": False}}
    extract = AsyncMock()
    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities", extract)
    result = await ingest_to_graph(db, "abc", source=source)
    assert result["unchanged"]
    extract.assert_not_awaited()
    db.publish_source_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_caps_keep_endpoint_closure(monkeypatch):
    text = "x" * 17000
    source = build_source("text", "large", "large", text)
    db = AsyncMock()
    db.get_source.return_value = None
    db.publish_source_graph.return_value = {"nodes_written": 500, "edges_written": 500}

    async def extract(content, model=None):
        return {"nodes": [{"id": str(i), "label": "N"} for i in range(300)],
                "edges": [{"source": str(i), "target": str(i)} for i in range(300)]}

    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities", extract)
    result = await ingest_to_graph(db, text, source=source)
    graph = db.publish_source_graph.call_args.args[0]
    ids = {n["id"] for n in graph["nodes"]}
    assert len(ids) == 500
    assert all(e["source"] in ids and e["target"] in ids for e in graph["edges"])
    assert result["coverage"]["graph_capped"] is True
    assert result["coverage"]["partial"] is False


@pytest.mark.asyncio
async def test_malformed_chunk_aborts(monkeypatch):
    source = build_source("text", "book", "book", "abc")
    db = AsyncMock()
    db.get_source.return_value = None
    monkeypatch.setattr("src.ingestion.graph_writer.extract_entities",
                        AsyncMock(return_value={"nodes": "bad", "edges": []}))
    result = await ingest_to_graph(db, "abc", source=source)
    assert not result["success"]
    db.publish_source_graph.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_file_limit_has_real_offsets():
    from src.ingestion.file_extractor import extract_from_file

    result = await extract_from_file(b"x" * 210000, "notes.txt", max_chars=200000)
    assert result["content"] == "x" * 200000
    assert result["input_truncated"] is True
    assert result["char_count"] == 200000
