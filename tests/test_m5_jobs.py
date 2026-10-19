"""Bounded manifests and durable refresh claims without provider calls."""
import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest

from src.agent.durable_jobs import DurableJobManager
from src.agent.jobs import JobManager, JobQueueFull
from src.agent.research_jobs import ResearchJobs
from src.agent.state import StateDatabase


def command(text="content"):
    return {"version": 1, "kind": "text", "text": text, "source": "manifest"}


@pytest.fixture
async def service(tmp_path):
    os.chmod(tmp_path, 0o700)
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    jobs = DurableJobManager(db, AsyncMock(), max_active=2)
    await jobs.initialize()
    graph = AsyncMock()
    graph.get_source.return_value = {"version": "v1"}
    result = ResearchJobs(db, jobs, graph, AsyncMock())
    await result.initialize()
    return result


async def test_manifest_validated_entirely_before_submit(service):
    with pytest.raises(ValueError):
        await service.bulk([command(), {"version": 1, "kind": "evil"}])
    assert await service.jobs.list() == []
    with pytest.raises(ValueError):
        await service.bulk([command("x" * 150000), command("y" * 150000)])
    with pytest.raises(ValueError):
        await service.bulk([{**command(), "_principal_id": "alice", "_workspace_id": "one"}])


async def test_partial_queue_outcomes_and_batch_cancel(service):
    result = await service.bulk([command("a"), command("b"), command("c")])
    assert [item["status"] for item in result["items"]] == ["accepted", "accepted", "rejected"]
    assert result["items"][2]["reason"] == "queue_full"
    assert "content" not in json.dumps(result)
    assert await service.cancel_batch(result["id"]) == {"ok": True}
    assert [item["status"] for item in (await service.list_batches())[0]["items"]][:2] == [
        "cancelled", "cancelled"]


async def test_refresh_redaction_source_identity_reopen_and_coalesce(service):
    url = "https://example.com/article?secret=signed-token"
    with pytest.raises(ValueError):
        await service.create_refresh(url, 3600, source_id="wrong")
    for interval in (1, True, 30 * 86400 + 1):
        with pytest.raises(ValueError):
            await service.create_refresh(url, interval)
    row = await service.create_refresh(url, 3600)
    assert "secret" not in json.dumps(row)
    await service._state(lambda state: state["refreshes"][0].update(next_due=1), True)
    reopened = ResearchJobs(service.db, service.jobs, service.neo4j, service.agent)
    await reopened.initialize()
    await asyncio.gather(service.tick(), reopened.tick())
    jobs = await service.jobs.list()
    assert len(jobs) == 1
    assert jobs[0]["params"]["url"] == "https://example.com/article"
    assert "signed-token" not in json.dumps(jobs)
    listed = await reopened.list_refreshes()
    assert listed[0]["last_job_id"] == jobs[0]["id"]
    assert listed[0]["next_due"] > 3600
    await reopened.run_refresh(row["id"])
    assert len(await service.jobs.list()) == 1  # no overlapping manual refresh
    await reopened.delete_refresh(row["id"])
    assert (await service.jobs.get(jobs[0]["id"]))["status"] == "cancelled"
    stored = await service.db.run(
        lambda c: c.execute("SELECT payload FROM research_jobs").fetchone()[0])
    assert "signed-token" not in stored


async def test_refresh_actor_revocation_prevents_submission(service):
    identity = AsyncMock()
    service.identity, service.workspace_id = identity, "one"
    row = await service.create_refresh("https://example.com", 3600, principal_id="alice")
    identity.authorize_principal.side_effect = PermissionError("revoked")
    assert await service.run_refresh(row["id"]) is None
    assert await service.jobs.list() == []
    assert await service.list_refreshes() == []


async def test_refresh_commands_have_server_actor_fields(service):
    service.identity, service.workspace_id = AsyncMock(), "one"
    row = await service.create_refresh("https://example.com", 3600, principal_id="alice")
    await service.run_refresh(row["id"])
    stored = await service.db.run(
        lambda c: c.execute("SELECT command FROM durable_jobs").fetchone()[0])
    assert json.loads(stored)["_principal_id"] == "alice"
    assert json.loads(stored)["_workspace_id"] == "one"


async def test_memory_cancel_does_not_kill_worker():
    agent, graph = AsyncMock(), AsyncMock()
    graph.get_ingest_operation.return_value = None
    agent.neo4j = graph
    agent.ingest_text.return_value = {"success": True}
    jobs = JobManager(workers=1)
    service = ResearchJobs(None, jobs, graph, agent)
    await service.initialize()
    first = await service.bulk([command("cancelled")])
    await service.cancel_batch(first["id"])
    jobs.start()
    second = await service.bulk([command("keep")])
    for _ in range(50):
        if jobs.get(second["items"][0]["job_id"])["status"] == "succeeded":
            break
        await asyncio.sleep(0.01)
    assert jobs.get(second["items"][0]["job_id"])["status"] == "succeeded"
    agent.ingest_text.assert_awaited_once()
    assert agent.ingest_text.await_args.args == ("keep",)
    await jobs.shutdown()


async def test_schedule_queue_full_retains_retry_claim(service):
    await service.bulk([command("a"), command("b")])
    row = await service.create_refresh("https://example.com", 3600)
    with pytest.raises(JobQueueFull):
        await service.run_refresh(row["id"])
    listed = await service.list_refreshes()
    assert listed[0]["last_status"] == "queue_full"
    assert listed[0]["last_job_id"] is None


async def test_stale_schedule_claim_cannot_overwrite_new_owner(service):
    row = await service.create_refresh("https://example.com", 3600)
    entered, release = asyncio.Event(), asyncio.Event()
    submit = service._submit

    async def stalled(*args, **kwargs):
        entered.set()
        await release.wait()
        return await submit(*args, **kwargs)

    service._submit = stalled
    first = asyncio.create_task(service.run_refresh(row["id"]))
    await entered.wait()
    await service._state(lambda state: state["refreshes"][0].update(lease_until=1), True)
    second = ResearchJobs(service.db, service.jobs, service.neo4j, service.agent)
    winner = await second.run_refresh(row["id"])
    release.set()
    assert await first is False
    assert len(await service.jobs.list()) == 1
    assert (await second.list_refreshes())[0]["last_job_id"] == winner["last_job_id"]


async def test_delete_during_submit_cancels_newly_owned_job(service):
    row = await service.create_refresh("https://example.com", 3600)
    entered, release = asyncio.Event(), asyncio.Event()
    submit = service._submit

    async def stalled(*args, **kwargs):
        entered.set()
        await release.wait()
        return await submit(*args, **kwargs)

    service._submit = stalled
    pending = asyncio.create_task(service.run_refresh(row["id"]))
    await entered.wait()
    other = ResearchJobs(service.db, service.jobs, service.neo4j, service.agent)
    assert await other.delete_refresh(row["id"]) == {"ok": True}
    release.set()
    assert await pending is None
    assert (await service.jobs.list())[0]["status"] == "cancelled"
    assert await service.list_refreshes() == []


async def test_memory_rejects_idempotency_before_submission():
    jobs = JobManager()
    service = ResearchJobs(None, jobs, AsyncMock(), AsyncMock())
    with pytest.raises(ValueError):
        await service.bulk([command()], "key")
    assert jobs.list() == []


async def test_refresh_version_snapshot_is_stable_metadata(service):
    row = await service.create_refresh("https://example.com", 3600)
    queued = await service.run_refresh(row["id"])
    await service.db.run(lambda c: c.execute(
        "UPDATE durable_jobs SET status='succeeded',result=? WHERE id=?",
        (json.dumps({"success": True, "version": "v2"}), queued["last_job_id"])), write=True)
    first = (await service.list_refreshes())[0]
    assert (first["previous_version"], first["current_version"], first["changed"]) == (
        "v1", "v2", True)
    service.neo4j.get_source.return_value = {"version": "v3"}
    assert (await service.list_refreshes())[0]["current_version"] == "v2"
