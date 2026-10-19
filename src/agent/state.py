"""Private, local SQLite state shared by durable workflow adapters.

Callbacks must do only short database work. Each operation owns its connection
and transaction; no connection or cursor may escape the callback's thread.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")
APPLICATION_ID = 0x47414633  # GAF3
SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000


class StateDatabase:
    """Single-host state database; construction performs no blocking I/O.

    Keep this on a local disk, inside a private directory. SQLite WAL is not
    supported on network filesystems. Initialization errors are fatal rather
    than triggering a fallback to transient state.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(path)
        if not raw_path or raw_path == ":memory:":
            raise ValueError("durable state requires a local database path")
        self.path = Path(os.path.abspath(os.path.expanduser(raw_path)))
        self._initialized = False

    async def initialize(self) -> None:
        await self._thread(self._initialize)
        self._initialized = True

    async def run(self, callback: Callable[[sqlite3.Connection], _T], *, write: bool = False) -> _T:
        if not self._initialized:
            raise RuntimeError("state database is not initialized")
        return await self._thread(lambda: self._run(callback, write=write))

    @staticmethod
    async def _thread(callback: Callable[[], _T]) -> _T:
        # Cancelling an await does not stop sqlite3's worker thread. Wait for
        # its transaction cleanup before allowing resource shutdown to proceed.
        task = asyncio.create_task(asyncio.to_thread(callback))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    def _check_paths(self, *, create: bool = False) -> None:
        parts = [*reversed(self.path.parent.parents), self.path.parent]
        for directory in parts:
            try:
                info = directory.lstat()
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    directory.mkdir(mode=0o700)
                except FileExistsError:
                    pass  # another initializing process won the mkdir race
                info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("state path directories must not be symlinks")
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise PermissionError("state database requires an owned private directory (0700)")
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ValueError("state files must be owned regular files without links")
            # Older installations may have a permissive umask. Tighten only
            # state files, never arbitrary existing directories.
            try:
                os.chmod(candidate, 0o600, follow_symlinks=False)
            except FileNotFoundError:
                if candidate == self.path:
                    raise
                # Last-connection close can remove a WAL/SHM sidecar while
                # another operation performs its permission check.

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode=rw",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        return connection

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if application_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise ValueError("unsupported durable state schema")

    def _initialize(self) -> None:
        self._check_paths(create=True)
        # O_NOFOLLOW also protects the final component during creation. The
        # containing private directory prevents other users swapping sidecars.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self._check_paths()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if application_id == 0 and version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables:
                    raise ValueError("existing database is not a durable state database")
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._check_schema(connection)
            if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise ValueError("durable state integrity check failed")
            connection.commit()
            # Journal-mode changes can return SQLITE_BUSY immediately, ignoring
            # busy_timeout, while another process initializes the same file.
            deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
            while True:
                try:
                    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as error:
                    if (
                        error.sqlite_errorcode != sqlite3.SQLITE_BUSY
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.01)
            if mode.lower() != "wal":
                raise ValueError("durable state requires WAL journal mode")
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._check_paths()

    def _run(self, callback: Callable[[sqlite3.Connection], _T], *, write: bool) -> _T:
        self._check_paths()
        connection = self._connect()
        try:
            self._check_schema(connection)
            if not write:
                connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = callback(connection)
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
