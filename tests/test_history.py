"""Tests for src.agent.history — the in-memory query history store, offline.

Pure stdlib: the store has no collaborators, no I/O and no event loop, so
every test here is synchronous and builds its own `QueryHistory`.
"""
from __future__ import annotations

import json

from src.agent.history import UNKNOWN_STATUS, QueryHistory

#: The frozen public schema. Mirrors tests/test_jobs.py's PUBLIC_KEYS lock:
#: the frontend pins these names, so a change here is a breaking change.
PUBLIC_KEYS = {
    "id", "question", "answer", "status", "context_nodes",
    "source_docs", "saved", "created_at", "duration_ms",
}


def _response(**overrides) -> dict:
    payload = {
        "answer": "GraphAgent Forge builds knowledge graphs.",
        "status": "answered",
        "context_nodes": ["GraphAgent Forge", "Neo4j"],
        "sources": [{"id": "n1", "label": "GraphAgent Forge", "score": 0.91}],
        "source_docs": ["README"],
    }
    payload.update(overrides)
    return payload


def _record(store: QueryHistory, question: str = "q?", **overrides) -> dict:
    return store.record(question=question, response=_response(**overrides), duration_ms=12)


# ------------------------------------------------------------------
# Public schema
# ------------------------------------------------------------------
def test_public_schema_is_exactly_the_nine_keys() -> None:
    store = QueryHistory()
    entry = _record(store)
    assert set(entry) == PUBLIC_KEYS
    assert store.get(entry["id"]).keys() == PUBLIC_KEYS
    assert set(store.list()[0]) == PUBLIC_KEYS


def test_sources_is_never_stored() -> None:
    """`sources` is a heterogeneous list of full node dicts — excluded on
    purpose so a ring buffer never fills with megabytes the UI never shows."""
    store = QueryHistory()
    entry = _record(store)
    assert "sources" not in entry
    assert "score" not in json.dumps(entry)


def test_recorded_fields_round_trip() -> None:
    store = QueryHistory()
    entry = store.record(question="Who built it?", response=_response(), duration_ms=37)
    assert entry["question"] == "Who built it?"
    assert entry["answer"] == "GraphAgent Forge builds knowledge graphs."
    assert entry["status"] == "answered"
    assert entry["context_nodes"] == ["GraphAgent Forge", "Neo4j"]
    assert entry["source_docs"] == ["README"]
    assert entry["duration_ms"] == 37
    assert entry["saved"] is False
    assert entry["id"] and entry["created_at"]


# ------------------------------------------------------------------
# Defensive copying
# ------------------------------------------------------------------
def test_to_public_returns_copies_not_live_lists() -> None:
    store = QueryHistory()
    entry = _record(store)
    entry["context_nodes"].append("injected")
    entry["source_docs"].clear()
    fresh = store.get(entry["id"])
    assert fresh["context_nodes"] == ["GraphAgent Forge", "Neo4j"]
    assert fresh["source_docs"] == ["README"]


# ------------------------------------------------------------------
# Ordering + listing
# ------------------------------------------------------------------
def test_list_is_newest_first_and_limited() -> None:
    store = QueryHistory()
    ids = [_record(store, f"q{i}")["id"] for i in range(5)]
    listed = store.list(limit=3)
    assert [e["id"] for e in listed] == list(reversed(ids))[:3]


def test_list_handles_junk_and_non_positive_limits() -> None:
    store = QueryHistory()
    _record(store)
    assert store.list(limit=0) == []
    assert store.list(limit=-1) == []
    assert len(store.list(limit="not-a-number")) == 1  # falls back to the default


# ------------------------------------------------------------------
# Eviction
# ------------------------------------------------------------------
def test_eviction_drops_oldest_unsaved_first() -> None:
    store = QueryHistory(limit=3)
    ids = [_record(store, f"q{i}")["id"] for i in range(3)]
    extra = _record(store, "q3")["id"]

    assert store.get(ids[0]) is None  # oldest gone
    assert [e["id"] for e in store.list(limit=10)] == [extra, ids[2], ids[1]]
    assert store.counts() == {"total": 3, "saved_count": 0}


def test_saved_entries_survive_eviction() -> None:
    store = QueryHistory(limit=3)
    keep = _record(store, "keep-me")["id"]
    assert store.set_saved(keep, True)["saved"] is True
    for i in range(10):
        _record(store, f"filler{i}")

    assert store.get(keep) is not None
    assert store.get(keep)["saved"] is True
    # Still bounded: the pin holds a slot, unsaved entries roll through it.
    assert store.counts()["total"] == 3
    assert store.counts()["saved_count"] == 1


def test_all_saved_ring_stops_evicting_instead_of_looping() -> None:
    store = QueryHistory(limit=2, saved_limit=10)
    for i in range(4):
        entry = _record(store, f"q{i}")
        store.set_saved(entry["id"], True)
    assert store.counts() == {"total": 4, "saved_count": 4}


# ------------------------------------------------------------------
# Saved flag
# ------------------------------------------------------------------
def test_saved_limit_refusal_is_a_no_op_returning_the_entry() -> None:
    store = QueryHistory(limit=50, saved_limit=2)
    ids = [_record(store, f"q{i}")["id"] for i in range(3)]
    assert store.set_saved(ids[0], True)["saved"] is True
    assert store.set_saved(ids[1], True)["saved"] is True

    refused = store.set_saved(ids[2], True)
    assert refused is not None
    assert refused["saved"] is False  # the route turns this into a 409
    assert store.counts()["saved_count"] == 2

    # Re-saving an already-saved entry is allowed even at the cap.
    assert store.set_saved(ids[0], True)["saved"] is True

    # Freeing a slot lets the refused one through.
    store.set_saved(ids[0], False)
    assert store.set_saved(ids[2], True)["saved"] is True


def test_set_saved_and_delete_on_unknown_id() -> None:
    store = QueryHistory()
    assert store.set_saved("nope", True) is None
    assert store.set_saved("nope", False) is None
    assert store.delete("nope") is False
    assert store.get("nope") is None


def test_delete_removes_from_both_ring_and_index() -> None:
    store = QueryHistory()
    entry = _record(store)
    assert store.delete(entry["id"]) is True
    assert store.get(entry["id"]) is None
    assert store.list() == []
    assert store.counts() == {"total": 0, "saved_count": 0}
    assert store.delete(entry["id"]) is False


def test_deleting_a_saved_entry_frees_a_slot() -> None:
    store = QueryHistory(saved_limit=1)
    first = _record(store, "a")["id"]
    second = _record(store, "b")["id"]
    store.set_saved(first, True)
    assert store.set_saved(second, True)["saved"] is False
    store.delete(first)
    assert store.set_saved(second, True)["saved"] is True


# ------------------------------------------------------------------
# Bounding
# ------------------------------------------------------------------
def test_answer_and_question_are_truncated() -> None:
    store = QueryHistory(max_answer_chars=20)
    entry = store.record(
        question="q" * 500, response=_response(answer="a" * 100_000), duration_ms=1
    )
    assert entry["answer"] == "a" * 20
    assert entry["question"] == "q" * 20


def test_lists_are_truncated_in_length_and_per_item() -> None:
    store = QueryHistory(max_list_items=3, max_answer_chars=5)
    entry = store.record(
        question="q",
        response=_response(
            context_nodes=[f"node-{i}" for i in range(50)],
            source_docs=["d" * 900] * 40,
        ),
        duration_ms=1,
    )
    assert entry["context_nodes"] == ["node-", "node-", "node-"]
    assert entry["source_docs"] == ["ddddd"] * 3


def test_limits_are_clamped_to_at_least_one() -> None:
    store = QueryHistory(limit=0, saved_limit=-5, max_answer_chars=0, max_list_items=-1)
    entry = store.record(
        question="abc", response=_response(context_nodes=["x", "y"]), duration_ms=1
    )
    assert entry["question"] == "a"
    assert entry["context_nodes"] == ["x"]
    _record(store, "second")
    assert store.counts()["total"] == 1


# ------------------------------------------------------------------
# Malformed payloads
# ------------------------------------------------------------------
def test_record_tolerates_an_empty_or_partial_response() -> None:
    store = QueryHistory()
    entry = store.record(question="q", response={}, duration_ms=5)
    assert entry["answer"] == ""
    assert entry["status"] == UNKNOWN_STATUS
    assert entry["context_nodes"] == []
    assert entry["source_docs"] == []

    partial = store.record(question="q", response={"answer": "hi"}, duration_ms=5)
    assert partial["answer"] == "hi"
    assert partial["status"] == UNKNOWN_STATUS


def test_record_tolerates_a_non_dict_response_and_bad_duration() -> None:
    store = QueryHistory()
    for junk in (None, "not a dict", 42, ["a"]):
        entry = store.record(question="q", response=junk, duration_ms=None)
        assert set(entry) == PUBLIC_KEYS
        assert entry["answer"] == ""
        assert entry["duration_ms"] == 0
    assert store.record(question="q", response={}, duration_ms=-9)["duration_ms"] == 0


def test_junk_values_never_leak_raw_exception_text_into_json() -> None:
    """A payload carrying live objects must serialize cleanly and reveal
    nothing about them — repr()s leak bolt URIs, credentials and stack detail.
    """
    secret = "bolt://neo4j:hunter2@10.0.0.5:7687"
    store = QueryHistory()
    entry = store.record(
        question=RuntimeError(f"boom {secret}"),
        response={
            "answer": RuntimeError(f"ServiceUnavailable {secret}"),
            "status": ValueError(f"status blew up {secret}"),
            "context_nodes": [RuntimeError(secret), "ok-label", object()],
            "source_docs": RuntimeError(secret),
            "sources": [RuntimeError(secret)],
        },
        duration_ms=3,
    )
    blob = json.dumps(entry)  # must not raise — every value is JSON-native
    assert secret not in blob
    assert "boom" not in blob
    assert "RuntimeError" not in blob
    assert "ServiceUnavailable" not in blob
    assert entry["question"] == ""
    assert entry["answer"] == ""
    assert entry["status"] == UNKNOWN_STATUS
    assert entry["context_nodes"] == ["ok-label"]
    assert entry["source_docs"] == []


def test_counts_reflect_records_saves_and_deletes() -> None:
    store = QueryHistory()
    assert store.counts() == {"total": 0, "saved_count": 0}
    ids = [_record(store, f"q{i}")["id"] for i in range(3)]
    store.set_saved(ids[0], True)
    assert store.counts() == {"total": 3, "saved_count": 1}
    store.delete(ids[0])
    assert store.counts() == {"total": 2, "saved_count": 0}
