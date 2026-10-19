"""Opt-in durable startup and teardown use real local state without services."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

import src.main as main


@pytest.fixture
def dependencies(monkeypatch):
    graph = AsyncMock()
    graph.driver = object()
    agent = SimpleNamespace(nosana=SimpleNamespace(aclose=AsyncMock()))
    monkeypatch.setattr(main, 'Neo4jClient', lambda **kwargs: graph)
    monkeypatch.setattr(main, 'GraphAgent', lambda neo4j: agent)
    return graph, agent


@pytest.mark.asyncio
async def test_durable_lifespan_reopens_history(tmp_path, monkeypatch, dependencies):
    graph, agent = dependencies
    monkeypatch.setenv('STATE_DB_PATH', str(tmp_path / 'private' / 'state.sqlite3'))
    app = FastAPI()
    async with main.lifespan(app):
        assert app.state.storage_mode == 'sqlite'
        entry = await app.state.history.record(
            question='Persist?', response={'answer': 'Yes', 'status': 'answered'}, duration_ms=2)
        await app.state.history.set_saved(entry['id'], True)
    graph.close.assert_awaited_once()
    agent.nosana.aclose.assert_awaited_once()
    reopened = FastAPI()
    async with main.lifespan(reopened):
        assert (await reopened.state.history.get(entry['id']))['saved'] is True


@pytest.mark.asyncio
async def test_bad_durable_storage_aborts_and_closes_resources(tmp_path, monkeypatch, dependencies):
    graph, agent = dependencies
    public_dir = tmp_path / 'public'
    public_dir.mkdir(mode=0o755)
    public_dir.chmod(0o755)
    monkeypatch.setenv('STATE_DB_PATH', str(public_dir / 'state.sqlite3'))
    with pytest.raises((ValueError, PermissionError)):
        async with main.lifespan(FastAPI()):
            pytest.fail('must not silently fall back to memory')
    graph.close.assert_awaited_once()
    agent.nosana.aclose.assert_awaited_once()
