"""Reloadable operator identities and bounded opaque browser sessions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextvars import ContextVar
from pathlib import Path

from fastapi import HTTPException

private_authorized: ContextVar[bool] = ContextVar("private_authorized", default=False)
_ID = re.compile(r"[a-z0-9_-]{1,48}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
SESSION_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 1000


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class IdentityStore:
    def __init__(self, db, config_path, allowed_origins, secure=True):
        self.db = db
        self.config_path = Path(config_path)
        self.allowed_origins = frozenset(allowed_origins)
        self.secure = secure

    def _read_config(self):
        path = self.config_path
        # Reject symlinks in all components, including the containing directory.
        absolute = Path(os.path.abspath(path))
        for parent in [*reversed(absolute.parent.parents), absolute.parent]:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("invalid identity configuration directory")
        parent_info = absolute.parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("identity configuration requires a private directory")
        descriptor = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 65536):
                raise ValueError("invalid identity configuration permissions")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise ValueError("identity configuration too large")
            config = json.loads(raw)
        finally:
            os.close(descriptor)
        if not isinstance(config, dict) or set(config) != {"version", "workspaces", "principals"}:
            raise ValueError("invalid identity configuration")
        if type(config["version"]) is not int or config["version"] != 1:
            raise ValueError("unsupported identity configuration")
        workspaces, principals = config["workspaces"], config["principals"]
        if (not isinstance(workspaces, list) or not 1 <= len(workspaces) <= 8
                or not isinstance(principals, list) or not 1 <= len(principals) <= 100):
            raise ValueError("invalid identity configuration limits")
        ws = {}
        for item in workspaces:
            if (not isinstance(item, dict) or set(item) != {"id", "title"}
                    or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                    or item["id"] in ws or not isinstance(item["title"], str)
                    or not 1 <= len(item["title"]) <= 200):
                raise ValueError("invalid workspace configuration")
            ws[item["id"]] = item
        people, hashes = {}, set()
        for item in principals:
            if (not isinstance(item, dict)
                    or set(item) != {"id", "token_sha256", "memberships"}
                    or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                    or item["id"] in people or not isinstance(item["token_sha256"], str)
                    or not _HASH.fullmatch(item["token_sha256"])
                    or item["token_sha256"] in hashes
                    or not isinstance(item["memberships"], dict)):
                raise ValueError("invalid principal configuration")
            for key, role in item["memberships"].items():
                if key not in ws or role not in ("reader", "editor"):
                    raise ValueError("invalid membership configuration")
            people[item["id"]] = item
            hashes.add(item["token_sha256"])
        return ws, people

    async def configuration(self):
        return await asyncio.to_thread(self._read_config)

    async def _configuration(self):
        try:
            return await self.configuration()
        except (OSError, ValueError, TypeError, UnicodeError):
            raise HTTPException(503, "identity configuration unavailable") from None

    async def initialize(self):
        await self.configuration()

        def initialize(connection):
            connection.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
                token_hash TEXT NOT NULL, expires REAL NOT NULL, created REAL NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS auth_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                principal_id TEXT, workspace_id TEXT, action TEXT NOT NULL,
                result TEXT NOT NULL)""")

        await self.db.run(initialize, write=True)

    async def _token_auth(self, token, config):
        if not isinstance(token, str) or not 32 <= len(token) <= 512:
            raise HTTPException(401, "authentication required")
        digest = _digest(token)
        for principal in config[1].values():
            if secrets.compare_digest(principal["token_sha256"], digest):
                return {"principal_id": principal["id"], "session_id": None,
                        "token_hash": digest}
        raise HTTPException(401, "authentication required")

    async def authenticate(self, headers, cookies):
        config = await self._configuration()
        authorization = headers.get("authorization")
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer":
                raise HTTPException(401, "authentication required")
            return await self._token_auth(token, config)
        cookie = cookies.get("ga_session")
        if not isinstance(cookie, str) or not 32 <= len(cookie) <= 256:
            raise HTTPException(401, "authentication required")
        session_id = _digest(cookie)
        row = await self.db.run(lambda c: c.execute(
            "SELECT * FROM auth_sessions WHERE id=? AND expires>?",
            (session_id, time.time()),
        ).fetchone())
        principal = config[1].get(row["principal_id"]) if row else None
        if not principal or not secrets.compare_digest(
            principal["token_sha256"], row["token_hash"]
        ):
            raise HTTPException(401, "authentication required")
        return {"principal_id": principal["id"], "session_id": session_id}

    async def authorize(self, auth, workspace_id, write=False):
        ws, people = await self._configuration()
        # Revalidate session expiry/revocation during WS deliveries and job stages.
        if auth.get("session_id"):
            row = await self.db.run(lambda c: c.execute(
                "SELECT token_hash FROM auth_sessions WHERE id=? AND principal_id=? AND expires>?",
                (auth["session_id"], auth["principal_id"], time.time()),
            ).fetchone())
            person = people.get(auth.get("principal_id"))
            if not row or not person or row[0] != person["token_sha256"]:
                raise HTTPException(401, "authentication required")
        person = people.get(auth.get("principal_id"))
        if not auth.get("session_id") and not auth.get("_durable_principal"):
            if not person or not secrets.compare_digest(
                person["token_sha256"], auth.get("token_hash", "")
            ):
                raise HTTPException(401, "authentication required")
        role = person["memberships"].get(workspace_id) if person else None
        if not role or (write and role != "editor"):
            raise HTTPException(403, "workspace access denied")
        return {**ws[workspace_id], "role": role}

    async def workspaces(self, auth):
        _, people = await self._configuration()
        person = people.get(auth.get("principal_id"))
        if not person:
            raise HTTPException(401, "authentication required")
        return [await self.authorize(auth, key) for key in person["memberships"]]

    async def authorize_principal(self, principal_id, workspace_id, write=True):
        """Recheck a durable submitter against the current operator configuration."""
        return await self.authorize(
            {"principal_id": principal_id, "session_id": None, "_durable_principal": True},
            workspace_id, write=write
        )

    def check_csrf(self, headers, auth, force=False):
        if force or auth.get("session_id"):
            if headers.get("origin") not in self.allowed_origins:
                raise HTTPException(403, "origin denied")

    async def create_session(self, token):
        config = await self._configuration()
        auth = await self._token_auth(token, config)
        cookie = secrets.token_urlsafe(32)
        session_id = _digest(cookie)
        now = time.time()

        def create(connection):
            connection.execute("DELETE FROM auth_sessions WHERE expires<=?", (now,))
            connection.execute(
                "INSERT INTO auth_sessions VALUES (?,?,?,?,?)",
                (session_id, auth["principal_id"], _digest(token), now + SESSION_SECONDS, now),
            )
            connection.execute("""DELETE FROM auth_sessions WHERE id IN (
                SELECT id FROM auth_sessions ORDER BY created DESC, id DESC LIMIT -1 OFFSET ?)
                """, (MAX_SESSIONS,))

        await self.db.run(create, write=True)
        auth["session_id"] = session_id
        auth.pop("token_hash", None)
        return cookie, auth

    async def revoke(self, auth):
        if auth.get("session_id"):
            await self.db.run(lambda c: c.execute(
                "DELETE FROM auth_sessions WHERE id=?", (auth["session_id"],)
            ), write=True)

    async def audit(self, action, result, principal_id=None, workspace_id=None):
        # Call sites supply fixed action/result labels. Never accept research text.
        if (not re.fullmatch(r"[a-z_.-]{1,48}", action)
                or not re.fullmatch(r"[a-z_.-]{1,32}", result)
                or any(value is not None and not _ID.fullmatch(value)
                       for value in (principal_id, workspace_id))):
            raise ValueError("invalid audit metadata")

        def record(connection):
            connection.execute("""INSERT INTO auth_audit
                (timestamp,principal_id,workspace_id,action,result) VALUES (?,?,?,?,?)""",
                (time.time(), principal_id, workspace_id, action, result))
            connection.execute("""DELETE FROM auth_audit WHERE id IN (
                SELECT id FROM auth_audit ORDER BY id DESC LIMIT -1 OFFSET 1000)""")

        await self.db.run(record, write=True)
