"""Single-host durable ingest queue with fenced SQLite leases.

Only short database callbacks run in transactions. Provider work and cancellation
cleanup run outside transactions; a running permit stays held until cleanup ends.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from src.agent.jobs import JobQueueFull, _timeout_from_env

log = logging.getLogger(__name__)
TERMINAL = ("succeeded", "failed", "cancelled")
MAX_COMMAND_BYTES = 4_300_000
MAX_RESULT_BYTES = 1_000_000
MAX_EVENTS = 200
COMMAND_TTL = 24 * 3600


class IdempotencyConflict(Exception):
    """A retained key already identifies a different command."""


class RetryableJobError(Exception):
    """Explicit opt-in to a bounded retry of a safe-to-replay operation."""


class _LeaseLost(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any, limit: int) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                     allow_nan=False)
    if len(raw.encode("utf-8")) > limit:
        raise ValueError("job payload exceeds storage limit")
    return raw


def _public(row) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "status": row["status"],
        "stage": row["stage"], "params": json.loads(row["params"]),
        "result": json.loads(row["result"]) if row["result"] is not None else None,
        "error": row["error"], "created_at": row["created_at"],
        "started_at": row["started_at"], "finished_at": row["finished_at"],
    }


class DurableJobManager:
    """SQLite-backed bounded queue, usable by multiple managers on one host.

    Managers sharing a database must use the same policy. The first initialize
    persists the policy and later initializations reject incompatible settings.
    """

    def __init__(self, db, handler, *, broadcast=None, workers=2, max_active=20,
                 history_limit=100, job_timeout=None, lease_seconds=30,
                 max_attempts=2, attempt_budget=100, budget_window_seconds=3600):
        self.db = db
        self.handler = handler
        self._broadcast = broadcast
        self.workers = max(1, int(workers))
        self.max_active = max(1, int(max_active))
        self.history_limit = max(1, int(history_limit))
        configured_timeout = _timeout_from_env() if job_timeout is None else job_timeout
        raw_timings = (configured_timeout or 600, lease_seconds, budget_window_seconds)
        if not all(math.isfinite(float(value)) for value in raw_timings):
            raise ValueError("job timing settings must be finite")
        self.job_timeout = min(600.0, max(0.01, float(configured_timeout or 600)))
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self.attempt_budget = max(1, int(attempt_budget))
        self.budget_window_seconds = max(0.1, float(budget_window_seconds))
        if not all(math.isfinite(value) for value in (
            self.job_timeout, self.lease_seconds, self.budget_window_seconds
        )):
            raise ValueError("job timing settings must be finite")
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._started = False
        self._wake = asyncio.Event()
        self._policy = None

    async def initialize(self):
        await self.db.initialize()

        def create(conn):
            conn.execute("""CREATE TABLE IF NOT EXISTS durable_jobs (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL, stage TEXT, params TEXT NOT NULL,
                command TEXT, command_hash TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                result TEXT, error TEXT, created_at TEXT NOT NULL, created_epoch REAL NOT NULL,
                started_at TEXT, finished_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
                available_epoch REAL NOT NULL DEFAULT 0
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS durable_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT, attempt INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES durable_jobs(id) ON DELETE CASCADE
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS durable_jobs_status ON durable_jobs(status)")
            conn.execute("""CREATE INDEX IF NOT EXISTS durable_events_job
                         ON durable_job_events(job_id, id)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS durable_attempt_windows (
                window INTEGER PRIMARY KEY, attempts INTEGER NOT NULL
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS durable_job_policy (
                id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL
            )""")
            policy = _json({
                "workers": self.workers, "max_active": self.max_active,
                "history_limit": self.history_limit, "max_attempts": self.max_attempts,
                "attempt_budget": self.attempt_budget,
                "budget_window_seconds": self.budget_window_seconds,
                "lease_seconds": self.lease_seconds, "job_timeout": self.job_timeout,
            }, 4096)
            conn.execute("INSERT OR IGNORE INTO durable_job_policy VALUES(1,?)", (policy,))
            stored = conn.execute("SELECT policy FROM durable_job_policy WHERE id=1").fetchone()[0]
            if stored != policy:
                raise ValueError("durable job policy differs from initialized database; "
                                 "restore configured settings or migrate state offline")
            self._validate_schema(conn)
            self._maintain(conn, time.time())
            return policy
        self._policy = await self.db.run(create, write=True)

    async def start(self):
        if not self._started and not self._closed:
            self._started = True
            self._tasks = [asyncio.create_task(self._worker(), name=f"durable-job-{i}")
                           for i in range(self.workers)]

    async def shutdown(self):
        self._closed = True
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def submit_command(self, kind, public_params, command, idempotency_key=None):
        if self._closed:
            raise JobQueueFull("job manager is shutting down")
        from src.agent.commands import validate_command
        command = validate_command(command)
        if kind != command["kind"]:
            raise ValueError("command kind mismatch")
        if idempotency_key is not None:
            if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                    or any(ord(c) < 32 or ord(c) > 126 for c in idempotency_key)):
                raise ValueError("invalid idempotency key")
        allowed = {"url", "source", "text_chars", "filename", "content_type", "size_bytes"}
        if not isinstance(public_params, dict) or set(public_params) - allowed:
            raise ValueError("invalid public job parameters")
        if any(not isinstance(v, str | int | type(None)) or isinstance(v, bool)
               for v in public_params.values()):
            raise ValueError("invalid public job parameters")
        params = _json(public_params, 8192)
        raw = _json(command, MAX_COMMAND_BYTES)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        # Store a hash of client keys, not key material.
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
        job_id = uuid.uuid4().hex

        def submit(conn):
            self._check_policy(conn)
            self._maintain(conn, time.time())
            if key_hash:
                row = conn.execute("SELECT * FROM durable_jobs WHERE idempotency_key=?",
                                   (key_hash,)).fetchone()
                if row:
                    if row["command_hash"] != digest:
                        raise IdempotencyConflict("idempotency key reused for different command")
                    return _public(row)
            active = conn.execute("SELECT count(*) FROM durable_jobs WHERE status IN "
                                  "('queued','running')").fetchone()[0]
            if active >= self.max_active:
                raise JobQueueFull("too many active jobs")
            conn.execute("""INSERT INTO durable_jobs
                (id,kind,status,params,command,command_hash,idempotency_key,created_at,created_epoch)
                VALUES(?,?,'queued',?,?,?,?,?,?)""",
                         (job_id, kind, params, raw, digest, key_hash, _now_iso(), time.time()))
            self._event(conn, job_id)
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return _public(row)
        record = await self.db.run(submit, write=True)
        await self._emit(record)
        self._wake.set()
        return record

    async def get(self, job_id):
        def read(conn):
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return _public(row) if row else None
        return await self.db.run(read)

    async def list(self, limit=20):
        try:
            limit = max(0, min(int(limit), 100))
        except (ValueError, TypeError):
            limit = 20
        return await self.db.run(lambda conn: [_public(row) for row in conn.execute(
            "SELECT * FROM durable_jobs ORDER BY seq DESC LIMIT ?", (limit,))])

    async def wait(self, job_id):
        while True:
            record = await self.get(job_id)
            if record is None:
                raise KeyError(job_id)
            if record["status"] in TERMINAL:
                return record
            await asyncio.sleep(0.05)

    async def events(self, job_id, after=0, limit=100):
        after = max(0, int(after))
        limit = max(0, min(int(limit), 100))
        return await self.db.run(lambda conn: [dict(row) for row in conn.execute(
            "SELECT id,job_id,status,stage,attempt,created_at FROM durable_job_events "
            "WHERE job_id=? AND id>? ORDER BY id LIMIT ?", (job_id, after, limit))])

    async def cancel(self, job_id):
        def request(conn):
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] == "queued":
                self._terminal(conn, job_id, "cancelled")
            elif row["status"] == "running":
                conn.execute("UPDATE durable_jobs SET cancel_requested=1 WHERE id=?", (job_id,))
                self._event(conn, job_id)
            self._prune(conn)
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return _public(row) if row else None
        record = await self.db.run(request, write=True)
        if record:
            await self._emit(record)
        self._wake.set()
        return record

    @staticmethod
    def _validate_schema(conn):
        expected = {
            "durable_jobs": {
                "seq", "id", "kind", "status", "stage", "params", "command",
                "command_hash", "idempotency_key", "result", "error", "created_at",
                "created_epoch", "started_at", "finished_at", "attempts", "lease_token",
                "lease_until", "cancel_requested", "available_epoch",
            },
            "durable_job_events": {"id", "job_id", "status", "stage", "attempt", "created_at"},
            "durable_attempt_windows": {"window", "attempts"},
            "durable_job_policy": {"id", "policy"},
        }
        for table, columns in expected.items():
            if {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")} != columns:
                raise ValueError("unsupported durable job table schema")
        fks = conn.execute("PRAGMA foreign_key_list(durable_job_events)").fetchall()
        if not any(row["table"] == "durable_jobs" and row["from"] == "job_id"
                   and row["to"] == "id" and row["on_delete"] == "CASCADE" for row in fks):
            raise ValueError("unsupported durable job event foreign key")
        unique_columns = set()
        for index in conn.execute("PRAGMA index_list(durable_jobs)"):
            if index["unique"]:
                quoted = index["name"].replace('"', '""')
                names = tuple(row["name"] for row in conn.execute(
                    f'PRAGMA index_info("{quoted}")'))
                unique_columns.add(names)
        if not {("id",), ("idempotency_key",)} <= unique_columns:
            raise ValueError("unsupported durable job uniqueness constraints")

    def _check_policy(self, conn):
        row = conn.execute("SELECT policy FROM durable_job_policy WHERE id=1").fetchone()
        if row is None or row[0] != self._policy:
            raise ValueError("durable job policy changed; restart this manager")

    def _event(self, conn, job_id):
        conn.execute("""INSERT INTO durable_job_events(job_id,status,stage,attempt,created_at)
            SELECT id,status,stage,attempts,? FROM durable_jobs WHERE id=?""", (_now_iso(), job_id))
        conn.execute("""DELETE FROM durable_job_events WHERE job_id=? AND id NOT IN
            (SELECT id FROM durable_job_events WHERE job_id=? ORDER BY id DESC LIMIT ?)""",
                     (job_id, job_id, MAX_EVENTS))

    def _terminal(self, conn, job_id, status, *, result=None, error=None):
        conn.execute("""UPDATE durable_jobs SET status=?, result=?, error=?, finished_at=?,
            command=NULL, lease_token=NULL, lease_until=NULL WHERE id=?""",
                     (status, result, error, _now_iso(), job_id))
        self._event(conn, job_id)

    def _prune(self, conn):
        conn.execute("""DELETE FROM durable_jobs WHERE status IN ('succeeded','failed','cancelled')
            AND id NOT IN (SELECT id FROM durable_jobs WHERE status IN
            ('succeeded','failed','cancelled') ORDER BY finished_at DESC,seq DESC LIMIT ?)""",
                     (self.history_limit,))

    def _maintain(self, conn, now):
        expired = conn.execute("""SELECT id,attempts,cancel_requested,created_epoch
            FROM durable_jobs
            WHERE (status='running' AND lease_until<=?) OR
            (status='queued' AND created_epoch<=?)""", (now, now - COMMAND_TTL)).fetchall()
        for row in expired:
            if row["cancel_requested"]:
                self._terminal(conn, row["id"], "cancelled")
            elif row["attempts"] >= self.max_attempts or row["created_epoch"] <= now - COMMAND_TTL:
                self._terminal(conn, row["id"], "failed", error="job expired")
            else:
                conn.execute("""UPDATE durable_jobs SET status='queued',lease_token=NULL,
                             lease_until=NULL WHERE id=?""", (row["id"],))
                self._event(conn, row["id"])
        self._prune(conn)
        window = int(now // self.budget_window_seconds)
        conn.execute("DELETE FROM durable_attempt_windows WHERE window<?", (window,))

    async def _claim(self):
        def claim(conn):
            self._check_policy(conn)
            now = time.time()
            self._maintain(conn, now)
            running = conn.execute("SELECT count(*) FROM durable_jobs WHERE status='running'")
            if running.fetchone()[0] >= self.workers:
                return None
            window = int(now // self.budget_window_seconds)
            row = conn.execute("SELECT attempts FROM durable_attempt_windows WHERE window=?",
                               (window,)).fetchone()
            if row and row[0] >= self.attempt_budget:
                waiting = conn.execute("SELECT id FROM durable_jobs WHERE status='queued' "
                                       "AND (stage IS NULL OR stage!='waiting_budget')").fetchall()
                for item in waiting:
                    conn.execute("UPDATE durable_jobs SET stage='waiting_budget' WHERE id=?",
                                 (item["id"],))
                    self._event(conn, item["id"])
                return None
            row = conn.execute("SELECT * FROM durable_jobs WHERE status='queued' "
                               "AND available_epoch<=? ORDER BY seq LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            conn.execute("""INSERT INTO durable_attempt_windows VALUES(?,1)
                ON CONFLICT(window) DO UPDATE SET attempts=attempts+1""", (window,))
            conn.execute("""UPDATE durable_jobs SET status='running', attempts=attempts+1,
                lease_token=?,lease_until=?,stage=NULL,started_at=COALESCE(started_at,?)
                WHERE id=?""",
                         (token, now + self.lease_seconds, _now_iso(), row["id"]))
            self._event(conn, row["id"])
            claimed = dict(conn.execute("SELECT * FROM durable_jobs WHERE id=?",
                                        (row["id"],)).fetchone())
            return claimed
        return await self.db.run(claim, write=True)

    async def _heartbeat(self, job_id, token):
        def beat(conn):
            now = time.time()
            row = conn.execute("""SELECT cancel_requested,created_epoch FROM durable_jobs
                WHERE id=? AND status='running' AND lease_token=? AND lease_until>?""",
                               (job_id, token, now)).fetchone()
            if not row:
                return "lost"
            conn.execute("UPDATE durable_jobs SET lease_until=? WHERE id=? AND lease_token=?",
                         (now + self.lease_seconds, job_id, token))
            if row["created_epoch"] <= now - COMMAND_TTL:
                return "expired"
            return "cancel" if row["cancel_requested"] else "alive"
        return await self.db.run(beat, write=True)

    async def _progress(self, job_id, token, stage):
        if stage is not None and (not isinstance(stage, str) or len(stage) > 64):
            raise ValueError("invalid job stage")

        def update(conn):
            cursor = conn.execute("""UPDATE durable_jobs SET stage=? WHERE id=? AND
                status='running' AND lease_token=? AND lease_until>? AND cancel_requested=0""",
                                  (stage, job_id, token, time.time()))
            if not cursor.rowcount:
                raise _LeaseLost()
            self._event(conn, job_id)
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return _public(row)
        record = await self.db.run(update, write=True)
        await self._emit(record)

    async def _finish(self, job_id, token, *, status, result=None, error=None, retry=False):
        raw = _json(result, MAX_RESULT_BYTES) if result is not None else None

        def finish(conn):
            row = conn.execute("""SELECT * FROM durable_jobs WHERE id=? AND status='running'
                AND lease_token=? AND lease_until>?""", (job_id, token, time.time())).fetchone()
            if not row:
                return None
            if row["cancel_requested"]:
                self._terminal(conn, job_id, "cancelled")
            elif retry and row["attempts"] < self.max_attempts:
                delay = min(30.0, 2.0 ** min(row["attempts"] - 1, 5))
                conn.execute("""UPDATE durable_jobs SET status='queued',lease_token=NULL,
                    lease_until=NULL,available_epoch=? WHERE id=?""", (time.time() + delay, job_id))
                self._event(conn, job_id)
            else:
                self._terminal(conn, job_id, status, result=raw, error=error)
            self._prune(conn)
            record = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return _public(record) if record else None
        record = await self.db.run(finish, write=True)
        if record:
            await self._emit(record)
        self._wake.set()
        return record

    async def _relinquish(self, job_id, token):
        # Called only after this owner's handler cleanup is complete. No expiry
        # condition: an expired token may still relinquish until it is replaced.
        def release(conn):
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=? AND status='running' "
                               "AND lease_token=?", (job_id, token)).fetchone()
            if not row:
                return
            if row["cancel_requested"]:
                self._terminal(conn, job_id, "cancelled")
            elif row["attempts"] >= self.max_attempts:
                self._terminal(conn, job_id, "failed", error="server shutdown")
            else:
                conn.execute("""UPDATE durable_jobs SET status='queued',lease_token=NULL,
                    lease_until=NULL WHERE id=?""", (job_id,))
                self._event(conn, job_id)
            self._prune(conn)
        await self.db.run(release, write=True)

    async def _cleanup(self, task, job_id, token):
        """Retain the permit and renew the lease while cancellation unwinds."""
        if task.done():
            await asyncio.gather(task, return_exceptions=True)
            return
        if not task.cancelling():
            task.cancel()
        while not task.done():
            await asyncio.wait({task}, timeout=min(0.2, self.lease_seconds / 3))
            if not task.done():
                try:
                    await self._heartbeat(job_id, token)
                except Exception:
                    # A database outage must never abandon provider cleanup.
                    log.warning("Lease renewal during cleanup failed", exc_info=True)
        await asyncio.gather(task, return_exceptions=True)

    async def _execute(self, row):
        from src.agent.commands import validate_command
        job_id, token = row["id"], row["lease_token"]
        task = None
        started = time.monotonic()
        await self._emit(_public(row))
        try:
            command = validate_command(json.loads(row["command"]))
            task = asyncio.create_task(self.handler(
                command, lambda stage: self._progress(job_id, token, stage), job_id))
            while True:
                done, _ = await asyncio.wait({task}, timeout=min(0.2, self.lease_seconds / 3))
                # Check cancellation/ownership before exposing even a completed result.
                state = await self._heartbeat(job_id, token)
                timed_out = time.monotonic() - started >= self.job_timeout
                if state != "alive" or timed_out:
                    await self._cleanup(task, job_id, token)
                    if state == "lost":
                        return
                    await self._finish(job_id, token,
                                       status="cancelled" if state == "cancel" else "failed",
                                       error=None if state == "cancel" else (
                                           "timed out" if timed_out else "job expired"))
                    return
                if done:
                    result = task.result()
                    if not isinstance(result, dict) or result.get("success") is False:
                        await self._finish(job_id, token, status="failed", error="internal error")
                    else:
                        await self._finish(job_id, token, status="succeeded", result=result)
                    return
        except RetryableJobError:
            if task is not None and not task.done():
                await self._cleanup(task, job_id, token)
            await self._finish(job_id, token, status="failed", error="internal error", retry=True)
        except _LeaseLost:
            return
        except asyncio.CancelledError:
            if task is not None:
                await self._cleanup(task, job_id, token)
            await self._relinquish(job_id, token)
            raise
        except Exception:
            log.exception("Durable job %s failed", job_id)
            if task is not None and not task.done():
                await self._cleanup(task, job_id, token)
            await self._finish(job_id, token, status="failed", error="internal error")
        finally:
            if task is not None and not task.done():
                await self._cleanup(task, job_id, token)

    async def _worker(self):
        while not self._closed:
            try:
                row = await self._claim()
                if row:
                    await self._execute(row)
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.1)
                except TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                # Storage failures do not turn active work into untracked work.
                log.exception("Durable job worker database operation failed")
                await asyncio.sleep(0.1)

    async def _emit(self, record):
        if self._broadcast is not None:
            try:
                await self._broadcast({"type": "job_update", "job": record})
            except Exception:
                log.warning("Durable job broadcast failed", exc_info=True)
