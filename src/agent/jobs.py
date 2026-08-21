"""In-memory ingest job queue.

Generic on purpose: this module knows nothing about GraphAgent, FastAPI or the
WebSocket manager. Callers hand :meth:`JobManager.submit` an opaque ``params``
dict plus an async ``run`` callable; the manager owns status bookkeeping,
timeouts, history eviction and progress broadcasting.

Deliberately imports neither ``src.main`` nor ``src.agent.core`` — the
broadcast sink is injected, which keeps this importable from anywhere without
a circular import.

State is per-process and in memory: everything is lost on restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# A job's progress reporter: `await progress("extracting")`.
ProgressCb = Callable[[str], Awaitable[None]]
# The unit of work: receives a progress reporter, returns the result payload.
RunFn = Callable[[ProgressCb], Awaitable[dict]]
# Injected sink for `job_update` envelopes (e.g. ConnectionManager.broadcast).
BroadcastFn = Callable[[dict[str, Any]], Awaitable[None]]

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

# The only three strings that may ever reach a client via `record["error"]`.
# Raw exception text is logged server-side and never stored on the record.
ERR_INTERNAL = "internal error"
ERR_TIMEOUT = "timed out"
ERR_SHUTDOWN = "server shutdown"


class JobQueueFull(Exception):
    """Raised by :meth:`JobManager.submit` when the queue cannot take more work."""


def _utcnow() -> str:
    # `UTC` is the 3.11+ alias of `timezone.utc` — same value, ruff-clean (UP017).
    return datetime.now(UTC).isoformat()


class _Job:
    """Internal record. Private fields are excluded by :meth:`to_public`."""

    __slots__ = (
        "id",
        "kind",
        "status",
        "stage",
        "params",
        "result",
        "error",
        "created_at",
        "started_at",
        "finished_at",
        "_run",
        "_done",
        "_seq",
    )

    def __init__(self, *, job_id: str, kind: str, params: dict, run: RunFn, seq: int):
        self.id = job_id
        self.kind = kind
        self.status: str = QUEUED
        self.stage: str | None = None
        self.params: dict = dict(params or {})
        self.result: dict | None = None
        self.error: str | None = None
        self.created_at: str = _utcnow()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self._run: RunFn = run
        self._done = asyncio.Event()
        self._seq = seq

    def to_public(self) -> dict[str, Any]:
        """JSON-serializable snapshot — never leaks `_run`/`_done`."""
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "params": dict(self.params),
            "result": dict(self.result) if isinstance(self.result, dict) else self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    """Bounded, in-memory async job queue with a fixed pool of workers."""

    def __init__(
        self,
        *,
        broadcast: BroadcastFn | None = None,
        workers: int = 2,
        max_active: int = 20,
        history_limit: int = 100,
        job_timeout: float | None = None,
    ):
        self._broadcast = broadcast
        self._worker_count = max(1, int(workers))
        self._max_active = max(1, int(max_active))
        self._history_limit = max(1, int(history_limit))
        if job_timeout is None:
            job_timeout = _timeout_from_env()
        self.job_timeout: float | None = job_timeout

        self._jobs: dict[str, _Job] = {}
        self._finished: deque[str] = deque()  # finished job ids, oldest first
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._emit_tasks: set[asyncio.Task] = set()
        self._seq = 0
        self._started = False
        self._closed = False

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Spawn the worker tasks. Idempotent; requires a running event loop."""
        if self._started or self._closed:
            return
        loop = asyncio.get_running_loop()  # RuntimeError if called outside a loop
        self._workers = [
            loop.create_task(self._worker(i), name=f"job-worker-{i}")
            for i in range(self._worker_count)
        ]
        self._started = True

    async def shutdown(self) -> None:
        """Stop workers and fail every job still in flight. Idempotent."""
        if self._closed:
            return
        self._closed = True

        for task in self._workers:
            task.cancel()
        if self._workers:
            # A cancel that lands exactly as a job's inner future completes can
            # be swallowed by asyncio.wait_for — the worker survives and parks
            # on queue.get(). The loop's `while not self._closed` check catches
            # most of those; re-cancelling anything still pending catches the
            # rest, so shutdown can never hang on a live worker.
            done, pending = await asyncio.wait(self._workers, timeout=5.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._workers = []

        # Drain the backlog so nothing is left dangling in the queue.
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

        # Anything not terminal (queued, or running under a worker that never
        # got to its cancel handler) fails as a shutdown casualty.
        for record in list(self._jobs.values()):
            if record.status in (QUEUED, RUNNING):
                self._finalize(record, FAILED, error=ERR_SHUTDOWN)
                await self._emit(record)
        self._evict()

        # Let any fire-and-forget emits land before the loop goes away.
        pending = list(self._emit_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # ----------------------------------------------------------- public API
    def submit(self, kind: str, params: dict, run: RunFn) -> dict[str, Any]:
        """Register a job and enqueue it. Returns the public record."""
        if self._closed:
            raise JobQueueFull("job manager is shutting down")
        active = sum(1 for j in self._jobs.values() if j.status in (QUEUED, RUNNING))
        if active >= self._max_active:
            raise JobQueueFull(f"too many active jobs ({active}/{self._max_active})")

        self._seq += 1
        record = _Job(
            job_id=uuid.uuid4().hex,
            kind=kind,
            params=params,
            run=run,
            seq=self._seq,
        )
        self._jobs[record.id] = record
        # Schedule the `queued` emit before waking a worker so clients observe
        # the status transitions in order.
        self._schedule_emit(record)
        self._queue.put_nowait(record.id)
        return record.to_public()

    def get(self, job_id: str) -> dict[str, Any] | None:
        record = self._jobs.get(job_id)
        return record.to_public() if record is not None else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        """Newest-first snapshot of the registry."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        if limit <= 0:
            return []
        records = sorted(
            self._jobs.values(), key=lambda j: (j.created_at, j._seq), reverse=True
        )
        return [r.to_public() for r in records[:limit]]

    async def wait(self, job_id: str) -> dict[str, Any]:
        """Block until the job reaches a terminal state, then return it."""
        record = self._jobs.get(job_id)
        if record is None:
            raise KeyError(job_id)
        await record._done.wait()
        return record.to_public()

    # -------------------------------------------------------------- internals
    async def _emit(self, record: _Job) -> None:
        """Push a `job_update` envelope. A bad client must never fail a job."""
        await self._send({"type": "job_update", "job": record.to_public()})

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(payload)
        except Exception as exc:  # broadcasting is strictly best-effort
            log.warning("job_update broadcast failed: %s", exc)

    def _schedule_emit(self, record: _Job) -> None:
        """Fire-and-forget emit for sync call sites (submit, cancel handler).

        The snapshot is taken now, not when the task runs, so a fast follow-up
        transition cannot rewrite the payload of an already-queued emit.
        """
        if self._broadcast is None:
            return
        payload = {"type": "job_update", "job": record.to_public()}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._send(payload))
        self._emit_tasks.add(task)
        task.add_done_callback(self._emit_tasks.discard)

    def _finalize(
        self,
        record: _Job,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        if record._done.is_set():  # already terminal — never finalize twice
            return
        record.status = status
        record.result = result
        record.error = error
        record.finished_at = _utcnow()
        # `stage` is intentionally left as-is: the last stage reached is useful
        # context on both succeeded and failed records.
        self._finished.append(record.id)
        record._done.set()

    def _evict(self) -> None:
        """Trim finished history down to `history_limit`, oldest first."""
        while len(self._finished) > self._history_limit:
            old_id = self._finished.popleft()
            self._jobs.pop(old_id, None)

    async def _worker(self, index: int) -> None:
        # `not self._closed` (not `while True`): if shutdown's cancel was
        # swallowed by wait_for's completion race inside _run_job, the worker
        # must exit here instead of parking on an empty queue forever.
        while not self._closed:
            job_id = await self._queue.get()
            # Yield once so the `queued` emit scheduled by submit() is delivered
            # before this job's `running` emit — clients must never see a stale
            # `queued` land after `running`.
            await asyncio.sleep(0)
            try:
                record = self._jobs.get(job_id)
                if record is None or record.status != QUEUED:
                    continue
                await self._run_job(record)
            finally:
                self._queue.task_done()

    async def _run_job(self, record: _Job) -> None:
        record.status = RUNNING
        record.started_at = _utcnow()
        await self._emit(record)

        async def progress_cb(stage: str) -> None:
            record.stage = stage if stage is None else str(stage)
            await self._emit(record)

        try:
            result = await asyncio.wait_for(record._run(progress_cb), timeout=self.job_timeout)
        except TimeoutError:
            log.warning("Job %s (%s) timed out after %ss", record.id, record.kind, self.job_timeout)
            self._finalize(record, FAILED, error=ERR_TIMEOUT)
        except asyncio.CancelledError:
            # Worker is going away (shutdown). Mark, notify, then propagate so
            # the task actually ends up cancelled.
            self._finalize(record, FAILED, error=ERR_SHUTDOWN)
            self._schedule_emit(record)
            self._evict()
            raise
        except Exception:
            # Raw exception text stays in the log; clients only see the literal.
            log.exception("Job %s (%s) failed", record.id, record.kind)
            self._finalize(record, FAILED, error=ERR_INTERNAL)
        else:
            if not isinstance(result, dict):
                result = {"result": result}
            self._finalize(record, SUCCEEDED, result=result)

        await self._emit(record)
        self._evict()


def _timeout_from_env() -> float | None:
    raw = os.getenv("INGEST_JOB_TIMEOUT", "600")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning("Invalid INGEST_JOB_TIMEOUT=%r — falling back to 600s", raw)
        return 600.0
    return value if value > 0 else None
