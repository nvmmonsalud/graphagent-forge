"""Durable query history: disk reopen, bounded payloads and shared pin limits."""

from __future__ import annotations

import asyncio

import pytest

from src.agent.durable_history import SQLiteQueryHistory
from src.agent.history import QueryHistory
from src.agent.state import StateDatabase

PUBLIC_KEYS = {
    "id",
    "question",
    "answer",
    "status",
    "context_nodes",
    "source_docs",
    "saved",
    "created_at",
    "duration_ms",
}


async def make_store(tmp_path, **kwargs):
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    history = SQLiteQueryHistory(db, **kwargs)
    await history.initialize()
    return history


async def record(history, question="question", **kwargs):
    return await history.record(
        question=question,
        response={
            "answer": "answer",
            "status": "answered",
            "context_nodes": ["node"],
            "source_docs": ["source"],
            **kwargs,
        },
        duration_ms=12,
    )


async def test_saved_history_and_exact_schema_survive_real_disk_reopen(tmp_path):
    history = await make_store(tmp_path)
    entry = await record(history)
    assert set(entry) == PUBLIC_KEYS
    assert (await history.set_saved(entry["id"], True))["saved"]
    reopened = await make_store(tmp_path)
    assert await reopened.counts() == {"total": 1, "saved_count": 1}
    result = await reopened.get(entry["id"])
    assert result == {**entry, "saved": True}
    result["context_nodes"].append("mutated")
    assert (await reopened.get(entry["id"]))["context_nodes"] == ["node"]
    assert await reopened.list() == [{**entry, "saved": True}]


async def test_eviction_matches_memory_history_with_pinned_overflow(tmp_path):
    history = await make_store(tmp_path, limit=2, saved_limit=3)
    memory = QueryHistory(limit=2, saved_limit=3)
    for number in range(6):
        disk = await record(history, str(number))
        ram = memory.record(question=str(number), response={"answer": "answer"}, duration_ms=1)
        if number < 3:
            await history.set_saved(disk["id"], True)
            memory.set_saved(ram["id"], True)
        assert [row["question"] for row in await history.list(99)] == [
            row["question"] for row in memory.list(99)
        ]
        assert await history.counts() == memory.counts()
    assert await history.counts() == {"total": 4, "saved_count": 3}
    # Unpinning follows existing semantics: eviction occurs on next record.
    oldest = (await history.list(99))[-1]
    await history.set_saved(oldest["id"], False)
    assert await history.counts() == {"total": 4, "saved_count": 2}
    await record(history, "new")
    assert await history.get(oldest["id"]) is None
    assert await history.counts() == {"total": 3, "saved_count": 2}


async def test_two_managers_enforce_pin_cap_transactionally(tmp_path):
    left = await make_store(tmp_path, saved_limit=1)
    right = await make_store(tmp_path, saved_limit=1)
    entries = [await record(left, str(number)) for number in range(8)]
    results = await asyncio.gather(
        *(
            store.set_saved(entry["id"], True)
            for store, entry in zip([left, right] * 4, entries, strict=True)
        )
    )
    assert sum(result["saved"] for result in results) == 1
    assert await right.counts() == {"total": 8, "saved_count": 1}


async def test_payload_bounds_and_sensitive_unknown_fields_never_persist(tmp_path):
    history = await make_store(tmp_path, max_answer_chars=8, max_list_items=2)

    class Secret:
        def __str__(self):
            raise AssertionError("must not stringify untrusted values")

    result = await history.record(
        question="question" * 5,
        response={
            "answer": "answer" * 20,
            "status": "answered",
            "context_nodes": [Secret(), "node one extra", "node two extra", "node3"],
            "source_docs": ["source one", "source two", "source3"],
            "private": "never-persist-raw-secret",
            "citations": [{"text": "raw chunk"}],
        },
        duration_ms=-1,
    )
    assert result["question"] == "question"
    assert result["answer"] == "answeran"
    assert result["context_nodes"] == ["node one", "node two"]
    assert result["source_docs"] == ["source o", "source t"]
    assert result["duration_ms"] == 0
    assert "never-persist-raw-secret" not in str(
        await history.db.run(lambda c: tuple(c.execute("SELECT * FROM query_history").fetchone()))
    )
    junk = await history.record(question=Secret(), response=None, duration_ms=Secret())
    assert junk["question"] == junk["answer"] == ""
    assert junk["status"] == "unknown"
    assert junk["duration_ms"] == 0


async def test_list_bounds_unknown_ids_delete_and_reopen(tmp_path):
    history = await make_store(tmp_path)
    first = await record(history, "first")
    second = await record(history, "second")
    assert [row["id"] for row in await history.list(1)] == [second["id"]]
    assert await history.list(0) == await history.list(-1) == []
    assert len(await history.list(None)) == 2
    assert await history.get("missing") is None
    assert await history.set_saved("missing", True) is None
    assert not await history.delete("missing")
    assert await history.delete(first["id"])
    reopened = await make_store(tmp_path)
    assert await reopened.get(first["id"]) is None
    assert await reopened.counts() == {"total": 1, "saved_count": 0}


async def test_incompatible_history_schema_is_startup_failure(tmp_path):
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    await db.run(lambda c: c.execute("CREATE TABLE query_history (id TEXT)").rowcount, write=True)
    with pytest.raises(ValueError, match="schema"):
        await SQLiteQueryHistory(db).initialize()
