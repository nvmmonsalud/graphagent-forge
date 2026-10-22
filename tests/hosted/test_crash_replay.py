"""Actual SIGKILL after graph commit, followed by deterministic lease recovery.

The intervening refresh/delete uses scoped direct database fixture setup while
the application is dead. This proves receipt replay over real process/HTTP/state
boundaries, not a natural lease-expiry latency or real-provider billing guarantee.
"""
import asyncio
import hashlib
import sqlite3
import time

import pytest

from hosted.test_connected import DOCUMENT
from src.graph.workspace_scope import WorkspaceNeo4jClient
from src.ingestion.chunks import plan_chunks

pytestmark = pytest.mark.hosted


async def graph_state(hosted, job_id, intervention=None):
    # Ownership is checked against the locally launched process and data path
    # before connect(), since even get_source() can create a placeholder.
    with hosted.database.driver() as owner:
        hosted.database.verify_owner(owner)
    graph = WorkspaceNeo4jClient(
        hosted.database.uri, "neo4j", "disposable-fixture", "alpha",
    )
    await graph.connect()
    try:
        receipt = await graph.get_ingest_operation(job_id)
        assert receipt is not None
        source_id = receipt["source"]
        if intervention == "refresh":
            old_source = await graph.get_source(source_id)
            assert old_source["version"] == receipt["version"]
            text = "Cedar was rebuilt in 2043. This is newer invented evidence."
            source = {**old_source, "version": "h1-newer-version",
                      "content_hash": hashlib.sha256(text.encode()).hexdigest()}
            chunks = plan_chunks(text, source_id, source["version"])
            result = await graph.publish_source_graph({
                "nodes": [{"id": "h1-current-node", "label": "Current Cedar",
                           "evidence_ids": [chunks[0]["id"]]}],
                "edges": [], "chunks": chunks,
            }, source=source, expected_version=receipt["version"])
            assert result["nodes_written"] == 1
            assert result["conflict"] is False and not result["unchanged"]
        elif intervention == "delete":
            await graph.delete_source(source_id)
        return {
            "receipt": receipt,
            "source": await graph.get_source(source_id),
            "evidence": await graph.get_source_evidence(source_id),
            "graph": await graph.get_all_graph_data(),
        }
    finally:
        await graph.close()


@pytest.mark.parametrize("intervention", ["refresh", "delete"])
def test_forced_process_crash_replays_receipt_without_stale_publication(hosted, intervention):
    hosted.restart(publication_barrier=True)
    startup_denials = hosted.network_counts()["denied"]
    with hosted.client() as client:
        response = client.post("/api/ingest/text", json={"text": DOCUMENT,
                                                       "source": "crash-note"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 15
        while not hosted.barrier.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert hosted.barrier.read_text() == "published"
        assert client.get("/api/jobs/" + job_id).json()["status"] == "running"
    hosted.crash()
    database_path = hosted.state / "workspaces/alpha/state.sqlite3"
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT status,command,attempts FROM durable_jobs WHERE id=?",
                                 (job_id,)).fetchone()
        assert row[0] == "running" and row[1] is not None and row[2] == 1
        # The process really died. Expiring its persisted lease directly makes
        # this deterministic; it does not claim a measured recovery objective.
        assert connection.execute("UPDATE durable_jobs SET lease_until=0 WHERE id=?",
                                  (job_id,)).rowcount == 1
    before = asyncio.run(graph_state(hosted, job_id, intervention))
    if intervention == "refresh":
        assert before["source"]["version"] != before["receipt"]["version"]
        assert {node["label"] for node in before["graph"]["nodes"]} == {"Current Cedar"}
        assert before["evidence"]
    else:
        assert before["graph"]["nodes"] == []
        assert before["evidence"] == []
    assert hosted.provider.calls == {"extraction": 1, "answer": 0}
    hosted.provider.mode = "fail"  # Any attempted replay extraction must be visible.
    hosted.start()
    with hosted.client() as client:
        terminal = hosted.wait_job(client, job_id)
        assert terminal["status"] == "succeeded"
        assert terminal["result"] == before["receipt"]
        assert terminal["result"]["unchanged"] is True
    after = asyncio.run(graph_state(hosted, job_id))
    assert after == before
    assert hosted.provider.calls == {"extraction": 1, "answer": 0}
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT command,attempts FROM durable_jobs WHERE id=?",
                                  (job_id,)).fetchone() == (None, 2)
    # Dependency imports can make incidental denied lookups at startup. Replay
    # must not add denied traffic beyond that identical bootstrap baseline.
    assert hosted.network_counts()["denied"] == startup_denials
