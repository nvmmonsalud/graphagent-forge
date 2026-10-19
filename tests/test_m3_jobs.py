"""Real on-disk durable queue recovery, fencing and cross-manager tests."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.agent.durable_jobs import (
    COMMAND_TTL,
    MAX_EVENTS,
    DurableJobManager,
    IdempotencyConflict,
    RetryableJobError,
    _LeaseLost,
)
from src.agent.jobs import JobQueueFull
from src.agent.state import StateDatabase

PUBLIC_KEYS = {
    "id", "kind", "status", "stage", "params", "result", "error",
    "created_at", "started_at", "finished_at",
}
COMMAND = {"version": 1, "kind": "text", "text": "private body", "source": "note"}
PARAMS = {"source": "note", "text_chars": 12}


async def success(command, progress, operation_id):
    await progress("extracting")
    return {"success": True, "nodes": 3}


@pytest.fixture
async def factory(tmp_path):
    path = tmp_path / "private" / "state.sqlite3"
    managers = []

    async def create(handler=success, **kwargs):
        manager = DurableJobManager(StateDatabase(path), handler, **kwargs)
        await manager.initialize()
        managers.append(manager)
        return manager

    yield create
    for manager in managers:
        await manager.shutdown()


async def terminal(manager, job_id):
    return await asyncio.wait_for(manager.wait(job_id), 5)


@pytest.mark.asyncio
async def test_reopen_retains_command_result_events_and_private_purge(factory):
    first = await factory()
    record = await first.submit_command("text", PARAMS, COMMAND, "request one")
    assert set(record) == PUBLIC_KEYS
    assert "private body" not in json.dumps(record)
    reopened = await factory()
    assert await reopened.get(record["id"]) == record
    await reopened.start()
    result = await terminal(reopened, record["id"])
    assert result["status"] == "succeeded"
    assert result["stage"] == "extracting"
    assert result["result"] == {"success": True, "nodes": 3}
    events = await reopened.events(record["id"])
    assert [event["status"] for event in events] == [
        "queued", "running", "running", "succeeded"
    ]
    assert await first.events(record["id"], after=events[1]["id"]) == events[2:]
    private = await first.db.run(lambda conn: dict(conn.execute(
        "SELECT command,idempotency_key FROM durable_jobs").fetchone()))
    assert private["command"] is None
    assert private["idempotency_key"] != "request one"
    assert await first.get(record["id"]) == result


@pytest.mark.asyncio
async def test_cross_manager_idempotency_and_capacity_atomic(factory):
    a = await factory(max_active=1)
    b = await factory(max_active=1)
    one, two = await asyncio.gather(
        a.submit_command("text", PARAMS, COMMAND, "same-key"),
        b.submit_command("text", PARAMS, dict(reversed(list(COMMAND.items()))), "same-key"),
    )
    assert one["id"] == two["id"]
    with pytest.raises(IdempotencyConflict):
        await b.submit_command("text", PARAMS, {**COMMAND, "text": "changed"}, "same-key")
    with pytest.raises(JobQueueFull):
        await b.submit_command("text", PARAMS, COMMAND)
    assert len(await a.list()) == 1


@pytest.mark.asyncio
async def test_atomic_new_submission_capacity_race(factory):
    a, b = await factory(max_active=1), await factory(max_active=1)
    results = await asyncio.gather(
        a.submit_command("text", PARAMS, COMMAND), b.submit_command("text", PARAMS, COMMAND),
        return_exceptions=True,
    )
    assert sum(isinstance(item, JobQueueFull) for item in results) == 1
    assert len(await a.list()) == 1


@pytest.mark.asyncio
async def test_shared_running_permits_and_attempt_budget(factory):
    running = 0
    highest = 0
    called = 0
    release = asyncio.Event()

    async def handler(command, progress, operation_id):
        nonlocal running, highest, called
        running += 1
        called += 1
        highest = max(highest, running)
        try:
            await release.wait()
            return {"success": True}
        finally:
            running -= 1

    a = await factory(handler, workers=1, attempt_budget=1)
    b = await factory(handler, workers=1, attempt_budget=1)
    first = await a.submit_command("text", PARAMS, COMMAND)
    second = await a.submit_command("text", PARAMS, COMMAND)
    await asyncio.gather(a.start(), b.start())
    for _ in range(100):
        if called:
            break
        await asyncio.sleep(0.01)
    assert called == 1
    assert highest == 1
    release.set()
    assert (await terminal(a, first["id"]))["status"] == "succeeded"
    await asyncio.sleep(0.15)
    blocked = await b.get(second["id"])
    assert blocked["status"] == "queued"
    assert blocked["stage"] == "waiting_budget"
    assert called == 1
    count = await a.db.run(lambda conn: conn.execute(
        "SELECT sum(attempts) FROM durable_attempt_windows").fetchone()[0])
    assert count == 1


@pytest.mark.asyncio
async def test_shared_running_cap_even_with_remaining_budget(factory):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(command, progress, operation_id):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"success": True}

    a = await factory(handler, workers=1)
    b = await factory(handler, workers=1)
    one = await a.submit_command("text", PARAMS, COMMAND)
    two = await a.submit_command("text", PARAMS, COMMAND)
    await asyncio.gather(a.start(), b.start())
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.sleep(0.15)
    assert calls == 1
    release.set()
    await terminal(a, one["id"])
    await terminal(b, two["id"])
    assert calls == 2


@pytest.mark.asyncio
async def test_expired_claim_recovery_stale_tokens_and_attempt_cap(factory):
    a = await factory(workers=1, max_attempts=2)
    record = await a.submit_command("text", PARAMS, COMMAND)
    claim = await a._claim()
    await a.db.run(lambda conn: conn.execute(
        "UPDATE durable_jobs SET lease_until=0 WHERE id=?", (record["id"],)), write=True)
    b = await factory(workers=1, max_attempts=2)
    reclaimed = await b._claim()
    assert reclaimed["id"] == record["id"]
    assert reclaimed["attempts"] == 2
    assert reclaimed["lease_token"] != claim["lease_token"]
    with pytest.raises(_LeaseLost):
        await a._progress(claim["id"], claim["lease_token"], "stale")
    assert await a._finish(claim["id"], claim["lease_token"],
                           status="succeeded", result={"success": True}) is None
    await a.db.run(lambda conn: conn.execute(
        "UPDATE durable_jobs SET lease_until=0 WHERE id=?", (record["id"],)), write=True)
    assert await b._claim() is None
    assert (await a.get(record["id"]))["status"] == "failed"
    assert await a.db.run(lambda conn: conn.execute(
        "SELECT command FROM durable_jobs").fetchone()[0]) is None


@pytest.mark.asyncio
async def test_cancel_cleanup_retains_permit_and_lease_across_managers(factory):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    allow_cleanup = asyncio.Event()
    calls = []

    async def handler(command, progress, operation_id):
        calls.append(operation_id)
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await allow_cleanup.wait()
                raise
        return {"success": True}

    a = await factory(handler, workers=1, lease_seconds=0.15)
    b = await factory(handler, workers=1, lease_seconds=0.15)
    first = await a.submit_command("text", PARAMS, COMMAND)
    second = await a.submit_command("text", PARAMS, COMMAND)
    await asyncio.gather(a.start(), b.start())
    await asyncio.wait_for(entered.wait(), 3)
    await b.cancel(first["id"])
    await asyncio.wait_for(cleaning.wait(), 3)
    await asyncio.sleep(0.4)  # More than two lease periods; cleanup must keep renewing.
    assert (await a.get(first["id"]))["status"] == "running"
    assert calls == [first["id"]]
    allow_cleanup.set()
    assert (await terminal(b, first["id"]))["status"] == "cancelled"
    assert (await terminal(a, second["id"]))["status"] == "succeeded"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_shutdown_cleans_up_before_relinquishing_and_reopen_replays(factory):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    cleanup = asyncio.Event()

    async def handler(command, progress, operation_id):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await cleanup.wait()

    first = await factory(handler, workers=1, lease_seconds=0.15)
    job = await first.submit_command("text", PARAMS, COMMAND)
    await first.start()
    await asyncio.wait_for(entered.wait(), 3)
    shutdown = asyncio.create_task(first.shutdown())
    await asyncio.wait_for(cleaning.wait(), 3)
    await asyncio.sleep(0.2)
    assert not shutdown.done()
    assert (await first.get(job["id"]))["status"] == "running"
    cleanup.set()
    await asyncio.wait_for(shutdown, 3)
    assert (await first.get(job["id"]))["status"] == "queued"
    reopened = await factory(workers=1, lease_seconds=0.15)
    await reopened.start()
    assert (await terminal(reopened, job["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_only_explicit_retryable_errors_retry(factory, retry):
    calls = 0

    async def handler(command, progress, operation_id):
        nonlocal calls
        calls += 1
        if retry:
            raise RetryableJobError("private provider detail")
        raise RuntimeError("private provider detail")

    manager = await factory(handler)
    job = await manager.submit_command("text", PARAMS, COMMAND)
    await manager.start()
    result = await terminal(manager, job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "internal error"
    assert calls == (2 if retry else 1)
    assert "private provider detail" not in json.dumps(await manager.events(job["id"]))


@pytest.mark.asyncio
async def test_unknown_persisted_command_permanent_failure(factory):
    called = False

    async def handler(command, progress, operation_id):
        nonlocal called
        called = True
        return {"success": True}

    manager = await factory(handler)
    record = await manager.submit_command("text", PARAMS, COMMAND)
    await manager.db.run(lambda conn: conn.execute(
        "UPDATE durable_jobs SET command=? WHERE id=?", ('{"version":9}', record["id"])
    ), write=True)
    await manager.start()
    assert (await terminal(manager, record["id"]))["status"] == "failed"
    assert called is False
    attempts = await manager.db.run(lambda conn: conn.execute(
        "SELECT attempts FROM durable_jobs").fetchone()[0])
    assert attempts == 1


@pytest.mark.asyncio
async def test_queued_expiry_and_cancel_purge_and_terminal_eviction(factory):
    manager = await factory(history_limit=1)
    old = await manager.submit_command("text", PARAMS, COMMAND, "old-key")
    await manager.db.run(lambda conn: conn.execute(
        "UPDATE durable_jobs SET created_epoch=? WHERE id=?",
        (time.time() - COMMAND_TTL - 1, old["id"])), write=True)
    # Maintenance expires old work even while workers have not been started.
    new = await manager.submit_command("text", PARAMS, COMMAND, "new-key")
    assert (await manager.get(old["id"]))["status"] == "failed"
    assert (await manager.cancel(new["id"]))["status"] == "cancelled"
    assert await manager.get(old["id"]) is None
    assert await manager.events(old["id"]) == []
    private = await manager.db.run(lambda conn: conn.execute(
        "SELECT command FROM durable_jobs WHERE id=?", (new["id"],)).fetchone()[0])
    assert private is None
    replacement = await manager.submit_command("text", PARAMS, COMMAND, "old-key")
    assert replacement["id"] != old["id"]


@pytest.mark.asyncio
async def test_bounded_events_and_results(factory):
    manager = await factory()
    record = await manager.submit_command("text", PARAMS, COMMAND)
    claim = await manager._claim()
    for _ in range(MAX_EVENTS + 4):
        await manager._progress(record["id"], claim["lease_token"], "extracting")
    assert len(await manager.events(record["id"], limit=10000)) == 100
    count = await manager.db.run(lambda conn: conn.execute(
        "SELECT count(*) FROM durable_job_events").fetchone()[0])
    assert count == MAX_EVENTS
    with pytest.raises(ValueError):
        await manager._finish(record["id"], claim["lease_token"],
                              status="succeeded", result={"text": "x" * 1_000_001})


@pytest.mark.asyncio
async def test_invalid_submission_and_policy_do_not_store_payload(factory):
    manager = await factory()
    for key in ("", "x" * 129, "newline\nkey", "nonasciié"):
        with pytest.raises(ValueError):
            await manager.submit_command("text", PARAMS, COMMAND, key)
    with pytest.raises(ValueError):
        await manager.submit_command("text", {"text": "private"}, COMMAND)
    with pytest.raises(ValueError):
        await manager.submit_command("url", PARAMS, COMMAND)
    with pytest.raises(ValueError):
        await manager.submit_command("text", PARAMS, {"version": 999})
    assert await manager.list() == []
    await manager.submit_command("text", PARAMS, COMMAND)
    with pytest.raises(ValueError, match="policy"):
        await factory(workers=3)


@pytest.mark.asyncio
async def test_timeout_and_unsuccessful_result_are_permanent(factory):
    async def handler(command, progress, operation_id):
        await asyncio.sleep(5)
        return {"success": True}

    manager = await factory(handler, job_timeout=0.03)
    record = await manager.submit_command("text", PARAMS, COMMAND)
    await manager.start()
    assert (await terminal(manager, record["id"]))["error"] == "timed out"


@pytest.mark.asyncio
async def test_heartbeat_failure_does_not_release_before_handler_cleanup(factory, monkeypatch):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    allow_cleanup = asyncio.Event()
    calls = []

    async def handler(command, progress, operation_id):
        calls.append(operation_id)
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await allow_cleanup.wait()
        return {"success": True}

    owner = await factory(handler, workers=1, lease_seconds=1)
    observer = await factory(handler, workers=1, lease_seconds=1)
    one = await owner.submit_command("text", PARAMS, COMMAND)
    two = await owner.submit_command("text", PARAMS, COMMAND)
    original = owner._heartbeat
    failures = 2

    async def faulty_heartbeat(job_id, token):
        nonlocal failures
        if failures:
            failures -= 1
            raise OSError("temporary storage failure")
        return await original(job_id, token)

    monkeypatch.setattr(owner, "_heartbeat", faulty_heartbeat)
    await owner.start()
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(cleaning.wait(), 3)
    await observer.start()
    try:
        await asyncio.sleep(0.55)
        assert (await observer.get(one["id"]))["status"] == "running"
        assert calls == [one["id"]]
    finally:
        allow_cleanup.set()
    assert (await terminal(observer, one["id"]))["status"] == "failed"
    assert (await terminal(observer, two["id"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_shutdown_during_cancel_cleanup_does_not_cancel_cleanup_twice(factory):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    allow_cleanup = asyncio.Event()
    finished_cleanup = asyncio.Event()

    async def handler(command, progress, operation_id):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await allow_cleanup.wait()
            finished_cleanup.set()

    manager = await factory(handler, workers=1, lease_seconds=0.15)
    job = await manager.submit_command("text", PARAMS, COMMAND)
    await manager.start()
    await asyncio.wait_for(entered.wait(), 3)
    await manager.cancel(job["id"])
    await asyncio.wait_for(cleaning.wait(), 3)
    stopping = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0.2)
    assert not stopping.done()
    assert (await manager.get(job["id"]))["status"] == "running"
    allow_cleanup.set()
    await asyncio.wait_for(stopping, 3)
    assert finished_cleanup.is_set()
    assert (await manager.get(job["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_unsuccessful_result_never_retries(factory):
    calls = 0

    async def handler(command, progress, operation_id):
        nonlocal calls
        calls += 1
        return {"success": False, "error": "private detail"}

    manager = await factory(handler)
    job = await manager.submit_command("text", PARAMS, COMMAND)
    await manager.start()
    result = await terminal(manager, job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "internal error"
    assert result["result"] is None
    assert calls == 1


@pytest.mark.asyncio
async def test_policy_change_rejected_even_when_existing_manager_is_idle(factory):
    stale = await factory(attempt_budget=100)
    with pytest.raises(ValueError, match="policy differs"):
        await factory(attempt_budget=1)
    job = await stale.submit_command("text", PARAMS, COMMAND)
    assert (await stale._claim())["id"] == job["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["job_timeout", "lease_seconds", "budget_window_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_raw_timing_settings_rejected(factory, setting, value):
    with pytest.raises(ValueError, match="finite"):
        await factory(**{setting: value})


@pytest.mark.asyncio
async def test_retry_is_delayed_and_claim_does_not_reserve_budget_early(factory):
    manager = await factory()
    record = await manager.submit_command("text", PARAMS, COMMAND)
    claim = await manager._claim()
    retried = await manager._finish(record["id"], claim["lease_token"], status="failed",
                                    error="internal error", retry=True)
    assert retried["status"] == "queued"
    assert await manager._claim() is None
    state = await manager.db.run(lambda conn: dict(conn.execute(
        "SELECT available_epoch,attempts FROM durable_jobs").fetchone()))
    assert state["available_epoch"] > time.time()
    assert state["attempts"] == 1
    await manager.db.run(lambda conn: conn.execute(
        "UPDATE durable_jobs SET available_epoch=0"), write=True)
    assert (await manager._claim())["attempts"] == 2


@pytest.mark.asyncio
async def test_existing_incompatible_table_schema_fails_closed(factory):
    manager = await factory()
    await manager.db.run(lambda conn: conn.execute(
        "ALTER TABLE durable_jobs ADD COLUMN unsupported TEXT"), write=True)
    with pytest.raises(ValueError, match="unsupported durable job table schema"):
        await factory()


@pytest.mark.asyncio
async def test_existing_events_missing_cascade_fails_closed(factory):
    manager = await factory()

    def replace(conn):
        conn.execute("DROP TABLE durable_job_events")
        conn.execute("""CREATE TABLE durable_job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            status TEXT NOT NULL, stage TEXT, attempt INTEGER NOT NULL,
            created_at TEXT NOT NULL)""")

    await manager.db.run(replace, write=True)
    with pytest.raises(ValueError, match="event foreign key"):
        await factory()
