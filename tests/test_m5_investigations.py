"""Saved research CAS, metadata-only evidence notes and workspace-scoped API flows."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from src.agent.investigations import InvestigationConflict, InvestigationLimit, InvestigationStore
from src.agent.state import StateDatabase
from src.api.investigation_routes import router
from src.security.identity import IdentityStore
from src.security.middleware import WorkspaceMiddleware

EV = 'ev_' + 'a' * 64
TOKENS = {name: name + '-m5-test-' + 'x' * 40 for name in ('alice', 'bob', 'reader')}


def headers(name='alice', workspace='a'):
    return {'Authorization': 'Bearer ' + TOKENS[name], 'X-Workspace-ID': workspace}


@pytest.fixture(params=[False, True], ids=['memory', 'sqlite'])
async def store(request, tmp_path):
    db = None
    if request.param:
        db = StateDatabase(tmp_path / 'private' / 'state.sqlite3')
        await db.initialize()
    result = InvestigationStore(db)
    await result.initialize()
    return result


async def test_store_bounds_whitelist_and_revision_cas(store):
    first = await store.save({'title': 'Research', 'query': 'Question?'})
    assert first['revision'] == 1
    assert first['annotations'] == []
    assert len(await store.list()) == 1
    second = await store.save({'title': 'Updated'}, first['id'], revision=1)
    assert second['revision'] == 2 and second['created_at'] == first['created_at']
    with pytest.raises(InvestigationConflict):
        await store.save({'title': 'Lost edit'}, first['id'], revision=1)
    assert (await store.get(first['id']))['title'] == 'Updated'
    assert await store.get('guess') is None
    assert await store.save({'title': 'Missing'}, 'guess', revision=1) is None
    assert await store.delete(first['id'])
    assert not await store.delete(first['id'])
    for invalid in ({'title': 'x', 'raw_excerpt': 'secret'}, {'title': 'x' * 201},
                    {'title': 'x', 'selected_entity_ids': ['a'] * 2},
                    {'title': 'x', 'annotations': [{'evidence_id': EV, 'note': 'hello'}]}):
        with pytest.raises((ValueError, ValidationError)):
            await store.save(invalid)


async def test_store_capacity_and_concurrent_edits(store):
    first = await store.save({'title': 'Original'})
    results = await asyncio.gather(
        store.save({'title': 'A'}, first['id'], revision=1),
        store.save({'title': 'B'}, first['id'], revision=1), return_exceptions=True)
    assert sum(isinstance(item, InvestigationConflict) for item in results) == 1
    assert (await store.get(first['id']))['revision'] == 2
    for index in range(49):
        await store.save({'title': str(index)})
    assert len(await store.list()) == 50
    with pytest.raises(InvestigationLimit):
        await store.save({'title': 'Overflow'})


async def test_sqlite_reopen_preserves_metadata_notes_without_excerpts(tmp_path):
    db = StateDatabase(tmp_path / 'private' / 'state.sqlite3')
    await db.initialize()
    first = InvestigationStore(db)
    await first.initialize()
    saved = await first.save({'title': 'Durable', 'annotations': [{
        'evidence_id': EV, 'note': '<img src=x onerror=alert(1)>',
        'source_id': 'source', 'version': 'v1', 'start': 42, 'end': 84}]})
    second = InvestigationStore(db)
    await second.initialize()
    assert await second.get(saved['id']) == saved
    payload = await db.run(lambda c: c.execute('SELECT payload FROM investigations').fetchone()[0])
    assert 'text' not in json.loads(payload)['annotations'][0]
    assert json.loads(payload)['annotations'][0]['note'] == '<img src=x onerror=alert(1)>'


@pytest.fixture
async def research_http(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    config = directory / 'identities.json'
    config.write_text(json.dumps({'version': 1,
        'workspaces': [{'id': key, 'title': key} for key in ('a', 'b')],
        'principals': [{'id': name, 'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
                        'memberships': {'b' if name == 'bob' else 'a':
                                        'reader' if name == 'reader' else 'editor'}}
                       for name, token in TOKENS.items()]}))
    config.chmod(0o600)
    database = StateDatabase(directory / 'root.sqlite3')
    await database.initialize()
    identity = IdentityStore(database, config, ['http://test'], secure=False)
    await identity.initialize()
    resources = {}
    for key in ('a', 'b'):
        db = StateDatabase(directory / key / 'state.sqlite3')
        await db.initialize()
        investigations = InvestigationStore(db)
        await investigations.initialize()
        neo4j = AsyncMock()
        neo4j.get_evidence.return_value = {
            'id': EV, 'source_id': 'source-' + key, 'version': 'v1',
            'start': 42, 'end': 84, 'text': 'SECRET EXCERPT MUST NOT BE SAVED'}
        neo4j.get_entity_context.return_value = {
            'nodes': [{'id': 'same-id', 'label': key}], 'edges': [],
            'source_docs': ['source-' + key], 'text': 'summary-' + key}
        neo4j.browse_graph.return_value = {'nodes': [{'id': 'same-id'}], 'edges': [],
                                           'next_id': 'same-id', 'limit': 100}
        resources[key] = SimpleNamespace(investigations=investigations,
                                         neo4j=neo4j, neo4j_available=True)
    app = FastAPI()
    app.state.auth_mode = 'private'
    app.state.identity = identity
    runtime = SimpleNamespace(get=AsyncMock(side_effect=lambda key: resources[key]))
    app.state.workspace_runtime = runtime
    app.include_router(router)
    app.add_middleware(WorkspaceMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://test') as client:
        yield SimpleNamespace(client=client, resources=resources, runtime=runtime)


async def test_annotation_roundtrip_staleness_and_cas_through_http(research_http):
    env = research_http
    body = {'title': 'Investigation', 'query': 'Why?', 'selected_entity_ids': ['same-id'],
            'annotations': [{'evidence_id': EV, 'note': '<svg/onload=alert(1)>'}]}
    created = await env.client.post('/api/research/investigations', headers=headers(), json=body)
    assert created.status_code == 201, created.text
    view = created.json()
    assert view['annotations'][0]['stale'] is False
    assert view['annotations'][0]['source_id'] == 'source-a'
    assert 'SECRET EXCERPT' not in created.text
    stored = await env.resources['a'].investigations.get(view['id'])
    assert 'stale' not in stored['annotations'][0] and 'text' not in stored['annotations'][0]
    env.resources['a'].neo4j.get_evidence.return_value = None
    opened = await env.client.get('/api/research/investigations/' + view['id'], headers=headers())
    assert opened.json()['annotations'][0]['stale'] is True
    update = {**body, 'title': 'Renamed', 'revision': 1}
    saved = await env.client.put('/api/research/investigations/' + view['id'],
                                 headers=headers(), json=update)
    assert saved.status_code == 200 and saved.json()['revision'] == 2
    assert saved.json()['annotations'][0]['stale'] is True
    assert (await env.client.put('/api/research/investigations/' + view['id'],
                                headers=headers(), json=update)).status_code == 409
    stale_new = await env.client.post('/api/research/investigations', headers=headers(), json=body)
    assert stale_new.status_code == 409


async def test_reader_guessed_ids_and_workspace_isolation(research_http):
    env = research_http
    created = await env.client.post('/api/research/investigations', headers=headers(),
                                    json={'title': 'Private A'})
    view_id = created.json()['id']
    base = '/api/research/investigations'
    guessed = await env.client.get(base + '/' + view_id, headers=headers('bob', 'b'))
    assert guessed.status_code == 404
    other = await env.client.get(base, headers=headers('bob', 'b'))
    assert other.json() == {'investigations': []}
    reader = await env.client.get(base + '/' + view_id, headers=headers('reader'))
    assert reader.status_code == 200
    for method, path, body in (
        ('POST', base, {'title': 'Reader'}),
        ('PUT', base + '/' + view_id, {'title': 'Reader', 'revision': 1}),
        ('DELETE', base + '/' + view_id, None),
    ):
        response = await env.client.request(method, path, headers=headers('reader'), json=body)
        assert response.status_code == 403
    denied = await env.client.get(base + '/' + view_id, headers=headers('alice', 'b'))
    assert denied.status_code == 403
    guessed_delete = await env.client.delete(base + '/' + view_id, headers=headers('bob', 'b'))
    assert guessed_delete.status_code == 404
    assert (await env.client.delete(base + '/' + view_id, headers=headers())).status_code == 200


async def test_annotation_metadata_cannot_be_spoofed_and_list_has_no_note_bodies(research_http):
    env = research_http
    body = {'title': 'Notes', 'annotations': [{'evidence_id': EV, 'note': 'user note'}]}
    invalid = {**body, 'annotations': [{**body['annotations'][0], 'version': 'forged'}]}
    assert (await env.client.post('/api/research/investigations',
                                 headers=headers(), json=invalid)).status_code == 422
    created = await env.client.post('/api/research/investigations', headers=headers(), json=body)
    assert created.status_code == 201
    listing = await env.client.get('/api/research/investigations', headers=headers())
    assert 'user note' not in listing.text and 'annotations' not in listing.text
    assert listing.json()['investigations'][0]['annotation_count'] == 1


async def test_comparison_exact_ids_missing_and_reader_permission(research_http):
    env = research_http
    response = await env.client.post('/api/research/compare', headers=headers('reader'),
                                     json={'entity_ids': ['same-id', 'missing']})
    assert response.status_code == 200, response.text
    assert response.json()['missing_ids'] == ['missing']
    assert response.json()['contexts'][0]['source_docs'] == ['source-a']
    env.resources['b'].neo4j.get_entity_context.assert_not_awaited()
    env.resources['a'].neo4j.get_entity_context.assert_any_await('same-id', depth=1)
    for ids in ([], ['x'] * 6, ['same-id', 'same-id'], ['x' * 501]):
        assert (await env.client.post('/api/research/compare', headers=headers('reader'),
                                      json={'entity_ids': ids})).status_code == 422


async def test_browse_cursor_roundtrip_source_binding_and_limits(research_http):
    env = research_http
    client = env.client
    first = await client.get('/api/research/browse?source_id=source-a', headers=headers('reader'))
    assert first.status_code == 200, first.text
    cursor = first.json()['next_cursor']
    assert len(cursor) <= 1024 and 'next_id' not in first.json()
    second = await client.get('/api/research/browse', headers=headers('reader'),
                              params={'cursor': cursor, 'source_id': 'source-a', 'limit': 17})
    assert second.status_code == 200
    env.resources['a'].neo4j.browse_graph.assert_awaited_with(
        after_id='same-id', limit=17, source_id='source-a')
    for params in ({'cursor': cursor, 'source_id': 'source-b'}, {'cursor': 'malformed'},
                   {'cursor': 'x' * 1025}, {'limit': 101}):
        response = await client.get('/api/research/browse',
                                    headers=headers('reader'), params=params)
        assert response.status_code == 422
    env.resources['b'].neo4j.browse_graph.assert_not_awaited()


async def test_research_surfaces_authenticate_before_any_store_or_graph(research_http):
    env = research_http
    for method, path in [('GET', '/api/research/investigations'),
                         ('GET', '/api/research/investigations/guess'),
                         ('POST', '/api/research/compare'), ('GET', '/api/research/browse')]:
        assert (await env.client.request(method, path)).status_code == 401
    env.runtime.get.assert_not_awaited()


async def test_long_source_and_entity_ids_still_produce_bounded_usable_cursor(research_http):
    env = research_http
    env.resources['a'].neo4j.browse_graph.return_value['next_id'] = 'n' * 500
    response = await env.client.get('/api/research/browse', headers=headers(),
                                    params={'source_id': 's' * 500})
    cursor = response.json()['next_cursor']
    assert len(cursor) <= 1024
    resumed = await env.client.get('/api/research/browse', headers=headers(),
                                   params={'source_id': 's' * 500, 'cursor': cursor})
    assert resumed.status_code == 200


async def test_existing_note_keeps_original_metadata_when_current_version_differs(research_http):
    env = research_http
    created = await env.client.post('/api/research/investigations', headers=headers(), json={
        'title': 'Note', 'annotations': [{'evidence_id': EV, 'note': 'Original'}]})
    view = created.json()
    env.resources['a'].neo4j.get_evidence.return_value['version'] = 'replaced'
    opened = await env.client.get('/api/research/investigations/' + view['id'], headers=headers())
    assert opened.json()['annotations'][0]['stale'] is True
    edited = await env.client.put('/api/research/investigations/' + view['id'], headers=headers(),
                                   json={'title': 'Note', 'revision': 1,
                                         'annotations': [{'evidence_id': EV, 'note': 'Edited'}]})
    assert edited.status_code == 200
    assert edited.json()['annotations'][0]['version'] == 'v1'
    assert edited.json()['annotations'][0]['stale'] is True


@pytest.mark.parametrize('payload', [
    {'title': ''}, {'title': 'x', 'query': 'x' * 4001},
    {'title': 'x', 'source_id': 's' * 501},
    {'title': 'x', 'selected_entity_ids': ['x'] * 6},
    {'title': 'x', 'annotations': [{
        'evidence_id': EV, 'note': 'x' * 2001, 'source_id': 's',
        'version': 'v', 'start': 0, 'end': 1}]},
    {'title': 'x', 'annotations': [{
        'evidence_id': EV, 'note': 'n', 'source_id': 's',
        'version': 'v', 'start': 2, 'end': 1}]},
])
async def test_store_rejects_unbounded_or_invalid_fields_without_http(store, payload):
    with pytest.raises(ValueError):
        await store.save(payload)
    assert await store.list() == []
