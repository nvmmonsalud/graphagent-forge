"""Resource lifecycle attaches and reopens all research services on actual disk."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.durable_jobs import DurableJobManager
from src.agent.research_services import initialize_research, shutdown_research
from src.agent.state import StateDatabase


@pytest.mark.asyncio
async def test_resource_services_reopen_and_shutdown(tmp_path):
    path = tmp_path / 'private' / 'state.sqlite3'

    async def resources():
        db = StateDatabase(path)
        await db.initialize()
        graph = AsyncMock()
        graph.get_source.return_value = None
        jobs = DurableJobManager(db, AsyncMock())
        await jobs.initialize()
        result = SimpleNamespace(database=db, jobs=jobs, neo4j=graph,
                                 agent=SimpleNamespace(neo4j=graph))
        await initialize_research(result)
        return result

    first = await resources()
    try:
        view = await first.investigations.save({'title': 'Research view'})
        refresh = await first.research_jobs.create_refresh(
            'https://example.org/?private=token', 3600)
    finally:
        await shutdown_research(first)
        await first.jobs.shutdown()
    second = await resources()
    try:
        assert (await second.investigations.get(view['id']))['title'] == 'Research view'
        schedules = await second.research_jobs.list_refreshes()
        assert schedules[0]['id'] == refresh['id']
        assert 'private=token' not in str(schedules)
    finally:
        await shutdown_research(second)
        await second.jobs.shutdown()
