"""Real durable adapters exercised through HTTP; no provider services."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.durable_history import SQLiteQueryHistory
from src.agent.durable_jobs import DurableJobManager
from src.agent.state import StateDatabase
from src.api import routes


@pytest.fixture
async def workflow(tmp_path):
    db = StateDatabase(tmp_path / 'private' / 'state.sqlite3')
    await db.initialize()
    jobs = DurableJobManager(db, AsyncMock())
    await jobs.initialize()
    history = SQLiteQueryHistory(db)
    await history.initialize()
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    app.state.storage_mode = 'sqlite'
    app.state.jobs = jobs
    app.state.history = history
    app.state.agent = AsyncMock()
    app.state.neo4j_available = True
    routes._rate_buckets.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as client:
        yield client, jobs, history, app
    await jobs.shutdown()
    routes._rate_buckets.clear()


@pytest.mark.asyncio
async def test_durable_http_idempotency_private_commands_and_cancel(workflow):
    client, jobs, _, _ = workflow
    payload = {'text': 'PRIVATE document marker', 'source': 'named source'}
    headers = {'Idempotency-Key': 'client-operation'}
    first = await client.post('/api/ingest/text', json=payload, headers=headers)
    assert first.status_code == 202
    assert first.json()['cancel_supported'] is True
    again = await client.post('/api/ingest/text', json=payload, headers=headers)
    assert first.json() == again.json()
    conflict = await client.post('/api/ingest/text',
                                 json={**payload, 'text': 'different private text'},
                                 headers=headers)
    assert conflict.status_code == 409
    job_id = first.json()['job_id']
    for url in ('/api/jobs', '/api/jobs/' + job_id, '/api/jobs/' + job_id + '/events'):
        response = await client.get(url)
        assert response.status_code == 200
        assert 'PRIVATE document marker' not in response.text
        assert 'client-operation' not in response.text
    cancelled = await client.post('/api/jobs/' + job_id + '/cancel')
    assert cancelled.json()['status'] == 'cancelled'
    assert (await jobs.get(job_id))['status'] == 'cancelled'
    events = (await client.get('/api/jobs/' + job_id + '/events')).json()['events']
    assert [event['status'] for event in events] == ['queued', 'cancelled']
    assert (await client.get('/api/jobs/' + job_id + '/events?limit=101')).status_code == 422


@pytest.mark.asyncio
async def test_persisted_reads_and_actions_require_configured_key(workflow, monkeypatch):
    client, jobs, history, _ = workflow
    job = await jobs.submit_command('text', {'source': 's'},
                                    {'version': 1, 'kind': 'text', 'source': 's', 'text': 'secret'})
    await history.record(question='Private?', response={'answer': 'yes', 'status': 'answered'},
                         duration_ms=1)
    monkeypatch.setenv('API_KEY', 'key')
    for url in ('/api/jobs', '/api/jobs/' + job['id'], '/api/history',
                '/api/jobs/' + job['id'] + '/events'):
        assert (await client.get(url)).status_code == 401
        assert (await client.get(url, headers={'X-API-Key': 'key'})).status_code == 200
    assert (await client.post('/api/jobs/' + job['id'] + '/cancel')).status_code == 401


@pytest.mark.asyncio
async def test_ask_history_async_and_best_effort(workflow, monkeypatch):
    client, _, history, app = workflow
    app.state.agent.ask.return_value = {'answer': 'A', 'status': 'answered'}
    result = await client.post('/api/ask', json={'question': 'Q?'})
    assert result.status_code == 200
    entries = (await client.get('/api/history')).json()['entries']
    assert entries[0]['answer'] == 'A'
    entry_id = entries[0]['id']
    saved = await client.post('/api/history/' + entry_id + '/save')
    assert saved.json()['saved'] is True
    monkeypatch.setattr(history, 'record',
                        AsyncMock(side_effect=RuntimeError('private sqlite path')))
    result = await client.post('/api/ask', json={'question': 'Q2?'})
    assert result.status_code == 200
    assert result.json()['answer'] == 'A'
    assert (await client.delete('/api/history/' + entry_id)).status_code == 200
    assert (await history.counts())['total'] == 0


@pytest.mark.asyncio
async def test_durable_file_wait_and_terminal_private_payload_purge(workflow):
    client, jobs, _, _ = workflow
    seen = []

    async def handle(command, progress, operation_id):
        seen.append(command)
        await progress('parsing')
        return {'success': True, 'nodes': 0, 'edges': 0}

    jobs.handler = handle
    await jobs.start()
    response = await client.post(
        '/api/ingest/file?wait=true',
        files={'file': ('private.txt', b'PRIVATE FILE MARKER', 'text/plain')},
    )
    assert response.status_code == 200
    assert response.json()['success'] is True
    assert seen[0]['kind'] == 'file'
    rows = await jobs.db.run(lambda conn: [dict(row) for row in conn.execute(
        'SELECT command,status FROM durable_jobs')])
    assert rows == [{'command': None, 'status': 'succeeded'}]
    listing = await client.get('/api/jobs')
    assert 'PRIVATE FILE MARKER' not in listing.text
    assert 'data_b64' not in listing.text


@pytest.mark.asyncio
async def test_durable_bad_key_and_graph_independent_control(workflow):
    client, jobs, _, app = workflow
    response = await client.post('/api/ingest/text',
                                 json={'text': 'text', 'source': 's'},
                                 headers={'Idempotency-Key': 'x' * 129})
    assert response.status_code == 422
    job = await jobs.submit_command('text', {},
                                    {'version': 1, 'kind': 'text', 'source': 's', 'text': 'text'})
    app.state.neo4j_available = False
    assert (await client.get('/api/jobs/' + job['id'] + '/events')).status_code == 200
    assert (await client.post('/api/jobs/' + job['id'] + '/cancel')).status_code == 200
