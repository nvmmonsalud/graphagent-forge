"""HTTP quotas cover Ask/sandbox together, after auth and before execution."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from test_m4_api import bearer

from src.agent.usage import UsageConfig, UsageStore, record_completion
from src.api import routes

# Reuse the actual IdentityStore + workspace middleware fixture.
pytest_plugins = ["test_m4_api"]


@pytest.fixture
async def local_http():
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    store = UsageStore(config=UsageConfig(2))
    await store.initialize()

    async def answer(question):
        await record_completion(SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=4, completion_tokens=2)))
        return {'answer': 'answer', 'status': 'answered'}

    app.state.usage = store
    app.state.agent = SimpleNamespace(ask=AsyncMock(side_effect=answer),
                                      get_analytics=AsyncMock())
    app.state.neo4j_available = True
    app.state.neo4j = AsyncMock()
    routes._rate_buckets.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as client:
        yield app, client
    routes._rate_buckets.clear()


@pytest.mark.asyncio
async def test_shared_gate_auth_first_and_existing_schema(local_http, monkeypatch):
    app, client = local_http
    monkeypatch.setenv('API_KEY', 'test-secret')
    assert (await client.post('/api/ask', json={'question': 'q'})).status_code == 401
    assert (await client.get('/api/usage')).status_code == 401
    assert (await app.state.usage.snapshot())['ask_requests'] == 0
    app.state.agent.ask.assert_not_awaited()
    headers = {'X-API-Key': 'test-secret'}
    result = await client.post('/api/ask', json={'question': 'q'}, headers=headers)
    assert result.json() == {'answer': 'answer', 'status': 'answered'}
    # Verify quota reservation separately from the sandbox implementation.
    app.state.agent.get_analytics.return_value = {'ok': True}
    response = await client.get('/api/graph/analytics', headers=headers)
    assert response.status_code == 200, response.text
    rejected = await client.post('/api/ask', json={'question': 'q'}, headers=headers)
    assert rejected.status_code == 429
    assert app.state.agent.ask.await_count == 1
    snap = (await client.get('/api/usage', headers=headers)).json()
    assert snap['ask_requests'] == snap['sandbox_requests'] == 1
    assert snap['prompt_tokens'] == 4
    assert 'test-secret' not in str(snap)
    # Context has reset on return; unrelated helper work is excluded.
    await record_completion(SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=100, completion_tokens=100)))
    assert (await app.state.usage.snapshot())['prompt_tokens'] == 4


@pytest.mark.asyncio
async def test_graph_unavailable_is_not_charged(local_http):
    app, client = local_http
    app.state.neo4j_available = False
    assert (await client.post('/api/ask', json={'question': 'q'})).status_code == 503
    assert (await app.state.usage.snapshot())['ask_requests'] == 0


@pytest.mark.asyncio
async def test_missing_or_failed_store_fails_closed_before_work(local_http, monkeypatch):
    app, client = local_http
    monkeypatch.setenv('USAGE_REQUEST_LIMIT', '2')
    del app.state.usage
    assert (await client.post('/api/ask', json={'question': 'q'})).status_code == 503
    app.state.usage = SimpleNamespace(reserve=AsyncMock(side_effect=RuntimeError('secret URI')))
    response = await client.post('/api/ask', json={'question': 'q'})
    assert response.status_code == 503
    assert 'secret' not in response.text
    app.state.agent.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_execution_consumes_reservation_and_scope_resets(local_http):
    app, client = local_http
    app.state.agent.ask.side_effect = RuntimeError('not sent to client')
    result = await client.post('/api/ask', json={'question': 'q'})
    assert result.status_code == 500
    assert (await app.state.usage.snapshot())['ask_requests'] == 1
    await record_completion(SimpleNamespace(usage=None))
    assert (await app.state.usage.snapshot())['missing_usage_calls'] == 0


@pytest.mark.asyncio
async def test_private_membership_roles_and_workspace_quota_isolation(private_http):
    env = private_http
    for resource in env.resources.values():
        resource.usage = UsageStore(resource.history.db, config=UsageConfig(1))
        await resource.usage.initialize()
    for path in ('/api/usage', '/api/graph/verify'):
        assert (await env.client.get(path)).status_code == 401
        assert (await env.client.get(path, headers=bearer(workspace='b'))).status_code == 403
    for resource in env.resources.values():
        assert (await resource.usage.snapshot())['ask_requests'] == 0
    assert (await env.client.get('/api/graph/verify', headers=bearer('reader'))).status_code == 403
    assert (await env.client.get('/api/usage', headers=bearer('reader'))).status_code == 200
    first = await env.client.post('/api/ask', headers=bearer('reader'), json={'question': 'q'})
    assert first.status_code == 200
    assert (await env.client.post('/api/ask', headers=bearer(),
                                   json={'question': 'q'})).status_code == 429
    assert (await env.client.post('/api/ask', headers=bearer('bob', 'b'),
                                   json={'question': 'q'})).status_code == 200
    env.config['principals'][2]['memberships'] = {}
    env.rewrite()
    assert (await env.client.get('/api/usage', headers=bearer('reader'))).status_code == 403


@pytest.mark.asyncio
async def test_usage_read_marks_failed_accounting(local_http, monkeypatch):
    app, client = local_http
    store = app.state.usage
    original = store._change
    with monkeypatch.context() as patch:
        patch.setattr(store, '_change', AsyncMock(side_effect=RuntimeError('secret db path')))
        await store.record(SimpleNamespace(usage=None))
    assert store._change == original
    response = await client.get('/api/usage')
    assert response.status_code == 200
    assert response.json()['accounting_available'] is False
    assert 'secret' not in response.text
