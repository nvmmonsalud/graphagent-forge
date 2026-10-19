"""Isolation at query, command, live delivery and resource boundaries."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.agent.core import GraphAgent
from src.agent.durable_jobs import DurableJobManager
from src.agent.state import StateDatabase
from src.graph.workspace_scope import namespace, scope_query
from src.security.workspaces import WorkspaceAccessRevoked, WorkspaceConnectionManager


@pytest.mark.parametrize('identifier', ['', '../x', 'A', 'a.b', 'a'*49, None])
def test_workspace_namespace_rejects_unsafe_identifiers(identifier):
    with pytest.raises(ValueError):
        namespace(identifier)


def test_namespace_is_deterministic_full_digest_and_partitioned():
    assert namespace('a') == namespace('a')
    assert namespace('a') != namespace('b')
    assert len(namespace('a')) == 66


@pytest.mark.parametrize('pattern', [
    'MATCH (n:Entity)-[r]->(m)',
    'MATCH (n:Entity)--()',
    'MATCH (n:Entity)<-[r]-()',
    'MATCH path = (n:Entity)-[r*1..4]-(m:Entity) RETURN m',
    'MATCH path = shortestPath((a:Entity)-[*..6]-(b:Entity)) RETURN path',
])
def test_implicit_relationship_patterns_receive_namespace(pattern):
    prefix = namespace('a')
    rewritten = scope_query(pattern, prefix)
    assert ':'+prefix+'RELATES_TO' in rewritten
    assert ':Entity' not in rewritten
    if 'path =' in pattern:
        assert 'all(__workspace_node IN nodes(path)' in rewritten


def test_rewriter_preserves_literals_parameters_and_source_chunk_labels():
    rewritten = scope_query(
        "MATCH (s:Source)-[:HAS_CHUNK]->(c:EvidenceChunk) "
        "RETURN 'Entity:RELATES_TO', $source_id, c.text", namespace('a'))
    assert "'Entity:RELATES_TO'" in rewritten
    assert '$source_id' in rewritten
    assert ':' + namespace('a') + 'Source' in rewritten
    assert ':' + namespace('a') + 'HAS_CHUNK' in rewritten


def test_vector_schema_and_lookup_index_scoped_together():
    prefix = namespace('a')
    assert prefix+'entity_semantic_embedding' in scope_query(
        'CREATE VECTOR INDEX entity_semantic_embedding IF NOT EXISTS '
        'FOR (n:SemanticEntity) ON (n.semantic_embedding)', prefix)
    assert "'"+prefix+"entity_semantic_embedding'" in scope_query(
        "CALL db.index.vector.queryNodes('entity_semantic_embedding', $limit, $embedding)", prefix)


def test_injected_manager_prevents_global_broadcast_fallback():
    manager = object()
    assert GraphAgent(object(), ws_manager=manager)._get_ws_manager() is manager


async def test_ws_reauthenticates_each_delivery_and_disconnects_revoked_subscription():
    identity = SimpleNamespace(authenticate=AsyncMock(return_value={'principal_id': 'a'}),
                               authorize=AsyncMock())
    socket = SimpleNamespace(headers={}, cookies={}, accept=AsyncMock(),
                             send_text=AsyncMock(), close=AsyncMock())
    manager = WorkspaceConnectionManager(identity, 'a')
    assert await manager.connect(socket)
    await manager.broadcast({'type': 'graph_reset'})
    assert socket.send_text.await_count == 1
    identity.authorize.side_effect = HTTPException(403, 'workspace access denied')
    await manager.broadcast({'private': 'must not be sent'})
    assert socket.send_text.await_count == 1
    assert not manager.active_connections
    assert identity.authenticate.await_count == 3
    socket.close.assert_awaited_once()


async def test_revocation_is_cancelled_and_does_not_kill_durable_worker(tmp_path):
    async def handler(command, progress, operation_id):
        if command['text'] == 'revoke':
            await manager.cancel(operation_id)
            raise WorkspaceAccessRevoked()
        return {'success': True}

    manager = DurableJobManager(StateDatabase(tmp_path/'private'/'state.sqlite3'),
                                 handler, workers=1)
    await manager.initialize()
    await manager.start()
    try:
        command = {'version': 1, 'kind': 'text', 'text': 'revoke', 'source': 'x'}
        first = await manager.submit_command('text', {}, command)
        assert (await asyncio.wait_for(manager.wait(first['id']), 5))['status'] == 'cancelled'
        second = await manager.submit_command('text', {}, {**command, 'text': 'valid'})
        assert (await asyncio.wait_for(manager.wait(second['id']), 5))['status'] == 'succeeded'
        assert not manager._tasks[0].done()
        assert await manager.db.run(lambda c: c.execute(
            'SELECT command FROM durable_jobs WHERE id=?', (first['id'],)).fetchone()[0]) is None
    finally:
        await manager.shutdown()


async def test_runtime_commands_bind_identity_and_cancel_revoked_stages(tmp_path):
    from src.security.workspaces import WorkspaceRuntime

    identity = SimpleNamespace(
        configuration=AsyncMock(return_value=({'a': {}, 'b': {}}, {})),
        authorize_principal=AsyncMock(),
    )
    constructed = []

    def neo4j_factory(**kwargs):
        client = SimpleNamespace(connect=AsyncMock(), init_schema=AsyncMock(),
                                 close=AsyncMock(),
                                 get_ingest_operation=AsyncMock(return_value=None))
        constructed.append((kwargs['workspace_id'], client))
        return client

    def agent_factory(client, ws_manager):
        async def ingest_text(text, source, progress, operation_id):
            from src.ingestion.graph_writer import ingestion_authorization
            assert ingestion_authorization.get() is not None
            await progress('fetching')
            if text == 'revoke':
                identity.authorize_principal.side_effect = HTTPException(403, 'denied')
            await progress('extracting')
            return {'success': True}
        return SimpleNamespace(neo4j=client, ingest_text=ingest_text,
                               nosana=SimpleNamespace(aclose=AsyncMock()))

    runtime = WorkspaceRuntime(identity, tmp_path/'private'/'state.sqlite3',
                               neo4j_factory=neo4j_factory, agent_factory=agent_factory)
    try:
        a, b = await runtime.get('a'), await runtime.get('b')
        assert a is await runtime.get('a')
        assert a.database.path != b.database.path
        assert a.ws_manager is not b.ws_manager
        command = {'version': 1, 'kind': 'text', 'text': 'revoke', 'source': 'note',
                   '_principal_id': 'alice', '_workspace_id': 'a'}
        record = await a.jobs.submit_command('text', {}, command)
        assert (await asyncio.wait_for(a.jobs.wait(record['id']), 5))['status'] == 'cancelled'
        assert await b.jobs.get(record['id']) is None
        identity.authorize_principal.side_effect = None
        record = await a.jobs.submit_command('text', {}, {**command, 'text': 'valid'})
        assert (await asyncio.wait_for(a.jobs.wait(record['id']), 5))['status'] == 'succeeded'
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("content", ["one chunk", "text " * 15000])
async def test_revocation_during_chunking_stops_later_calls_and_publication(monkeypatch, content):
    from src.ingestion import graph_writer

    calls = []
    revoked = False

    async def check():
        if revoked:
            raise WorkspaceAccessRevoked()

    async def extract(text, model=None):
        nonlocal revoked
        calls.append(text)
        revoked = True
        await asyncio.sleep(0)
        return {'nodes': [], 'edges': []}

    monkeypatch.setattr(graph_writer, 'extract_entities', extract)
    neo4j = SimpleNamespace(get_source=AsyncMock(return_value=None),
                            publish_source_graph=AsyncMock())
    context = graph_writer.ingestion_authorization.set(check)
    try:
        with pytest.raises(WorkspaceAccessRevoked):
            await graph_writer.ingest_to_graph(
                neo4j, content, source={'id': 's', 'version': 'v1'})
        assert len(calls) == 1
        neo4j.publish_source_graph.assert_not_awaited()
    finally:
        graph_writer.ingestion_authorization.reset(context)


@pytest.mark.parametrize('query', [
    'MATCH (n:Unknown) RETURN n',
    'MATCH (n:Entity)-[:UNKNOWN]->(m:Entity) RETURN m',
    'CREATE INDEX new_unreviewed_index IF NOT EXISTS FOR (n:Entity) ON (n.label)',
])
def test_unknown_schema_fails_closed(query):
    with pytest.raises(ValueError):
        scope_query(query, namespace('a'))


@pytest.mark.parametrize("method", ["execute_read", "execute_write", "read_transaction",
                                   "write_transaction", "execute_query"])
def test_scoped_runner_cannot_expose_unwrapped_transaction_callbacks(method):
    from src.graph.workspace_scope import _Session

    raw = SimpleNamespace(**{method: lambda: None})
    session = _Session(raw, namespace('a'))
    with pytest.raises(AttributeError):
        getattr(session, method)
