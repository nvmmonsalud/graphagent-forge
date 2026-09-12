"""Contract tests for DaytonaExecutor.verify_graph_sweep.

The sweep audits ONE sub-graph in N sandboxes, for each N in `sizes`, one size
at a time. It is the curve that follows from the fan-out: wall clock roughly
flat as N grows, while the one-at-a-time column scales linearly.

Ran:
    {"ok": True, "replicated": True, "results": [...], "sizes",
     "total_wall_ms", "method"}

Failed:
    {"ok": False, "error": SWEEP_ERROR_UNAVAILABLE, "method", "wall_ms"}
    with NO "results" key — its absence is the machine-readable "nothing ran"
    signal, distinct from a sweep that ran and short-changed a size.

`verify_graph_fanout` is monkeypatched, so these tests pin the COMPOSITION of
the sweep (ordering, replication, short_by, telemetry) rather than re-testing
fan-out, which test_fanout_envelope.py already owns.
"""
from __future__ import annotations

import pytest

from src.agent.daytona_exec import SWEEP_ERROR_UNAVAILABLE, DaytonaExecutor

PAYLOAD = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"source": "a", "target": "b"}]}


def _fanout_ok(n: int, *, wall_ms: int = 100) -> dict:
    """A fan-out result for a batch of `n` sandboxes."""
    return {
        "ok": True,
        "sources": [{"source": f"audit-{i + 1:02d}", "ok": True, "method": "daytona"}
                    for i in range(n)],
        "source_count": n,
        "sandbox_count": n,
        "wall_ms": wall_ms,
        "serial_ms": wall_ms * n,
        "speedup": float(n),
        "concurrency": n,
        "boot_ms": {"count": n, "min": 5, "max": 9, "avg": 7},
        "method": "daytona",
    }


@pytest.fixture
def executor() -> DaytonaExecutor:
    return DaytonaExecutor()


def _recording(executor: DaytonaExecutor, *, sandbox_count=None, ok=True):
    """Patch verify_graph_fanout, recording every call's (graph count, concurrency)."""
    calls: list[dict] = []

    async def _fake(graphs, *, on_event=None, max_concurrency=None):
        calls.append({
            "sources": list(graphs),
            "payloads": list(graphs.values()),
            "max_concurrency": max_concurrency,
        })
        n = len(graphs)
        if not ok:
            return {"ok": False, "error": "fan-out verification unavailable",
                    "method": "daytona", "wall_ms": 1}
        result = _fanout_ok(n)
        if sandbox_count is not None:
            result["sandbox_count"] = sandbox_count(n)
        return result

    executor.verify_graph_fanout = _fake  # type: ignore[assignment]
    return calls


@pytest.mark.asyncio
async def test_one_fanout_per_size_in_the_given_order(executor) -> None:
    calls = _recording(executor)

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[1, 3, 6, 10])

    assert result["ok"] is True
    assert [len(call["sources"]) for call in calls] == [1, 3, 6, 10]
    assert result["sizes"] == [1, 3, 6, 10]


@pytest.mark.asyncio
async def test_each_size_runs_at_its_own_concurrency(executor) -> None:
    """Sizes run sequentially, so each batch may ask for N sandboxes at once."""
    calls = _recording(executor)

    await executor.verify_graph_sweep(PAYLOAD, sizes=[1, 6])

    assert [call["max_concurrency"] for call in calls] == [1, 6]


@pytest.mark.asyncio
async def test_the_payload_is_replicated_not_varied(executor) -> None:
    """N sandboxes get the SAME sub-graph — this measures parallel throughput."""
    calls = _recording(executor)

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[3])

    batch = calls[0]
    assert len(batch["sources"]) == 3
    assert len(set(batch["sources"])) == 3, "sandbox keys must be distinct"
    assert all(payload is PAYLOAD for payload in batch["payloads"])
    assert result["replicated"] is True


@pytest.mark.asyncio
async def test_each_result_item_carries_the_measured_pair(executor) -> None:
    _recording(executor)

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[3])

    item = result["results"][0]
    assert item["n"] == 3
    assert item["ok"] is True
    assert item["sandbox_count"] == 3
    assert item["short_by"] == 0
    assert item["wall_ms"] == 100
    assert item["serial_ms"] == 300
    assert item["speedup"] == 3.0
    assert item["boot_ms"] == {"count": 3, "min": 5, "max": 9, "avg": 7}
    assert item["method"] == "daytona"


@pytest.mark.asyncio
async def test_short_by_surfaces_the_account_ceiling(executor) -> None:
    """A refused sandbox must be visible, never averaged away.

    Daytona answers an over-budget batch with fewer sandboxes than asked for;
    the sweep reports the difference rather than reporting the run as clean.
    """
    _recording(executor, sandbox_count=lambda n: min(n, 10))

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[10, 12])

    assert [item["sandbox_count"] for item in result["results"]] == [10, 10]
    assert [item["short_by"] for item in result["results"]] == [0, 2]


@pytest.mark.asyncio
async def test_no_size_producing_a_verdict_is_a_failed_sweep(executor) -> None:
    _recording(executor, ok=False)

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[1, 3])

    assert result["ok"] is False
    assert result["error"] == SWEEP_ERROR_UNAVAILABLE
    assert "results" not in result
    # Every size still reported its own failure, so the run is diagnosable.
    assert result["wall_ms"] >= 0


@pytest.mark.asyncio
async def test_a_size_that_could_not_run_does_not_sink_the_sweep(executor) -> None:
    """One size failing is a fact about that size, not about the sweep."""
    calls: list[int] = []

    async def _fake(graphs, *, on_event=None, max_concurrency=None):
        n = len(graphs)
        calls.append(n)
        if n == 3:
            return {"ok": False, "error": "fan-out verification unavailable",
                    "method": "daytona", "wall_ms": 2}
        return _fanout_ok(n)

    executor.verify_graph_fanout = _fake  # type: ignore[assignment]

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[1, 3, 6])

    assert result["ok"] is True
    assert calls == [1, 3, 6]
    by_n = {item["n"]: item for item in result["results"]}
    assert by_n[1]["ok"] is True
    assert by_n[6]["ok"] is True
    assert by_n[3]["ok"] is False
    assert by_n[3]["error"] == "fan-out verification unavailable"


@pytest.mark.asyncio
async def test_telemetry_reports_each_size_starting_and_finishing(executor) -> None:
    _recording(executor)
    events: list[dict] = []

    async def _sink(event):
        events.append(event)

    await executor.verify_graph_sweep(PAYLOAD, sizes=[1, 3], on_event=_sink)

    assert [(e["phase"], e["n"]) for e in events] == [
        ("size_started", 1), ("size_done", 1),
        ("size_started", 3), ("size_done", 3),
    ]
    assert events[1]["result"]["n"] == 1


@pytest.mark.asyncio
async def test_a_raising_sink_never_fails_the_sweep(executor) -> None:
    """Telemetry is never load-bearing — same rule as the fan-out sink."""
    _recording(executor)

    async def _bad_sink(event):
        raise RuntimeError("socket closed")

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[1], on_event=_bad_sink)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_total_wall_clock_is_measured_across_the_whole_sweep(executor) -> None:
    _recording(executor)

    result = await executor.verify_graph_sweep(PAYLOAD, sizes=[1])

    assert isinstance(result["total_wall_ms"], int)
    assert result["total_wall_ms"] >= 0
