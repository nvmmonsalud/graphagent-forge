"""Adversarial private HTTP boundary tests with actual identity and durable stores."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.agent.durable_history import SQLiteQueryHistory
from src.agent.durable_jobs import DurableJobManager
from src.agent.state import StateDatabase
from src.api import routes
from src.security.auth_routes import router as auth_router
from src.security.identity import IdentityStore
from src.security.middleware import WorkspaceMiddleware

ORIGIN = 'http://test'
TOKENS = {name: name + '-test-token-' + 'x' * 40 for name in ('alice', 'bob', 'reader')}


def bearer(principal='alice', workspace='a'):
    return {'Authorization': 'Bearer ' + TOKENS[principal], 'X-Workspace-ID': workspace}


@pytest.fixture
async def private_http(tmp_path, monkeypatch):
    monkeypatch.setenv('API_KEY', 'legacy-key-must-not-be-required')
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    config_path = directory / 'identities.json'
    config = {
        'version': 1,
        'workspaces': [{'id': 'a', 'title': 'A'}, {'id': 'b', 'title': 'B'}],
        'principals': [
            {'id': name, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
             'memberships': {'b' if name == 'bob' else 'a':
                             'reader' if name == 'reader' else 'editor'}}
            for name, token in TOKENS.items()
        ],
    }

    def rewrite():
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)

    rewrite()
    db = StateDatabase(directory / 'root.sqlite3')
    await db.initialize()
    identity = IdentityStore(db, config_path, [ORIGIN], secure=False)
    await identity.initialize()
    resources = {}
    for workspace in ('a', 'b'):
        state = StateDatabase(directory / workspace / 'state.sqlite3')
        await state.initialize()
        history = SQLiteQueryHistory(state)
        await history.initialize()
        await history.record(question='private-' + workspace,
                             response={'answer': 'answer-' + workspace, 'status': 'answered'},
                             duration_ms=1)
        jobs = DurableJobManager(state, AsyncMock())
        await jobs.initialize()
        await jobs.submit_command('text', {'source': workspace},
                                  {'version': 1, 'kind': 'text', 'source': workspace,
                                   'text': 'private-' + workspace})
        neo4j = AsyncMock()
        neo4j.get_all_graph_data.return_value = {
            'nodes': [{'id': 'same-id', 'label': 'private-' + workspace}], 'edges': []}
        neo4j.get_graph_data_by_source.return_value = {'nodes': [], 'edges': []}
        neo4j.get_sources.return_value = [{'source_doc': 'same-source', 'title': workspace}]
        neo4j.get_evidence.return_value = None
        neo4j.get_source_evidence.return_value = []
        agent = AsyncMock()
        agent.ask.return_value = {'answer': 'answer-' + workspace, 'status': 'answered'}
        agent.search_graph.return_value = {'nodes': [], 'edges': []}
        agent.find_path.return_value = {'nodes': [], 'edges': []}
        resources[workspace] = SimpleNamespace(
            neo4j=neo4j, agent=agent, jobs=jobs, history=history,
            neo4j_available=True, storage_mode='sqlite', ws_manager=AsyncMock())
    runtime = SimpleNamespace(get=AsyncMock(side_effect=lambda workspace: resources[workspace]))
    app = FastAPI()
    app.state.auth_mode = 'private'
    app.state.identity = identity
    app.state.workspace_runtime = runtime
    # Any fallback to app.state instead of a bound workspace is a regression.
    app.state.agent = AsyncMock(side_effect=AssertionError('unscoped fallback'))
    app.state.neo4j_available = False
    app.include_router(auth_router)
    app.include_router(routes.router, prefix='/api')
    app.add_middleware(WorkspaceMiddleware)
    routes._rate_buckets.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=ORIGIN) as client:
        yield SimpleNamespace(client=client, identity=identity, resources=resources,
                              runtime=runtime, config=config, rewrite=rewrite, app=app)
    for resource in resources.values():
        await resource.jobs.shutdown()
    routes._rate_buckets.clear()


@pytest.mark.parametrize('method,path', [
    ('GET', '/api/graph/data'), ('GET', '/api/graph/export'),
    ('GET', '/api/sources'), ('GET', '/api/evidence/guessed'),
    ('GET', '/api/sources/guessed/evidence'), ('GET', '/api/jobs'),
    ('GET', '/api/jobs/guessed'), ('GET', '/api/jobs/guessed/events'),
    ('GET', '/api/history'), ('POST', '/api/ask'),
    ('POST', '/api/ingest/text'), ('GET', '/api/graph/analytics'),
])
async def test_no_identity_never_resolves_resources_or_calls_provider(private_http, method, path):
    env = private_http
    result = await env.client.request(method, path, json={'question': 'secret'})
    assert result.status_code == 401
    assert 'no-store' in result.headers.get('cache-control', '')
    env.runtime.get.assert_not_awaited()
    for resource in env.resources.values():
        resource.agent.ask.assert_not_awaited()
        resource.neo4j.get_all_graph_data.assert_not_awaited()


async def test_cross_workspace_headers_ids_exports_and_replays(private_http):
    env = private_http
    b = env.resources['b']
    job_id = (await b.jobs.list())[0]['id']
    entry_id = (await b.history.list())[0]['id']
    for path in ('/api/graph/data', '/api/graph/export', '/api/history',
                 '/api/jobs/' + job_id, '/api/jobs/' + job_id + '/events',
                 '/api/evidence/guessed', '/api/sources/guessed/evidence'):
        denied = await env.client.get(path, headers=bearer(workspace='b'))
        assert denied.status_code == 403
        assert 'private-b' not in denied.text
    for path in ('/api/jobs/' + job_id, '/api/jobs/' + job_id + '/events'):
        assert (await env.client.get(path, headers=bearer())).status_code == 404
    saved = await env.client.post('/api/history/' + entry_id + '/save', headers=bearer())
    assert saved.status_code == 404
    for path in ('/api/graph/data', '/api/graph/export', '/api/history', '/api/jobs'):
        result = await env.client.get(path, headers=bearer())
        assert result.status_code == 200, result.text
        assert 'private-b' not in result.text and 'answer-b' not in result.text
    exported = await env.client.get('/api/graph/export', headers=bearer('bob', 'b'))
    assert 'private-b' in exported.text and 'private-a' not in exported.text


@pytest.mark.parametrize('method,path', [
    ('POST', '/api/ingest/text'), ('POST', '/api/ingest/url'),
    ('POST', '/api/graph/clear'), ('POST', '/api/graph/merge'),
    ('DELETE', '/api/sources?source_doc=same-source'),
    ('POST', '/api/history/guessed/save'), ('DELETE', '/api/history/guessed'),
    ('POST', '/api/jobs/guessed/cancel'), ('GET', '/api/graph/verify'),
    ('GET', '/api/graph/analytics'), ('POST', '/api/graph/verify/fanout'),
    ('POST', '/api/graph/verify/sweep'),
])
async def test_reader_cannot_mutate_or_spend_sandbox_compute(private_http, method, path):
    env = private_http
    result = await env.client.request(method, path, headers=bearer('reader'), json={})
    assert result.status_code == 403
    env.runtime.get.assert_not_awaited()


async def test_reader_can_ask_and_membership_revocation_is_immediate(private_http):
    env = private_http
    result = await env.client.post('/api/ask', headers=bearer('reader'),
                                   json={'question': 'What is known?'})
    assert result.status_code == 200, result.text
    env.resources['a'].agent.ask.assert_awaited_once()
    env.config['principals'][2]['memberships'] = {}
    env.rewrite()
    result = await env.client.post('/api/ask', headers=bearer('reader'),
                                   json={'question': 'Try again?'})
    assert result.status_code == 403
    assert env.resources['a'].agent.ask.await_count == 1


async def test_cookie_csrf_logout_switch_and_session_revocation(private_http):
    env = private_http
    payload = {'token': TOKENS['alice'], 'workspace_id': 'a'}
    assert (await env.client.post('/api/auth/session', json=payload)).status_code == 403
    login = await env.client.post('/api/auth/session', json=payload, headers={'Origin': ORIGIN})
    assert login.status_code == 200, login.text
    cookie = env.client.cookies.get('ga_session')
    assert cookie
    assert 'httponly' in login.headers.get('set-cookie', '').lower()
    assert (await env.client.get('/api/history')).status_code == 200
    for method, path, body in (
        ('POST', '/api/ask', {'question': 'CSRF'}),
        ('POST', '/api/auth/workspace', {'workspace_id': 'b'}),
        ('DELETE', '/api/auth/session', None),
    ):
        for headers in ({}, {'Origin': 'http://evil.invalid'}):
            result = await env.client.request(method, path, json=body, headers=headers)
            assert result.status_code == 403
    switch = await env.client.post('/api/auth/workspace', json={'workspace_id': 'b'},
                                    headers={'Origin': ORIGIN})
    assert switch.status_code == 403
    logout = await env.client.delete('/api/auth/session', headers={'Origin': ORIGIN})
    assert logout.status_code == 200
    env.client.cookies.set('ga_session', cookie)
    replay = await env.client.get('/api/history', headers={'X-Workspace-ID': 'a'})
    assert replay.status_code == 401
    env.resources['a'].agent.ask.assert_not_awaited()


async def test_unknown_methods_and_invalid_bearer_fail_closed(private_http):
    env = private_http
    for method in ('HEAD', 'OPTIONS', 'TRACE', 'PATCH'):
        response = await env.client.request(method, '/api/history', headers=bearer('reader'))
        assert response.status_code in (403, 405)
    response = await env.client.post('/api/ask', headers={
        'Authorization': 'Bearer incorrect', 'X-Workspace-ID': 'a'},
        json={'question': 'Never call the provider'})
    assert response.status_code == 401
    env.resources['a'].agent.ask.assert_not_awaited()


async def test_unauthorized_malformed_json_is_rejected_before_validation(private_http):
    env = private_http
    response = await env.client.post('/api/ingest/text', content=b'{this is not JSON',
                                     headers={'Content-Type': 'application/json'})
    assert response.status_code == 401
    env.runtime.get.assert_not_awaited()


async def test_cookie_workspace_switch_rebinds_resources_without_shared_history(private_http):
    env = private_http
    env.config['principals'][0]['memberships']['b'] = 'editor'
    env.rewrite()
    assert (await env.client.post('/api/auth/session', headers={'Origin': ORIGIN},
                                  json={'token': TOKENS['alice'],
                                        'workspace_id': 'a'})).status_code == 200
    before = await env.client.get('/api/history')
    assert 'private-a' in before.text and 'private-b' not in before.text
    switched = await env.client.post('/api/auth/workspace', headers={'Origin': ORIGIN},
                                      json={'workspace_id': 'b'})
    assert switched.status_code == 200
    after = await env.client.get('/api/history')
    assert 'private-b' in after.text and 'private-a' not in after.text
    assert env.client.cookies.get('ga_workspace') == 'b'
    # Explicit selection still checks membership instead of trusting the cookie.
    env.config['principals'][0]['memberships'].pop('b')
    env.rewrite()
    assert (await env.client.get('/api/history')).status_code == 403


async def test_expired_cookie_and_invalid_reloaded_config_fail_closed(private_http):
    env = private_http
    login = await env.client.post('/api/auth/session', headers={'Origin': ORIGIN},
                                  json={'token': TOKENS['alice'], 'workspace_id': 'a'})
    assert login.status_code == 200
    await env.identity.db.run(lambda c: c.execute('UPDATE auth_sessions SET expires=0'), write=True)
    assert (await env.client.get('/api/history')).status_code == 401
    env.runtime.get.assert_not_awaited()
    env.config['version'] = 999
    env.rewrite()
    response = await env.client.post('/api/ask', headers=bearer(), json={'question': 'No call'})
    assert response.status_code == 503
    env.resources['a'].agent.ask.assert_not_awaited()


async def test_identical_evidence_ids_resolve_only_within_authorized_workspace(private_http):
    env = private_http
    for key, resource in env.resources.items():
        resource.neo4j.get_evidence.return_value = {
            'id': 'ev_' + 'a' * 64, 'source_id': 'same-source', 'version': 'v1',
            'text': 'private-excerpt-' + key, 'start': 0, 'end': 17,
        }
    for principal, workspace in (('alice', 'a'), ('bob', 'b'), ('reader', 'a')):
        response = await env.client.get('/api/evidence/ev_' + 'a' * 64,
                                        headers=bearer(principal, workspace))
        assert response.status_code == 200, response.text
        assert response.json()['text'] == 'private-excerpt-' + workspace
        assert 'no-store' in response.headers.get('cache-control', '')
    denied = await env.client.get('/api/evidence/ev_' + 'a' * 64, headers=bearer('alice', 'b'))
    assert denied.status_code == 403 and 'private-excerpt' not in denied.text


async def test_reader_indirect_search_and_paths_use_selected_agent(private_http):
    env = private_http
    for path, payload in (
        ('/api/graph/search', {'query': 'private-b'}),
        ('/api/graph/path', {'from_label': 'same-id', 'to_label': 'private-b'}),
    ):
        result = await env.client.post(path, headers=bearer('reader'), json=payload)
        assert result.status_code == 200, result.text
        assert 'private-b' not in result.text
    env.resources['b'].agent.search_graph.assert_not_awaited()
    env.resources['b'].agent.find_path.assert_not_awaited()
    env.resources['a'].agent.search_graph.assert_awaited_once_with('private-b')
    env.resources['a'].agent.find_path.assert_awaited_once_with('same-id', 'private-b')


async def test_private_ingest_persists_server_identity_for_later_execution(private_http):
    env = private_http
    submitted = await env.client.post('/api/ingest/text', headers=bearer(), json={
        'text': 'private command', 'source': 'source',
        '_principal_id': 'bob', '_workspace_id': 'b',
    })
    assert submitted.status_code == 202, submitted.text
    command = await env.resources['a'].jobs.db.run(lambda c: c.execute(
        'SELECT command FROM durable_jobs WHERE id=?', (submitted.json()['job_id'],)
    ).fetchone()[0])
    command = json.loads(command)
    assert command['_principal_id'] == 'alice'
    assert command['_workspace_id'] == 'a'
    assert len(await env.resources['b'].jobs.list()) == 1
    env.resources['a'].agent.ingest_text.assert_not_awaited()
