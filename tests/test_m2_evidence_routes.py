"""Evidence access enforces configured auth and current-excerpt lookup."""
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from src.api import routes

EVIDENCE_ID = 'ev_' + 'a' * 64


@pytest.fixture
async def evidence_client():
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    app.state.neo4j = AsyncMock()
    app.state.neo4j_available = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        yield client, app.state.neo4j


@pytest.mark.asyncio
async def test_evidence_auth_and_current_lookup(evidence_client, monkeypatch):
    client, graph = evidence_client
    monkeypatch.setenv('API_KEY', 'test-secret')
    response = await client.get('/api/evidence/' + EVIDENCE_ID)
    assert response.status_code == 401
    graph.get_evidence.assert_not_awaited()
    graph.get_evidence.return_value = {'id': EVIDENCE_ID, 'text': '<script>untrusted</script>'}
    response = await client.get('/api/evidence/' + EVIDENCE_ID,
                                headers={'X-API-Key': 'test-secret'})
    assert response.status_code == 200
    assert response.json()['text'] == '<script>untrusted</script>'
    graph.get_evidence.return_value = None
    assert (await client.get('/api/evidence/' + EVIDENCE_ID,
                             headers={'X-API-Key': 'test-secret'})).status_code == 404


@pytest.mark.asyncio
async def test_evidence_missing_invalid_and_sanitized_error(evidence_client):
    client, graph = evidence_client
    assert (await client.get('/api/evidence/invalid')).status_code == 404
    graph.get_evidence.assert_not_awaited()
    graph.get_evidence.side_effect = RuntimeError('private database credentials')
    response = await client.get('/api/evidence/' + EVIDENCE_ID)
    assert response.status_code == 500
    assert 'private' not in response.text


@pytest.mark.asyncio
async def test_source_evidence_auth_and_availability(evidence_client, monkeypatch):
    client, graph = evidence_client
    monkeypatch.setenv('API_KEY', 'test-secret')
    assert (await client.get('/api/sources/src_test/evidence')).status_code == 401
    graph.get_source_evidence.return_value = [{'id': EVIDENCE_ID, 'text': 'Original'}]
    response = await client.get('/api/sources/src_test/evidence',
                                headers={'X-API-Key': 'test-secret'})
    assert response.json() == {'evidence': [{'id': EVIDENCE_ID, 'text': 'Original'}]}
