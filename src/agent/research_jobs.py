"""Bounded bulk manifests and explicit URL refresh schedules."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException

from src.agent.commands import command_handler, validate_command
from src.agent.durable_jobs import IdempotencyConflict
from src.agent.jobs import JobQueueFull
from src.ingestion.sources import build_source

log = logging.getLogger(__name__)


async def _call(function, *args, **kwargs):
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _display(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class ResearchJobs:
    def __init__(self, db, jobs, neo4j, agent, identity=None, workspace_id=None):
        self.db, self.jobs, self.neo4j, self.agent = db, jobs, neo4j, agent
        self.identity, self.workspace_id = identity, workspace_id
        self._memory = {"batches": [], "refreshes": []}
        self._lock = asyncio.Lock()
        self._task = None
        self._closed = False
        self._running = {}
        self._cancelled = {}

    async def initialize(self):
        if self.db:
            await self.db.run(lambda c: c.execute("""CREATE TABLE IF NOT EXISTS research_jobs (
                id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"""), write=True)

    async def _state(self, callback, write=False):
        if not self.db:
            return json.loads(json.dumps(callback(self._memory)))

        def run(connection):
            row = connection.execute("SELECT payload FROM research_jobs WHERE id=1").fetchone()
            data = json.loads(row[0]) if row else {"batches": [], "refreshes": []}
            result = callback(data)
            if write:
                connection.execute("INSERT OR REPLACE INTO research_jobs VALUES(1,?)",
                                   (json.dumps(data, separators=(",", ":")),))
            return result

        return await self.db.run(run, write=write)

    async def start(self):
        if self._task is None and not self._closed:
            self._task = asyncio.create_task(self._loop(), name="research-refresh")

    async def shutdown(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while not self._closed:
            try:
                await self.tick()
            except Exception:
                # Provider/graph failures must not kill future scheduling. No
                # exception text is reflected into schedule metadata.
                log.exception("Research refresh scheduler failed")
            await asyncio.sleep(1)

    async def _authorize(self, principal_id):
        if self.identity:
            await self.identity.authorize_principal(principal_id, self.workspace_id, write=True)

    async def _submit(self, command, key=None, principal_id=None):
        await self._authorize(principal_id)
        command = dict(command)
        if self.identity:
            command.update(_principal_id=principal_id, _workspace_id=self.workspace_id)
        params = {}
        if command["kind"] == "url":
            # Public queue metadata has an independent 8KB JSON bound, including
            # ASCII escaping; never let a long display URL fail mid-manifest.
            params["url"] = _display(command["url"])[:1000]
        if hasattr(self.jobs, "submit_command"):
            return await self.jobs.submit_command(command["kind"], params, command,
                                                  idempotency_key=key)
        marker = str(uuid.uuid4())
        handler = command_handler(self.agent)

        async def run(progress):
            if marker in self._cancelled:
                return {"cancelled": True}
            task = asyncio.create_task(handler(command, progress, marker))
            self._running[marker] = task
            try:
                return await task
            except asyncio.CancelledError:
                if marker in self._cancelled:
                    return {"cancelled": True}
                raise
            finally:
                self._running.pop(marker, None)

        record = self.jobs.submit(command["kind"], params, run)
        record["_marker"] = marker
        return record

    @staticmethod
    def validate_bulk(commands):
        if not isinstance(commands, list) or not 1 <= len(commands) <= 10:
            raise ValueError("bulk import requires one to ten commands")
        validated, text_size, file_size = [], 0, 0
        for command in commands:
            if not isinstance(command, dict) or any(key.startswith("_") for key in command):
                raise ValueError("invalid bulk command")
            value = validate_command(command)
            if value["kind"] == "text":
                text_size += len(value["text"])
            elif value["kind"] == "file":
                file_size += len(base64.b64decode(value["data_b64"], validate=True))
            validated.append(value)
        if (text_size > 200000 or file_size > 3 * 1024 * 1024
                or len(json.dumps(commands).encode()) > 4 * 1024 * 1024):
            raise ValueError("bulk import exceeds aggregate limits")
        return validated

    async def bulk(self, commands, idempotency_key=None, principal_id=None):
        commands = self.validate_bulk(commands)
        if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                            or not 1 <= len(idempotency_key) <= 128):
            raise ValueError("invalid idempotency key")
        if idempotency_key is not None and not hasattr(self.jobs, "submit_command"):
            raise ValueError("idempotency requires durable jobs")
        batch = {"id": str(uuid.uuid4()), "created_at": time.time(), "items": []}
        for index, command in enumerate(commands):
            key = ("bulk_" + hashlib.sha256(
                json.dumps([idempotency_key, index], separators=(",", ":")).encode()
            ).hexdigest()) if idempotency_key else None
            try:
                record = await self._submit(command, key, principal_id)
                item = {"index": index, "job_id": record["id"], "status": "accepted"}
                if "_marker" in record:
                    item["_marker"] = record["_marker"]
            except JobQueueFull:
                item = {"index": index, "job_id": None, "status": "rejected",
                        "reason": "queue_full"}
            except IdempotencyConflict:
                item = {"index": index, "job_id": None, "status": "rejected",
                        "reason": "idempotency_conflict"}
            batch["items"].append(item)

        def store(state):
            state["batches"].insert(0, batch)
            del state["batches"][50:]

        async with self._lock:
            await self._state(store, True)
        return self._public_batch(batch)

    @staticmethod
    def _public_batch(batch):
        return {**batch, "items": [{key: value for key, value in item.items()
                                    if not key.startswith("_")} for item in batch["items"]]}

    async def list_batches(self):
        batches = await self._state(lambda state: state["batches"])
        result = []
        for batch in batches:
            public = self._public_batch(batch)
            for item, original in zip(public["items"], batch["items"], strict=True):
                if item["job_id"]:
                    job = await _call(self.jobs.get, item["job_id"])
                    item["status"] = ("cancelled" if original.get("_marker") in self._cancelled
                                      else job["status"] if job else "expired")
            result.append(public)
        return result

    async def _cancel(self, job_id, marker=None):
        if hasattr(self.jobs, "cancel"):
            await _call(self.jobs.cancel, job_id)
        elif marker:
            self._cancelled[marker] = True
            while len(self._cancelled) > 1000:
                self._cancelled.pop(next(iter(self._cancelled)))
            task = self._running.get(marker)
            if task:
                task.cancel()

    async def cancel_batch(self, batch_id):
        batch = await self._state(lambda state: next(
            (row for row in state["batches"] if row["id"] == batch_id), None))
        if batch is None:
            return None
        for item in batch["items"]:
            if item["job_id"]:
                await self._cancel(item["job_id"], item.get("_marker"))
        return {"ok": True}

    async def create_refresh(self, url, interval_seconds, source_id=None, principal_id=None):
        validate_command({"version": 1, "kind": "url", "url": url})
        if type(interval_seconds) is not int or not 3600 <= interval_seconds <= 30 * 86400:
            raise ValueError("refresh interval must be between one hour and thirty days")
        expected = build_source("url", url, "", "")["id"]
        if source_id is not None and source_id != expected:
            raise ValueError("source does not match URL identity")
        await self._authorize(principal_id)
        row = {"id": str(uuid.uuid4()), "url": url, "source_id": expected,
               "interval_seconds": interval_seconds, "principal_id": principal_id,
               "next_due": time.time() + interval_seconds, "pending": None,
               "lease_until": 0, "last_job_id": None, "last_status": "scheduled",
               "last_error": None, "previous_version": None,
               "current_version": None, "changed": None}

        def store(state):
            if len(state["refreshes"]) >= 20:
                raise ValueError("refresh schedule limit reached")
            state["refreshes"].append(row)

        async with self._lock:
            await self._state(store, True)
        return self._public_refresh(row)

    @staticmethod
    def _public_refresh(row):
        return {key: row.get(key) for key in (
            "id", "source_id", "interval_seconds", "next_due", "last_job_id", "last_status",
            "last_error", "previous_version", "current_version", "changed"
        )} | {"display_uri": _display(row["url"])}

    async def list_refreshes(self):
        rows = await self._state(lambda state: state["refreshes"])
        result = []
        for row in rows:
            public = self._public_refresh(row)
            if row["last_job_id"]:
                job = await _call(self.jobs.get, row["last_job_id"])
                public["last_status"] = job["status"] if job else "expired"
                outcome = (job.get("result") or {}) if job else {}
                if job and (job["status"] == "failed" or outcome.get("success") is False):
                    public["last_status"] = "failed"
                    public["last_error"] = "refresh failed"
                if job and job["status"] == "succeeded" and not row.get("snapshot_done"):
                    version = outcome.get("version")
                    public["current_version"] = version
                    public["changed"] = (version != public["previous_version"]
                                         if outcome.get("success") and version else None)

                    def snapshot(state, row=row, version=version, public=public):
                        current = next((item for item in state["refreshes"]
                                        if item["id"] == row["id"]), None)
                        if current and current["last_job_id"] == row["last_job_id"]:
                            current.update(current_version=version, changed=public["changed"],
                                           snapshot_done=True)

                    await self._state(snapshot, True)
            result.append(public)
        return result

    async def run_refresh(self, refresh_id):
        return await self._run_refresh(refresh_id, force=True)

    async def _run_refresh(self, refresh_id, force=False):
        async with self._lock:
            now = time.time()
            owner = str(uuid.uuid4())

            def claim(state):
                row = next((item for item in state["refreshes"] if item["id"] == refresh_id), None)
                if row is None:
                    return None
                if row["lease_until"] > now or (not force and row["next_due"] > now):
                    return False
                if row["pending"] is None:
                    row["pending"] = str(uuid.uuid4())
                row["lease_until"] = now + 60
                row["lease_owner"] = owner
                return dict(row)

            row = await self._state(claim, True)
            if not row:
                return row
            try:
                await self._authorize(row["principal_id"])
            except (HTTPException, PermissionError) as error:
                if isinstance(error, HTTPException) and error.status_code not in (401, 403):
                    raise
                await self._state(lambda state: state["refreshes"].remove(next(
                    item for item in state["refreshes"] if item["id"] == refresh_id)), True)
                if row["last_job_id"]:
                    await self._cancel(row["last_job_id"], row.get("_marker"))
                return None
            if row["last_job_id"]:
                previous = await _call(self.jobs.get, row["last_job_id"])
                if previous and previous["status"] in ("queued", "running"):
                    updated = await self._state(lambda state: self._update(
                        state, refresh_id, pending=None, lease_until=0), True)
                    return self._public_refresh(updated) if updated else None
            source = await self.neo4j.get_source(row["source_id"])
            if not row.get("snapshot_captured"):
                row["previous_version"] = source.get("version") if source else None
                await self._state(lambda state: self._update(
                    state, refresh_id, previous_version=row["previous_version"],
                    snapshot_captured=True, expected_owner=owner), True)
            try:
                record = await self._submit({"version": 1, "kind": "url", "url": row["url"]},
                                            "refresh_" + row["pending"], row["principal_id"])
            except JobQueueFull:
                await self._state(lambda state: self._update(state, refresh_id,
                                 lease_until=0, last_status="queue_full",
                                 last_error="job queue full",
                                 expected_owner=owner), True)
                raise
            # A second process may have deleted the schedule during submission.
            # Cancellation below fences the owned pending work in that case.
            updated = await self._state(lambda state: self._update(
                state, refresh_id, pending=None, lease_until=0,
                next_due=now + row["interval_seconds"], last_job_id=record["id"],
                last_status=record["status"], _marker=record.get("_marker"),
                last_error=None,
                previous_version=row["previous_version"], snapshot_captured=False,
                snapshot_done=False, current_version=None, changed=None,
                expected_owner=owner), True)
            if updated is None:
                await self._cancel(record["id"], record.get("_marker"))
                return None
            if updated is False:
                return False
            return self._public_refresh(updated)

    @staticmethod
    def _update(state, refresh_id, expected_owner=None, **values):
        row = next((item for item in state["refreshes"] if item["id"] == refresh_id), None)
        if row is not None:
            if expected_owner is not None and row.get("lease_owner") != expected_owner:
                return False
            row.update(values)
            return dict(row)
        return None

    async def delete_refresh(self, refresh_id):
        async with self._lock:
            def delete(state):
                row = next((item for item in state["refreshes"] if item["id"] == refresh_id), None)
                if row:
                    state["refreshes"].remove(row)
                return row
            row = await self._state(delete, True)
            if row and row["last_job_id"]:
                await self._cancel(row["last_job_id"], row.get("_marker"))
            return {"ok": True} if row else None

    async def tick(self):
        rows = await self._state(lambda state: [row["id"] for row in state["refreshes"]
                                                if row["next_due"] <= time.time()])
        for refresh_id in rows:
            try:
                await self._run_refresh(refresh_id)
            except JobQueueFull:
                pass
            except Exception:
                log.exception("Scheduled refresh unavailable")
                await self._state(lambda state, refresh_id=refresh_id: self._update(
                    state, refresh_id, last_error="refresh unavailable"), True)
