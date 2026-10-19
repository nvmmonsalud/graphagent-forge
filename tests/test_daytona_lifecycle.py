"""Execution owns local children and ephemeral sandboxes through cancellation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.daytona_exec import DaytonaExecutor


@pytest.fixture
def executor(monkeypatch):
    monkeypatch.setattr(DaytonaExecutor, "_local_semaphore", asyncio.Semaphore(1))
    return DaytonaExecutor()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_creation", [False, True])
async def test_cancel_reaps_real_child(executor, monkeypatch, during_creation):
    created = asyncio.Event()
    release_creation = asyncio.Event()
    processes = []
    real_create = asyncio.create_subprocess_exec

    async def create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        processes.append(proc)
        created.set()
        if during_creation:
            await release_creation.wait()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(executor.run_code("import time; time.sleep(60)"))
    try:
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        if during_creation:
            await asyncio.sleep(0)
            assert not task.done()
            task.cancel()  # shutdown may cancel again while acquisition finishes
            release_creation.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
        assert not executor._local_gate().locked()
    finally:
        release_creation.set()
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()


@pytest.mark.asyncio
async def test_timeout_reaps_and_preserves_envelope(executor, monkeypatch):
    real_wait_for = asyncio.wait_for
    real_create = asyncio.create_subprocess_exec
    processes = []

    async def create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        processes.append(proc)
        return proc

    async def short_wait(awaitable, *, timeout):
        assert timeout == 30
        return await real_wait_for(awaitable, timeout=0.05)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(asyncio, "wait_for", short_wait)
    try:
        result = await executor.run_code("import time; time.sleep(60)")
        assert result == {
            "success": False, "error": "Execution timed out (30s)", "method": "local",
        }
        assert processes[0].returncode is not None
        assert not executor._local_gate().locked()
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()


@pytest.mark.asyncio
async def test_gate_held_until_reap_despite_repeated_cancellation(executor, monkeypatch):
    communicating = asyncio.Event()
    reaping = asyncio.Event()
    allow_reap = asyncio.Event()

    async def communicate(**kwargs):
        communicating.set()
        await asyncio.Event().wait()

    async def drain():
        reaping.set()
        await allow_reap.wait()
        proc.returncode = -9
        return b"", b""

    calls = 0

    async def communicate_or_drain(**kwargs):
        nonlocal calls
        calls += 1
        return await (communicate(**kwargs) if calls == 1 else drain())

    proc = SimpleNamespace(returncode=None, communicate=communicate_or_drain,
                           kill=lambda: None, wait=AsyncMock())
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    task = asyncio.create_task(executor.run_code("pass"))
    await communicating.wait()
    task.cancel()
    await reaping.wait()
    waiter = asyncio.create_task(executor._local_gate().acquire())
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    assert not waiter.done()
    allow_reap.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await waiter
    executor._local_gate().release()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_creation_error_releases_capacity(executor, monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("missing"))
    )
    assert await executor.run_code("pass") == {
        "success": False, "error": "missing", "method": "local",
    }
    assert not executor._local_gate().locked()


@pytest.mark.asyncio
async def test_local_stdin_stdout_stderr_and_nonzero(executor):
    result = await executor._local_fallback(
        "import sys; print(sys.stdin.read()); print('bad', file=sys.stderr); sys.exit(3)",
        "python", stdin_data=b"payload",
    )
    assert result == {"success": False, "output": "payload\n", "error": "bad\n", "method": "local"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation", ["run_code", "verify_graph", "analyze_graph", "run_in_sandbox"]
)
@pytest.mark.parametrize("during_creation", [False, True])
async def test_remote_cancel_deletes_acquired_sandbox(executor, operation, during_creation):
    entered = asyncio.Event()
    release = asyncio.Event()
    deleting = asyncio.Event()
    release_delete = asyncio.Event()

    async def acquire(*args):
        if during_creation:
            entered.set()
            await release.wait()
        return sandbox

    async def run(*args):
        entered.set()
        await asyncio.Event().wait()

    async def delete():
        deleting.set()
        await release_delete.wait()

    sandbox = SimpleNamespace(id="test", process=SimpleNamespace(code_run=run),
                              delete=AsyncMock(side_effect=delete))
    executor.client = SimpleNamespace(create=acquire, get=acquire)
    argument = "pass" if operation == "run_code" else {}
    task = asyncio.create_task(
        executor.run_in_sandbox("test", "pass") if operation == "run_in_sandbox"
        else getattr(executor, operation)(argument)
    )
    await entered.wait()
    task.cancel()
    release.set()
    await deleting.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release_delete.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_retry_deletes_each_object_without_get(executor):
    sandboxes = [SimpleNamespace(id="test", process=SimpleNamespace(
        code_run=AsyncMock(side_effect=RuntimeError("private failure"))), delete=AsyncMock())
        for _ in range(2)]
    executor.client = SimpleNamespace(create=AsyncMock(side_effect=sandboxes), get=AsyncMock())
    result = await executor.verify_graph({})
    assert result["ok"] is False
    assert result["error"] == "verification unavailable"
    assert "valid" not in result
    for sandbox in sandboxes:
        sandbox.delete.assert_awaited_once()
    executor.client.get.assert_not_called()


@pytest.mark.asyncio
async def test_failed_delete_does_not_swallow_cancellation(executor):
    deleting = asyncio.Event()
    release = asyncio.Event()

    async def delete():
        deleting.set()
        await release.wait()
        raise RuntimeError("cleanup failed")

    sandbox = SimpleNamespace(id="test", delete=delete, process=SimpleNamespace(
        code_run=AsyncMock(return_value=SimpleNamespace(result="ok"))))
    executor.client = SimpleNamespace(create=AsyncMock(return_value=sandbox))
    task = asyncio.create_task(executor.run_code("pass"))
    await deleting.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_communication_error_still_kills_and_waits(executor, monkeypatch):
    def kill():
        proc.returncode = -9

    proc = SimpleNamespace(returncode=None, kill=kill, wait=AsyncMock(),
                           communicate=AsyncMock(side_effect=[OSError("pipe failed"), (b"", b"")]))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    result = await executor.run_code("pass")
    assert result == {"success": False, "error": "pipe failed", "method": "local"}
    assert proc.returncode == -9
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_reaps_child_with_full_output_pipe(executor, monkeypatch):
    real_create = asyncio.create_subprocess_exec
    ready = asyncio.Event()
    processes = []

    async def create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        processes.append(proc)
        assert await proc.stderr.readline() == b"ready\n"
        # Wait until StreamReader pauses its pipe transport at its buffer limit.
        while not proc.stdout._paused:
            await asyncio.sleep(0.001)
        assert proc.returncode is None
        ready.set()
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    task = asyncio.create_task(executor.run_code(
        r"import os,time; os.write(2,b'ready\n'); "
        "os.write(1,b'x'*1000000); time.sleep(60)"
    ))
    try:
        await asyncio.wait_for(ready.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes[0].returncode is not None
    finally:
        task.cancel()
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()


@pytest.mark.asyncio
@pytest.mark.parametrize("during_get", [False, True])
async def test_persistent_sandbox_not_deleted_when_delete_false(executor, during_get):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def get(*args):
        if during_get:
            entered.set()
            await release.wait()
        return sandbox

    async def run(*args):
        entered.set()
        await asyncio.Event().wait()

    sandbox = SimpleNamespace(id="persistent", process=SimpleNamespace(code_run=run),
                              delete=AsyncMock())
    executor.client = SimpleNamespace(get=get)
    task = asyncio.create_task(executor.run_in_sandbox("persistent", "pass", delete=False))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.delete.assert_not_called()


@pytest.mark.asyncio
async def test_persistent_creation_cancel_cleans_late_result(executor):
    entered = asyncio.Event()
    release = asyncio.Event()
    sandbox = SimpleNamespace(id="persistent", delete=AsyncMock())

    async def create():
        entered.set()
        await release.wait()
        return sandbox

    executor.client = SimpleNamespace(create=create)
    task = asyncio.create_task(executor.create_persistent_sandbox("test"))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_drain_preserves_original_cancellation_and_waits(executor, monkeypatch):
    communicating = asyncio.Event()

    async def communicate(**kwargs):
        communicating.set()
        await asyncio.Event().wait()

    calls = 0

    async def fail_drain(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await communicate(**kwargs)
        raise OSError("drain failed")

    def kill():
        proc.returncode = -9

    proc = SimpleNamespace(returncode=None, kill=kill, wait=AsyncMock(), communicate=fail_drain)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    task = asyncio.create_task(executor.run_code("pass"))
    await communicating.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc.wait.assert_awaited_once()
    assert not executor._local_gate().locked()
