"""Contract tests for DaytonaExecutor.verify_graph_fanout.

The fan-out audits one source per sandbox, all launched together.

Ran:
    {"ok": True, "sources": [...], "source_count", "sandbox_count", "wall_ms",
     "serial_ms", "speedup", "concurrency", "boot_ms", "method"}

Failed:
    {"ok": False, "error": FANOUT_ERROR_UNAVAILABLE, "method", "wall_ms"}
    with NO "sources" key — its absence is the machine-readable "nothing ran"
    signal, distinct from an audit that ran and found problems.

Each `sources` item carries its own verdict, so one source failing never sinks
the batch. `wall_ms`/`serial_ms`/`speedup` are measured at run time — these
tests assert the RELATIONSHIP between them, never a fixed number.
"""
from __future__ import annotations

import asyncio

import pytest

from src.agent.daytona_exec import (
    _MAX_FANOUT_CONCURRENCY,
    DEFAULT_FANOUT_CONCURRENCY,
    FANOUT_ERROR_UNAVAILABLE,
    DaytonaExecutor,
)


def _ok_item(source: str, *, duration_ms: int = 5, sandbox: bool = True) -> dict:
    item = {
        "source": source,
        "ok": True,
        "valid": True,
        "node_count": 2,
        "edge_count": 1,
        "orphan_count": 0,
        "duration_ms": duration_ms,
        "method": "daytona" if sandbox else "local",
    }
    if sandbox:
        item["sandbox_id"] = f"sbx-{source}"
        item["boot_ms"] = 7
    return item


@pytest.mark.asyncio
async def test_empty_graphs_is_a_valid_zero_source_result() -> None:
    """No sources is a well-defined answer, not a failure."""
    result = await DaytonaExecutor().verify_graph_fanout({})

    assert result["ok"] is True
    assert result["sources"] == []
    assert result["source_count"] == 0
    assert result["sandbox_count"] == 0
    assert result["speedup"] == 0.0


@pytest.mark.asyncio
async def test_one_verification_per_source_with_item_verdicts(monkeypatch) -> None:
    executor = DaytonaExecutor()
    seen: list[dict] = []

    async def _fake(graph_data):
        seen.append(graph_data)
        return _ok_item(graph_data["source_doc"])

    monkeypatch.setattr(executor, "verify_graph", _fake)

    graphs = {"doc-a": {"source_doc": "doc-a"}, "doc-b": {"source_doc": "doc-b"}}
    result = await executor.verify_graph_fanout(graphs)

    assert result["ok"] is True
    assert result["source_count"] == 2
    # One sandbox per source: the whole point of the feature.
    assert result["sandbox_count"] == 2
    assert [item["source"] for item in result["sources"]] == ["doc-a", "doc-b"]
    assert len(seen) == 2
    assert all(item["valid"] is True for item in result["sources"])


@pytest.mark.asyncio
async def test_parallel_wall_clock_beats_serial_sum(monkeypatch) -> None:
    """The measured wall clock must be well under the sum of the parts.

    Each fake verification sleeps; run concurrently the batch costs roughly one
    sleep, so `wall_ms` is a fraction of `serial_ms` and `speedup` says so.
    """
    executor = DaytonaExecutor()
    per_item_s = 0.08

    async def _slow(graph_data):
        await asyncio.sleep(per_item_s)
        return _ok_item(graph_data["source_doc"], duration_ms=int(per_item_s * 1000))

    monkeypatch.setattr(executor, "verify_graph", _slow)

    graphs = {f"doc-{i}": {"source_doc": f"doc-{i}"} for i in range(4)}
    result = await executor.verify_graph_fanout(graphs)

    assert result["ok"] is True
    assert result["serial_ms"] >= per_item_s * 1000 * 4  # 4 items, ~320ms of work
    assert result["wall_ms"] < result["serial_ms"]
    assert result["speedup"] > 1.5


@pytest.mark.asyncio
async def test_concurrency_cap_is_respected(monkeypatch) -> None:
    executor = DaytonaExecutor()
    cap = 2
    in_flight = 0
    peak = 0

    async def _slow(graph_data):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return _ok_item(graph_data["source_doc"])

    monkeypatch.setattr(executor, "verify_graph", _slow)

    graphs = {f"doc-{i}": {"source_doc": f"doc-{i}"} for i in range(6)}
    result = await executor.verify_graph_fanout(graphs, max_concurrency=cap)

    assert result["concurrency"] == cap
    assert peak <= cap, f"peak in-flight {peak} exceeded the cap {cap}"
    assert len(result["sources"]) == 6


@pytest.mark.asyncio
async def test_boot_ms_stats_are_aggregated_over_sandbox_runs(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _fake(graph_data):
        return _ok_item(graph_data["source_doc"])

    monkeypatch.setattr(executor, "verify_graph", _fake)

    result = await executor.verify_graph_fanout({"d": {"source_doc": "d"}})

    assert result["boot_ms"] == {"count": 1, "min": 7, "max": 7, "avg": 7}


@pytest.mark.asyncio
async def test_boot_ms_is_null_when_no_sandbox_ran(monkeypatch) -> None:
    """Local fallback runs must not invent a boot time."""
    executor = DaytonaExecutor()

    async def _local(graph_data):
        return _ok_item(graph_data["source_doc"], sandbox=False)

    monkeypatch.setattr(executor, "verify_graph", _local)

    result = await executor.verify_graph_fanout({"d": {"source_doc": "d"}})

    assert result["ok"] is True
    assert result["boot_ms"] is None
    assert result["sandbox_count"] == 0


@pytest.mark.asyncio
async def test_one_failing_source_does_not_sink_the_batch(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _mixed(graph_data):
        if graph_data["source_doc"] == "bad":
            return {
                "ok": False,
                "error": "max sandboxes reached",
                "method": "daytona",
                "duration_ms": 3,
            }
        return _ok_item(graph_data["source_doc"])

    monkeypatch.setattr(executor, "verify_graph", _mixed)

    result = await executor.verify_graph_fanout(
        {"bad": {"source_doc": "bad"}, "good": {"source_doc": "good"}}
    )

    assert result["ok"] is True
    by_source = {item["source"]: item for item in result["sources"]}
    assert by_source["bad"]["ok"] is False
    assert by_source["bad"]["error"] == "max sandboxes reached"
    assert by_source["good"]["valid"] is True
    # Only the run that produced a verdict counts toward the sandbox total.
    assert result["sandbox_count"] == 1


@pytest.mark.asyncio
async def test_all_failures_report_the_failed_envelope_without_sources(monkeypatch) -> None:
    executor = DaytonaExecutor()

    async def _down(graph_data):
        return {
            "ok": False,
            "error": "verification unavailable",
            "method": "daytona",
            "duration_ms": 1,
        }

    monkeypatch.setattr(executor, "verify_graph", _down)

    result = await executor.verify_graph_fanout({"d": {"source_doc": "d"}})

    assert result["ok"] is False
    assert result["error"] == FANOUT_ERROR_UNAVAILABLE
    assert "sources" not in result
    assert isinstance(result["wall_ms"], int)


@pytest.mark.asyncio
async def test_a_crashing_verification_is_normalized_never_raw(monkeypatch) -> None:
    """An exception that escapes verify_graph's own envelope is scrubbed."""
    executor = DaytonaExecutor()

    async def _boom(graph_data):
        if graph_data["source_doc"] == "bad":
            raise RuntimeError("bolt://neo4j:hunter2@db:7687 exploded")
        return _ok_item(graph_data["source_doc"])

    monkeypatch.setattr(executor, "verify_graph", _boom)

    result = await executor.verify_graph_fanout(
        {"bad": {"source_doc": "bad"}, "good": {"source_doc": "good"}}
    )

    dumped = str(result)
    assert "hunter2" not in dumped
    assert "bolt://" not in dumped
    by_source = {item["source"]: item for item in result["sources"]}
    assert by_source["bad"]["ok"] is False
    assert by_source["bad"]["error"] == FANOUT_ERROR_UNAVAILABLE


@pytest.mark.asyncio
async def test_event_sink_sees_started_then_done_per_source(monkeypatch) -> None:
    executor = DaytonaExecutor()
    events: list[dict] = []

    async def _fake(graph_data):
        return _ok_item(graph_data["source_doc"])

    async def _sink(event):
        events.append(event)

    monkeypatch.setattr(executor, "verify_graph", _fake)

    await executor.verify_graph_fanout({"d": {"source_doc": "d"}}, on_event=_sink)

    phases = [(e["phase"], e["source"]) for e in events]
    assert phases == [("started", "d"), ("done", "d")]
    assert events[1]["result"]["valid"] is True


@pytest.mark.asyncio
async def test_a_raising_event_sink_never_fails_the_audit(monkeypatch) -> None:
    """Telemetry is never load-bearing."""
    executor = DaytonaExecutor()

    async def _fake(graph_data):
        return _ok_item(graph_data["source_doc"])

    async def _bad_sink(event):
        raise RuntimeError("websocket is gone")

    monkeypatch.setattr(executor, "verify_graph", _fake)

    result = await executor.verify_graph_fanout({"d": {"source_doc": "d"}}, on_event=_bad_sink)

    assert result["ok"] is True
    assert result["sandbox_count"] == 1


def test_fanout_concurrency_env_is_read_once_and_clamped(monkeypatch) -> None:
    monkeypatch.setenv("FANOUT_MAX_CONCURRENCY", "3")
    assert DaytonaExecutor().fanout_max_concurrency == 3

    monkeypatch.setenv("FANOUT_MAX_CONCURRENCY", "9999")
    assert DaytonaExecutor().fanout_max_concurrency == _MAX_FANOUT_CONCURRENCY

    monkeypatch.setenv("FANOUT_MAX_CONCURRENCY", "0")
    assert DaytonaExecutor().fanout_max_concurrency == 1

    monkeypatch.setenv("FANOUT_MAX_CONCURRENCY", "not-a-number")
    assert DaytonaExecutor().fanout_max_concurrency == DEFAULT_FANOUT_CONCURRENCY

    monkeypatch.delenv("FANOUT_MAX_CONCURRENCY")
    assert DaytonaExecutor().fanout_max_concurrency == DEFAULT_FANOUT_CONCURRENCY
