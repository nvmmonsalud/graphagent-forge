"""On-disk SQLite transactions, isolation and private-path startup checks."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import stat
import threading

import pytest

from src.agent.state import APPLICATION_ID, SCHEMA_VERSION, StateDatabase


async def test_state_reopen_transactions_and_connection_configuration(tmp_path):
    path = tmp_path / "private" / "state.sqlite3"
    db = StateDatabase(path)
    await db.initialize()

    def create(connection):
        assert connection.in_transaction
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
        connection.execute("CREATE TABLE state_test (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO state_test VALUES (1, 'retained')")

    await db.run(create, write=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    reopened = StateDatabase(path)
    await reopened.initialize()
    assert await reopened.run(lambda c: dict(c.execute("SELECT * FROM state_test").fetchone())) == {
        "id": 1,
        "value": "retained",
    }
    assert await reopened.run(lambda c: c.execute("PRAGMA application_id").fetchone()[0]) == (
        APPLICATION_ID
    )
    assert await reopened.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0]) == (
        SCHEMA_VERSION
    )


async def test_failed_callback_rolls_back_and_reads_cannot_write(tmp_path):
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    await db.run(
        lambda c: c.execute("CREATE TABLE values_test (value INTEGER)").rowcount, write=True
    )

    def fail(connection):
        connection.execute("INSERT INTO values_test VALUES (1)")
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await db.run(fail, write=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        await db.run(lambda c: c.execute("INSERT INTO values_test VALUES (2)"))
    assert await db.run(lambda c: c.execute("SELECT COUNT(*) FROM values_test").fetchone()[0]) == 0


async def test_shared_write_transactions_enforce_atomic_read_modify_write(tmp_path):
    db = StateDatabase(tmp_path / "state.sqlite3")
    second = StateDatabase(db.path)
    await db.initialize()
    await second.initialize()
    await db.run(lambda c: c.execute("CREATE TABLE counter (value INTEGER)").rowcount, write=True)
    await db.run(lambda c: c.execute("INSERT INTO counter VALUES (0)").rowcount, write=True)

    def increment(connection):
        value = connection.execute("SELECT value FROM counter").fetchone()[0]
        connection.execute("UPDATE counter SET value = ?", (value + 1,))

    await asyncio.gather(*(store.run(increment, write=True) for store in [db, second] * 10))
    assert await db.run(lambda c: c.execute("SELECT value FROM counter").fetchone()[0]) == 20


async def test_concurrent_initialization_is_idempotent(tmp_path):
    stores = [StateDatabase(tmp_path / "new" / "state.sqlite3") for _ in range(8)]
    await asyncio.gather(*(store.initialize() for store in stores))
    assert all(
        await asyncio.gather(
            *(
                store.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
                for store in stores
            )
        )
    )


async def test_database_work_runs_off_event_loop_and_cancellation_awaits_cleanup(tmp_path):
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    event_loop_thread = threading.get_ident()
    started = threading.Event()
    release = threading.Event()

    def callback(connection):
        assert threading.get_ident() != event_loop_thread
        started.set()
        assert release.wait(5)
        connection.execute("CREATE TABLE committed_after_cancel (value INTEGER)")

    task = asyncio.create_task(db.run(callback, write=True))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (
        await db.run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'committed_after_cancel'"
            ).fetchone()[0]
        )
        == 1
    )


@pytest.mark.parametrize("bad_component", ["file", "parent", "ancestor", "wal", "shm"])
async def test_symlink_paths_are_rejected(tmp_path, bad_component):
    actual = tmp_path / "real"
    actual.mkdir(mode=0o700)
    path = actual / "state.sqlite3"
    if bad_component == "file":
        target = actual / "target"
        target.touch()
        path.symlink_to(target)
    elif bad_component in {"parent", "ancestor"}:
        linked = tmp_path / "linked"
        linked.symlink_to(actual, target_is_directory=True)
        path = linked / ("nested/state.sqlite3" if bad_component == "ancestor" else "state.sqlite3")
    else:
        target = actual / "target"
        target.touch()
        (actual / f"state.sqlite3-{bad_component}").symlink_to(target)
    with pytest.raises(ValueError):
        await StateDatabase(path).initialize()


async def test_hardlinks_and_nonprivate_existing_directory_are_rejected(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.touch()
    path = tmp_path / "state.sqlite3"
    os.link(target, path)
    with pytest.raises(ValueError):
        await StateDatabase(path).initialize()
    path.unlink()
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(PermissionError):
        await StateDatabase(public / "state.sqlite3").initialize()
    assert stat.S_IMODE(public.stat().st_mode) == 0o755


@pytest.mark.parametrize("case", ["corrupt", "foreign", "future", "wrong_app", "zero_version"])
async def test_incompatible_or_corrupt_state_fails_startup(tmp_path, case):
    path = tmp_path / "state.sqlite3"
    if case == "corrupt":
        path.write_bytes(b"this is not a sqlite database")
    else:
        connection = sqlite3.connect(path)
        if case == "foreign":
            connection.execute("CREATE TABLE unrelated (id TEXT)")
        else:
            app_id = 123 if case == "wrong_app" else APPLICATION_ID
            version = 0 if case == "zero_version" else SCHEMA_VERSION + (case == "future")
            connection.execute(f"PRAGMA application_id = {app_id}")
            connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
        connection.close()
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        await StateDatabase(path).initialize()


async def test_requires_initialize_and_local_nonempty_path(tmp_path):
    with pytest.raises(ValueError):
        StateDatabase(":memory:")
    with pytest.raises(ValueError):
        StateDatabase("")
    with pytest.raises(RuntimeError, match="not initialized"):
        await StateDatabase(tmp_path / "state.sqlite3").run(lambda c: None)
