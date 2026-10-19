"""Predeclared fixture: English morphology adds recall over substring matching.

Expected hits are fixed before implementing retrieval. This measures four fixture
questions only; it does not measure answer correctness or general research quality.
"""
import os

import pytest

from src.graph.workspace_scope import WorkspaceNeo4jClient
from src.ingestion.chunks import plan_chunks

FIXTURE = [
    ('run', 'Engineers run experiments with lithium.', 'running'),
    ('battery', 'The batteries delivered reliable storage.', 'battery'),
    ('copper', 'Copper conducts electricity.', 'copper'),
    ('mango', 'Mango harvest begins in June.', 'mango'),
]
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('M5_DISPOSABLE_NEO4J') != '1', reason='Requires M5_DISPOSABLE_NEO4J=1')]


@pytest.fixture
async def partitions():
    clients = [WorkspaceNeo4jClient(
        os.environ['NEO4J_URI'], os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD'], key,
    ) for key in ['m5_a', 'm5_b']]
    for client in clients:
        await client.connect()
        await client.init_schema()
        await client.clear_graph()
    try:
        yield clients
    finally:
        for client in clients:
            await client.clear_graph()
            async with client.driver.session() as session:
                await session.run('MATCH (s:Source) DELETE s')
            await client.close()


async def publish(client, sid, text):
    chunks = plan_chunks(text, sid, 'v1')
    await client.publish_source_graph(
        {'nodes': [], 'edges': [], 'chunks': chunks},
        source={'id': sid, 'version': 'v1', 'title': sid}, expected_version=None,
    )


async def test_predeclared_morphology_fixture_recall_at_three(partitions):
    a, _ = partitions
    for sid, text, _ in FIXTURE:
        await publish(a, sid, text)
    old_hits = new_hits = 0
    for expected, text, question in FIXTURE:
        old_hits += question in text.lower()
        hits = await a.search_evidence(question, limit=3)
        assert hits.diagnostics['method'] == 'fulltext_rrf'
        new_hits += expected in [hit['source_id'] for hit in hits]
    assert old_hits == 2
    assert new_hits == 4


async def test_fulltext_same_id_partitions_source_filter_and_index_failure_fallback(partitions):
    a, b = partitions
    await publish(a, 'shared', 'Apricot pilots run experiments.')
    await publish(a, 'other', 'Mango project plans.')
    await publish(b, 'shared', 'Blueberry pilots run experiments.')
    assert 'Blueberry' not in str(await a.search_evidence('running'))
    assert 'Apricot' not in str(await b.search_evidence('running'))
    assert await a.search_evidence('running', source_id='other') == []
    hits = await a.search_evidence('running', source_id='shared')
    assert [hit['source_id'] for hit in hits] == ['shared']
    async with a.driver.session() as session:
        await session.run('DROP INDEX evidence_fulltext IF EXISTS')
    fallback = await a.search_evidence('Apricot', source_id='shared')
    assert fallback.diagnostics['fallback'] is True
    assert fallback.diagnostics['fallback_candidate_limit'] == 1000
    assert [hit['source_id'] for hit in fallback] == ['shared']
    assert (await b.search_evidence('running')).diagnostics['method'] == 'fulltext_rrf'


async def test_progressive_browse_is_ordered_bounded_closed_and_workspace_scoped(partitions):
    a, b = partitions
    nodes = [{'id': f'n{i:03}', 'label': f'Node {i}'} for i in range(205)]
    edges = [{'source': f'n{i:03}', 'target': f'n{j:03}', 'relationship': 'LINK'}
             for i in range(20) for j in range(20) if i != j]
    await a.write_graph({'nodes': nodes, 'edges': edges}, source_doc='a-source')
    await b.write_graph({'nodes': [{'id': 'private', 'label': 'Blueberry secret'}], 'edges': []},
                        source_doc='b-source')
    cursor, collected = None, []
    for expected_size in [100, 100, 5]:
        page = await a.browse_graph(after_id=cursor, limit=1000, source_id='a-source')
        assert len(page['nodes']) == expected_size
        assert len(page['edges']) <= 300
        ids = {node['id'] for node in page['nodes']}
        assert all(edge['source'] in ids and edge['target'] in ids for edge in page['edges'])
        collected.extend(node['id'] for node in page['nodes'])
        cursor = page['next_id']
    assert cursor is None
    assert collected == [node['id'] for node in nodes]
    assert (await a.browse_graph(source_id='b-source'))['nodes'] == []
    assert (await b.browse_graph())['nodes'][0]['id'] == 'private'
