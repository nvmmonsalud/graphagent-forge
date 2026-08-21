"""Tests for src.agent.jobs — the async in-memory ingest job queue, offline.

No agent/Neo4j collaborators here: `run` callables are plain stubs. Every
test builds its manager through the `manager_factory` fixture, which starts
it inside the test's running loop and always awaits `shutdown()` in
teardown, matching the lifecycle contract (`start()` needs a running loop;
`shutdown()` is idempotent).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.agent.jobs import JobManager, JobQueueFull

PUBLIC_KEYS = {
    "id", "kind", "status", "stage", "params", "result", "error",
    "created_at", "started_at", "finished_at",
}


@pytest.fixture
async def manager_factory():
    """Build+start JobManagers inside the test's running loop; shut them all
    down on teardown regardless of whether the test already did."""
    created: list[JobManager] = []

    def _factory(**kwargs) -> JobManager:
        manager = JobManager(**kwargs)
        manager.start()
        created.append(manager)
        return manager

    yield _factory

    for manager in created:
        await manager.shutdown()


async def _wait_for(predicate, *, attempts: int = 1000) -> None:
    """Poll `predicate()` by yielding the loop, without a wall-clock sleep."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met in time")


# ------------------------------------------------------------------
# Lifecycle: submit -> wait -> succeeded
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lifecycle_submit_wait_succeeded(manager_factory) -> None:
    manager = manager_factory()

    async def run(progress):
        return {"nodes": 3}

    record = manager.submit("text", {"source": "manual"}, run)

    assert set(record.keys()) == PUBLIC_KEYS
    assert record["status"] == "queued"
    assert record["kind"] == "text"
    assert record["params"] == {"source": "manual"}
    assert record["id"]
    assert record["created_at"]
    assert record["started_at"] is None
    assert record["finished_at"] is None
    assert record["result"] is None
    assert record["error"] is None

    final = await manager.wait(record["id"])

    assert final["status"] == "succeeded"
    assert final["result"] == {"nodes": 3}
    assert final["error"] is None
    assert final["started_at"] is not None
    assert final["finished_at"] is not None


@pytest.mark.asyncio
async def test_wait_unknown_id_raises_key_error(manager_factory) -> None:
    manager = manager_factory()
    with pytest.raises(KeyError):
        await manager.wait("does-not-exist")


@pytest.mark.asyncio
async def test_get_unknown_id_returns_none(manager_factory) -> None:
    manager = manager_factory()
    assert manager.get("does-not-exist") is None


# ------------------------------------------------------------------
# Stage reporting
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stage_reporting_keeps_last_stage_on_terminal(manager_factory) -> None:
    manager = manager_factory()

    async def run(progress):
        await progress("extracting")
        return {"ok": True}

    record = manager.submit("url", {}, run)
    final = await manager.wait(record["id"])

    assert final["status"] == "succeeded"
    assert final["stage"] == "extracting"


# ------------------------------------------------------------------
# Failure: unhandled exception -> "internal error", raw text never leaks
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failure_reports_internal_error_and_hides_raw_text(manager_factory) -> None:
    manager = manager_factory()

    async def run(progress):
        raise RuntimeError("bolt://secret-uri leaked in a stack trace")

    record = manager.submit("text", {}, run)
    final = await manager.wait(record["id"])

    assert final["status"] == "failed"
    assert final["error"] == "internal error"
    dumped = json.dumps(final)
    assert "bolt://secret-uri leaked in a stack trace" not in dumped
    assert "RuntimeError" not in dumped


# ------------------------------------------------------------------
# Timeout
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_reports_timed_out(manager_factory) -> None:
    manager = manager_factory(job_timeout=0.05)

    async def run(progress):
        await asyncio.sleep(1)
        return {}

    record = manager.submit("url", {}, run)
    final = await manager.wait(record["id"])

    assert final["status"] == "failed"
    assert final["error"] == "timed out"


# ------------------------------------------------------------------
# Cap: max_active
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_max_active_cap_raises_job_queue_full(manager_factory) -> None:
    manager = manager_factory(max_active=1)
    gate = asyncio.Event()

    async def run(progress):
        await gate.wait()
        return {}

    first = manager.submit("url", {}, run)

    with pytest.raises(JobQueueFull):
        manager.submit("url", {}, run)

    # Drain the parked job so the fixture's shutdown() doesn't have to.
    gate.set()
    final = await manager.wait(first["id"])
    assert final["status"] == "succeeded"


# ------------------------------------------------------------------
# Eviction
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_eviction_beyond_history_limit(manager_factory) -> None:
    manager = manager_factory(history_limit=2)

    ids: list[str] = []
    for i in range(4):
        async def run(progress, i=i):
            return {"i": i}

        record = manager.submit("text", {"i": i}, run)
        await manager.wait(record["id"])
        ids.append(record["id"])

    # Oldest two evicted, newest two retained.
    assert manager.get(ids[0]) is None
    assert manager.get(ids[1]) is None
    assert manager.get(ids[2]) is not None
    assert manager.get(ids[3]) is not None

    listing = manager.list()
    assert [r["id"] for r in listing] == [ids[3], ids[2]]

    assert len(manager.list(limit=1)) == 1


# ------------------------------------------------------------------
# Shutdown
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shutdown_fails_parked_jobs_and_unblocks_wait(manager_factory) -> None:
    manager = manager_factory()
    gate = asyncio.Event()

    async def run(progress):
        await gate.wait()
        return {}

    record = manager.submit("url", {}, run)

    await manager.shutdown()

    stored = manager.get(record["id"])
    assert stored["status"] == "failed"
    assert stored["error"] == "server shutdown"

    # wait() must not hang after shutdown.
    waited = await manager.wait(record["id"])
    assert waited["status"] == "failed"
    assert waited["error"] == "server shutdown"

    # Idempotent: calling again must not raise or hang.
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_with_queued_job_marks_it_failed(manager_factory) -> None:
    manager = manager_factory(workers=1, max_active=5)
    gate = asyncio.Event()

    async def blocking_run(progress):
        await gate.wait()
        return {}

    async def queued_run(progress):
        return {}

    # First job occupies the single worker; second stays queued behind it.
    first = manager.submit("url", {}, blocking_run)
    second = manager.submit("url", {}, queued_run)
    assert manager.get(second["id"])["status"] == "queued"

    await manager.shutdown()

    assert manager.get(first["id"])["status"] == "failed"
    assert manager.get(second["id"])["status"] == "failed"
    assert manager.get(second["id"])["error"] == "server shutdown"


# ------------------------------------------------------------------
# Broadcast
# ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_broadcast_covers_queued_running_stage_terminal(manager_factory) -> None:
    calls: list[dict] = []

    async def recorder(payload):
        calls.append(payload)

    manager = manager_factory(broadcast=recorder)
    gate = asyncio.Event()

    async def run(progress):
        await progress("extracting")
        await gate.wait()
        return {"ok": True}

    record = manager.submit("url", {}, run)

    # queued, running, stage="extracting" -> at least 3 emits before we gate.
    await _wait_for(lambda: len(calls) >= 3)
    gate.set()
    final = await manager.wait(record["id"])

    assert all(c["type"] == "job_update" for c in calls)
    statuses = [c["job"]["status"] for c in calls]
    assert "queued" in statuses
    assert "running" in statuses
    assert any(c["job"]["stage"] == "extracting" for c in calls)

    assert final["status"] == "succeeded"
    assert calls[-1]["job"]["status"] == "succeeded"
    assert calls[-1]["job"]["id"] == record["id"]


@pytest.mark.asyncio
async def test_broadcast_raising_does_not_fail_job(manager_factory) -> None:
    async def bad_broadcast(payload):
        raise RuntimeError("boom")

    manager = manager_factory(broadcast=bad_broadcast)

    async def run(progress):
        return {"ok": True}

    record = manager.submit("url", {}, run)
    final = await manager.wait(record["id"])

    assert final["status"] == "succeeded"
    assert final["result"] == {"ok": True}
