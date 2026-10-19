"""Real Neo4j private workspace partition regressions; disposable DB only."""
import os

import pytest
from neo4j import AsyncGraphDatabase

from src.graph.workspace_scope import WorkspaceNeo4jClient
from src.ingestion.chunks import plan_chunks

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('M4_DISPOSABLE_NEO4J') != '1', reason='Requires M4_DISPOSABLE_NEO4J=1')]


@pytest.fixture
async def partitions():
    args = (os.environ['NEO4J_URI'], os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD'])
    clients = [WorkspaceNeo4jClient(*args, workspace_id=key) for key in ['m4_a', 'm4_b']]
    raw = AsyncGraphDatabase.driver(args[0], auth=(args[1], args[2]))

    async def cleanup():
        async with raw.session() as session:
            for client in clients:
                for label in ['Entity', 'Source', 'EvidenceChunk', 'GraphState', 'IngestOperation']:
                    await session.run(f'MATCH (n:{client.namespace}{label}) DETACH DELETE n')
    await cleanup()
    for client in clients:
        await client.connect()
        await client.init_schema()
    try:
        yield (*clients, raw)
    finally:
        await cleanup()
        for client in clients:
            await client.close()
        await raw.close()


async def publish(client, content):
    chunks = plan_chunks(content, 's', 'v1')
    nodes = [{'id': key, 'label': key, 'type': 'Thing',
              'properties': {'summary': content}, 'evidence_ids': [chunks[0]['id']]}
             for key in ['n', 'm', 'duplicate']]
    return await client.publish_source_graph(
        {'nodes': nodes, 'edges': [{'source': 'n', 'target': 'm', 'type': 'LINK'}],
         'chunks': chunks}, source={'id': 's', 'version': 'v1', 'title': content},
        expected_version=None, operation_id='same-job')


async def test_same_ids_read_export_retrieval_receipt_merge_delete_clear_are_isolated(partitions):
    a, b, _ = partitions
    await publish(a, 'Apricot evidence')
    await publish(b, 'Blueberry evidence')
    assert (await a.get_source('s'))['title'] == 'Apricot evidence'
    assert (await b.get_source('s'))['title'] == 'Blueberry evidence'
    assert (await a.search_evidence('Apricot'))[0]['text'] == 'Apricot evidence'
    assert await a.search_evidence('Blueberry') == []
    bchunk = (await b.get_source_evidence('s'))[0]['id']
    assert await a.get_evidence(bchunk) is None
    assert (await a.get_ingest_operation('same-job'))['source'] == 's'
    assert 'Blueberry' not in str(await a.get_all_graph_data(limit=None))
    assert 'Apricot' not in str(await b.get_all_graph_data(limit=None))
    await a.merge_nodes(['n', 'duplicate'], canonical_id='n')
    assert len((await a.get_all_graph_data())['nodes']) == 2
    assert len((await b.get_all_graph_data())['nodes']) == 3
    await a.delete_source('s')
    assert (await a.get_all_graph_data())['nodes'] == []
    assert len((await b.get_all_graph_data())['nodes']) == 3
    await a.clear_graph()
    assert (await b.get_evidence(bchunk))['text'] == 'Blueberry evidence'
    assert await b.get_ingest_operation('same-job')


async def test_untyped_paths_analytics_and_context_ignore_cross_partition_links(partitions):
    a, b, raw = partitions
    await publish(a, 'Apricot evidence')
    await publish(b, 'Blueberry evidence')
    # Privileged operator injects edges carrying A's relationship type but B's
    # node label. Every endpoint and intermediate path node must still stay A.
    async with raw.session() as session:
        await session.run(f'MATCH (a:{a.namespace}Entity {{id:"n"}}), '
                          f'(b:{b.namespace}Entity {{id:"n"}}), '
                          f'(c:{a.namespace}Entity {{id:"duplicate"}}) '
                          f'CREATE (a)-[:{a.namespace}RELATES_TO]->(b) '
                          f'CREATE (b)-[:{a.namespace}RELATES_TO]->(c)')
    assert (await a.get_stats())['edges'] == 1
    assert (await a.get_all_graph_data())['total_edges'] == 1
    assert 'Blueberry' not in str(await a.get_entity_context('n'))
    assert 'Blueberry' not in await a.get_node_context('n')
    assert await a.find_path('n', 'duplicate') == []
    analytics = await a.get_analytics()
    assert analytics['isolated_nodes'] == 1
    assert (await b.get_stats())['edges'] == 1


async def test_private_schema_does_not_migrate_or_expose_legacy_and_chunk_links(partitions):
    a, b, raw = partitions
    await publish(a, 'Apricot evidence')
    await publish(b, 'Blueberry evidence')
    async with raw.session() as session:
        await session.run('CREATE (:Entity {id:"legacy-m4", label:"Legacy secret", '
                          'source_doc:"legacy-source"})')
        await session.run(f'MATCH (s:{a.namespace}Source {{id:"s"}}), '
                          f'(c:{b.namespace}EvidenceChunk) '
                          f'CREATE (s)-[:{a.namespace}HAS_CHUNK]->(c)')
    try:
        await a.init_schema()
        assert 'Blueberry' not in str(await a.get_source_evidence('s'))
        assert 'Legacy secret' not in str(await a.get_all_graph_data(limit=None))
        async with raw.session() as session:
            record = await (await session.run(
                'MATCH (n:Entity {id:"legacy-m4"}) RETURN n.source_docs AS docs'
            )).single()
            assert record['docs'] is None
    finally:
        async with raw.session() as session:
            await session.run('MATCH (n:Entity {id:"legacy-m4"}) DETACH DELETE n')
