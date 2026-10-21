"""Offline bounded usage, isolation and actual completion-report regressions."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.state import StateDatabase
from src.agent.usage import (
    QuotaExceeded,
    UsageConfig,
    UsageStore,
    record_completion,
    usage_scope,
)
from src.ingestion import entity_parser


def completion(prompt=3, output=7, *, text='answer', finish='stop'):
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=output),
        choices=[SimpleNamespace(finish_reason=finish,
                                 message=SimpleNamespace(content=text))],
    )


async def database(path):
    db = StateDatabase(path)
    await db.initialize()
    return db


@pytest.mark.parametrize('limit,window', [(-1, 3600), (1_000_001, 3600),
                                         (True, 3600), (0, 59), (0, 86401)])
def test_invalid_config(limit, window):
    with pytest.raises(ValueError, match='usage quota settings'):
        UsageConfig(limit, window)


def test_environment_invalid_fails_closed(monkeypatch):
    monkeypatch.setenv('USAGE_REQUEST_LIMIT', 'not-a-number')
    with pytest.raises(ValueError, match='usage quota settings'):
        UsageConfig.from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize('durable', [False, True])
async def test_concurrent_shared_quota_is_atomic(tmp_path, durable):
    db = await database(tmp_path / 'private' / 'state.sqlite3') if durable else None
    store = UsageStore(db, config=UsageConfig(7), clock=lambda: 3601)
    await store.initialize()
    peer = UsageStore(db, config=UsageConfig(7), clock=lambda: 3601) if durable else store
    await peer.initialize()

    async def attempt(i):
        try:
            await (store if i % 2 else peer).reserve('ask' if i % 3 else 'sandbox')
            return True
        except QuotaExceeded:
            return False

    assert sum(await asyncio.gather(*(attempt(i) for i in range(40)))) == 7
    snapshot = await store.snapshot()
    assert snapshot['ask_requests'] + snapshot['sandbox_requests'] == 7
    assert snapshot['window_start'] == 3600


@pytest.mark.asyncio
async def test_reopen_window_rollover_clock_rollback_and_bounded_rows(tmp_path):
    path = tmp_path / 'private' / 'state.sqlite3'
    now = [3601]
    store = UsageStore(await database(path), config=UsageConfig(1), clock=lambda: now[0])
    await store.initialize()
    await store.reserve('ask')
    reopened = UsageStore(await database(path), config=UsageConfig(1), clock=lambda: now[0])
    await reopened.initialize()
    with pytest.raises(QuotaExceeded):
        await reopened.reserve('sandbox')
    now[0] = 3600 * 2
    await reopened.reserve('sandbox')
    now[0] = 10  # Backward wall-clock changes do not replenish quota.
    with pytest.raises(QuotaExceeded):
        await reopened.reserve('ask')
    for i in range(3, 100):
        now[0] = 3600 * i
        await reopened.reserve('ask')
    assert await reopened.db.run(lambda conn: conn.execute(
        'SELECT count(*) FROM usage_window').fetchone()[0]) == 1


@pytest.mark.asyncio
async def test_overlapping_scopes_are_private_and_zero_is_reported(tmp_path):
    stores = [UsageStore(await database(tmp_path / name / 'state.sqlite3'))
              for name in ('alpha', 'beta')]
    for store in stores:
        await store.initialize()

    async def record(store, prompt):
        with usage_scope(store):
            await asyncio.sleep(0)
            await record_completion(completion(prompt, 0, text='TOP SECRET excerpt'))
            await record_completion(SimpleNamespace(usage=None))
            await record_completion(completion(-1, 2))

    await asyncio.gather(record(stores[0], 0), record(stores[1], 11))
    for store, expected in zip(stores, (0, 11), strict=True):
        snap = await store.snapshot()
        assert snap['reported_calls'] == 1
        assert snap['missing_usage_calls'] == 2
        assert snap['prompt_tokens'] == expected
        rows = await store.db.run(lambda conn: [tuple(row) for row in conn.execute(
            'SELECT * FROM usage_window')])
        assert 'TOP SECRET' not in str(rows)
        assert all(isinstance(value, int) for value in rows[0])
    # Neither nested nor exceptional contexts leak to the caller.
    with pytest.raises(RuntimeError), usage_scope(stores[0]):
        raise RuntimeError('scope ended')
    await record_completion(completion(100, 100))
    assert (await stores[0].snapshot())['prompt_tokens'] == 0


@pytest.mark.asyncio
async def test_recording_failure_does_not_break_answer_and_is_visible(caplog):
    db = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError('secret URI')))
    store = UsageStore(db)
    with usage_scope(store):
        await record_completion(completion())
    assert store.accounting_available is False
    assert 'secret URI' not in caplog.text
    assert 'recording unavailable' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('helper,text,finish', [
    ('answer_query', 'answer', 'stop'),
    ('extract_entities', '{}', 'length'),
    ('answer_with_citations', '{invalid', 'stop'),
])
async def test_parser_records_actual_usage_even_unusable_output(monkeypatch, helper, text, finish):
    monkeypatch.setenv('KIMI_API_KEY', 'test-key')
    create = AsyncMock(return_value=completion(13, 5, text=text, finish=finish))
    monkeypatch.setattr(entity_parser, 'get_kimi_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    store = UsageStore()
    with usage_scope(store):
        if helper == 'answer_with_citations':
            await entity_parser.answer_with_citations('question', [{
                'id': 'one', 'text': 'excerpt', 'source_id': 's',
                'version': 'v', 'start': 0, 'end': 7,
            }])
        else:
            args = ('question', 'context') if helper == 'answer_query' else ('text',)
            await getattr(entity_parser, helper)(*args)
    snap = await store.snapshot()
    assert (snap['reported_calls'], snap['prompt_tokens'], snap['completion_tokens']) == (1, 13, 5)
    assert snap['missing_usage_calls'] == 0
    assert 'temperature' not in create.call_args.kwargs


@pytest.mark.asyncio
async def test_provider_exception_counts_unknown_usage(monkeypatch):
    monkeypatch.setenv('KIMI_API_KEY', 'test-key')
    create = AsyncMock(side_effect=RuntimeError('provider failed'))
    monkeypatch.setattr(entity_parser, 'get_kimi_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    store = UsageStore()
    with usage_scope(store):
        await entity_parser.answer_query('question', 'context')
    snap = await store.snapshot()
    assert snap['reported_calls'] == 0
    assert snap['missing_usage_calls'] == 1


@pytest.mark.asyncio
async def test_cancellation_resets_scope_before_caller_continues():
    store = UsageStore()
    started = asyncio.Event()

    async def work():
        try:
            with usage_scope(store):
                started.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            # This runs in the cancelled task's own context, after scope exit.
            await record_completion(completion(100, 100))
            raise

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.snapshot())['reported_calls'] == 0


@pytest.mark.asyncio
async def test_lifespan_initializes_and_reopens_usage(tmp_path, monkeypatch):
    from fastapi import FastAPI

    import src.main as main

    monkeypatch.setenv('STATE_DB_PATH', str(tmp_path / 'private' / 'state.sqlite3'))
    monkeypatch.setenv('USAGE_REQUEST_LIMIT', '1')
    graph = AsyncMock()
    graph.driver = object()
    agent = SimpleNamespace(nosana=SimpleNamespace(aclose=AsyncMock()))
    monkeypatch.setattr(main, 'Neo4jClient', lambda **kwargs: graph)
    monkeypatch.setattr(main, 'GraphAgent', lambda neo4j: agent)
    app = FastAPI()
    async with main.lifespan(app):
        await app.state.usage.reserve('ask')
        with usage_scope(app.state.usage):
            await record_completion(completion(17, 8))
    reopened = FastAPI()
    async with main.lifespan(reopened):
        assert (await reopened.state.usage.snapshot())['prompt_tokens'] == 17
        with pytest.raises(QuotaExceeded):
            await reopened.state.usage.reserve('sandbox')
    assert graph.close.await_count == 2


@pytest.mark.asyncio
async def test_lifespan_rejects_invalid_usage_before_graph_work(monkeypatch):
    from fastapi import FastAPI

    import src.main as main

    monkeypatch.setenv('USAGE_WINDOW_SECONDS', '0')
    graph_factory = AsyncMock()
    monkeypatch.setattr(main, 'Neo4jClient', graph_factory)
    with pytest.raises(ValueError, match='usage quota settings'):
        async with main.lifespan(FastAPI()):
            pytest.fail('invalid configuration must not start')
    graph_factory.assert_not_called()
