"""Operation receipt recovery against an explicitly disposable real Neo4j."""
import asyncio
import os

import pytest

from src.graph.neo4j_client import Neo4jClient
from src.ingestion.chunks import plan_chunks

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('M3_DISPOSABLE_NEO4J') != '1', reason='Requires M3_DISPOSABLE_NEO4J=1')]


@pytest.fixture
async def db():
    client = Neo4jClient(os.environ['NEO4J_URI'], os.environ['NEO4J_USER'],
                         os.environ['NEO4J_PASSWORD'])
    await client.connect()
    await client.init_schema()
    await client.clear_graph()
    async with client.driver.session() as session:
        await session.run('MATCH (o:IngestOperation) DELETE o')
        await session.run('MATCH (s:Source) DELETE s')
    try:
        yield client
    finally:
        await client.clear_graph()
        async with client.driver.session() as session:
            await session.run('MATCH (o:IngestOperation) DELETE o')
        await client.close()


async def publish(db, version='v1', expected=None, operation='job'):
    chunks = plan_chunks('Original private evidence ' + version, 's', version)
    data = {'nodes': [{'id': version, 'label': version,
                       'evidence_ids': [chunks[0]['id']]}], 'edges': [], 'chunks': chunks}
    return await db.publish_source_graph(data, source={'id': 's', 'version': version},
                                          expected_version=expected, operation_id=operation)


@pytest.mark.parametrize('intervention', ['refresh', 'delete', 'clear'])
async def test_committed_publication_receipt_prevents_rollback_or_resurrection(db, intervention):
    # Simulate worker dying immediately after this graph commit, before durable result save.
    await publish(db)
    if intervention == 'refresh':
        await publish(db, version='v2', expected='v1', operation='new-job')
    elif intervention == 'delete':
        await db.delete_source('s')
    else:
        await db.clear_graph()
    before = await db.get_source('s')
    evidence_before = await db.get_source_evidence('s')
    receipt = await db.get_ingest_operation('job')
    assert receipt == dict(success=True, source='s', version='v1', nodes=1, edges=0,
                           unchanged=True)
    replay = await publish(db)
    assert replay['unchanged'] and replay['version'] == 'v1'
    assert replay['nodes_written'] == 1
    assert await db.get_source('s') == before
    assert await db.get_source_evidence('s') == evidence_before
    graph = await db.get_all_graph_data()
    assert all(node.get('type') != 'IngestOperation' for node in graph['nodes'])


async def test_unchanged_receipt_survives_clear_and_old_receipt_expires(db):
    await publish(db, operation=None)
    result = await publish(db, expected='v1', operation='unchanged-job')
    assert result['unchanged']
    await db.clear_graph()
    assert (await db.get_ingest_operation('unchanged-job'))['version'] == 'v1'
    async with db.driver.session() as session:
        await session.run("MATCH (o:IngestOperation {id: 'unchanged-job'}) "
                          "SET o.created_at = datetime() - duration('P8D')")
    assert await db.get_ingest_operation('unchanged-job') is None
    async with db.driver.session() as session:
        result = await session.run('MATCH (o:IngestOperation) RETURN count(o) AS n')
        record = await result.single()
    assert record['n'] == 0


async def test_racing_same_operation_records_one_publication(db):
    results = await asyncio.gather(publish(db), publish(db))
    assert sorted(r['unchanged'] for r in results) == [False, True]
    async with db.driver.session() as session:
        row = await (await session.run('MATCH (o:IngestOperation) RETURN count(o) AS n')).single()
    assert row['n'] == 1
    assert len(await db.get_source_evidence('s')) == 1


async def test_conflict_has_no_receipt(db):
    await publish(db)
    result = await publish(db, version='v2', expected='wrong', operation='conflict')
    assert result['conflict']
    assert await db.get_ingest_operation('conflict') is None
    assert (await db.get_source('s'))['version'] == 'v1'


async def test_receipt_constraint_failure_rolls_back_replacement_and_evidence(db):
    from neo4j.exceptions import ConstraintError

    await publish(db)
    before = await db.get_source_evidence('s')
    async with db.driver.session() as session:
        await session.run('CREATE CONSTRAINT m3_test_receipt_source IF NOT EXISTS '
                          'FOR (o:IngestOperation) REQUIRE o.source IS UNIQUE')
    try:
        with pytest.raises(ConstraintError):
            await publish(db, version='v2', expected='v1', operation='second-job')
        assert (await db.get_source('s'))['version'] == 'v1'
        assert await db.get_source_evidence('s') == before
        assert await db.get_ingest_operation('second-job') is None
    finally:
        async with db.driver.session() as session:
            await session.run('DROP CONSTRAINT m3_test_receipt_source IF EXISTS')


async def test_receipt_retained_beyond_command_expiry(db):
    await publish(db)
    async with db.driver.session() as session:
        await session.run("MATCH (o:IngestOperation {id: 'job'}) "
                          "SET o.created_at = datetime() - duration('P2D')")
    assert (await db.get_ingest_operation('job'))['version'] == 'v1'


async def test_reopened_sqlite_job_recovers_committed_graph_after_refresh(
    db, tmp_path, monkeypatch,
):
    """Lose the SQLite finalize after real graph commit, then replay a durable lease."""
    import json
    from unittest.mock import AsyncMock

    import src.ingestion.graph_writer as writer
    from src.agent.commands import command_handler
    from src.agent.core import GraphAgent
    from src.agent.durable_jobs import DurableJobManager
    from src.agent.state import StateDatabase
    from src.ingestion.sources import build_source

    agent = GraphAgent(db)
    extract = AsyncMock(return_value={'nodes': [{'id': 'original', 'label': 'Original'}],
                                      'edges': []})
    monkeypatch.setattr(writer, 'extract_entities', extract)

    async def crash_after_graph_commit(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(agent, '_post_ingest', crash_after_graph_commit)
    path = tmp_path / 'private-state' / 'jobs.sqlite3'
    first_state = StateDatabase(path)
    first = DurableJobManager(first_state, command_handler(agent), workers=1)
    await first.initialize()
    command = {'version': 1, 'kind': 'text', 'text': 'original evidence', 'source': 'manual'}
    job = await first.submit_command('text', {'source': 'manual', 'text_chars': 17}, command)
    claimed = await first._claim()
    assert claimed['id'] == job['id']
    with pytest.raises(asyncio.CancelledError):
        await first.handler(json.loads(claimed['command']), None, claimed['id'])
    receipt = await db.get_ingest_operation(job['id'])
    assert receipt is not None and extract.await_count == 1
    assert (await first.get(job['id']))['status'] == 'running'

    refreshed = build_source('text', 'manual', 'manual', 'newer source evidence')
    assert refreshed['id'] == receipt['source']
    chunks = plan_chunks('newer source evidence', refreshed['id'], refreshed['version'])
    await db.publish_source_graph({'nodes': [{'id': 'fresh', 'label': 'Fresh'}],
                                   'edges': [], 'chunks': chunks}, source=refreshed,
                                  expected_version=receipt['version'])
    graph_before = await db.get_all_graph_data()
    evidence_before = await db.get_source_evidence(receipt['source'])
    source_before = await db.get_source(receipt['source'])
    # No sleep or graceful relinquish: persisted lease expiry models a dead owner.
    await first_state.run(lambda conn: conn.execute(
        'UPDATE durable_jobs SET lease_until=0 WHERE id=?', (job['id'],)).rowcount, write=True)
    extract.side_effect = AssertionError('recovery must not call the paid extractor')
    replay = AsyncMock(wraps=command_handler(agent))
    recovered_state = StateDatabase(path)
    second = DurableJobManager(recovered_state, replay, workers=1)
    await second.initialize()
    await second.start()
    try:
        terminal = await asyncio.wait_for(second.wait(job['id']), timeout=5)
        assert terminal['status'] == 'succeeded'
        assert terminal['result'] == receipt
        replay.assert_awaited_once()
        assert replay.await_args.args[2] == job['id']
        assert extract.await_count == 1
        assert await db.get_source(receipt['source']) == source_before
        assert await db.get_source_evidence(receipt['source']) == evidence_before
        assert await db.get_all_graph_data() == graph_before
        # A surviving stale callback cannot overwrite the recovered durable result.
        assert await first._finish(job['id'], claimed['lease_token'],
                                   status='failed', error='stale') is None
        private = await recovered_state.run(lambda conn: dict(conn.execute(
            'SELECT command,attempts FROM durable_jobs WHERE id=?', (job['id'],)).fetchone()))
        assert private == {'command': None, 'attempts': 2}
    finally:
        await second.shutdown()
        await first.shutdown()
