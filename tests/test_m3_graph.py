"""Private operation receipts share publication transactions and preserve legacy calls."""
from unittest.mock import AsyncMock

import pytest

from tests.test_m1_graph import mocked_client


@pytest.mark.parametrize('operation_id', ['', None, 'x' * 129, 'has space', 'unicode🙂'])
async def test_private_lookup_rejects_invalid_ids(operation_id):
    db, session, _ = mocked_client()
    with pytest.raises(ValueError):
        await db.get_ingest_operation(operation_id)
    session.run.assert_not_awaited()


async def test_receipt_replay_precedes_source_cas_or_mutation():
    db, _, tx = mocked_client()
    db._read_ingest_operation = AsyncMock(return_value={
        'success': True, 'source': 'old', 'version': 'v1', 'nodes': 3, 'edges': 2,
        'unchanged': True})
    db._remove_source_graph = AsyncMock()
    result = await db.publish_source_graph({}, source={'id': 'new', 'version': 'v2'},
                                          expected_version='stale', operation_id='job-1')
    assert result == dict(nodes_written=3, edges_written=2, source='old', version='v1',
                          unchanged=True, conflict=False)
    assert all('Source' not in call.args[0] for call in tx.run.call_args_list)
    db._remove_source_graph.assert_not_awaited()
    tx.commit.assert_awaited_once()


async def test_unchanged_publication_creates_receipt_in_same_transaction():
    db, _, tx = mocked_client()
    db._read_ingest_operation = AsyncMock(return_value=None)
    db._write_ingest_operation = AsyncMock()
    result = AsyncMock()
    result.single.return_value = {'version': 'v1'}
    tx.run.return_value = result
    data = {'nodes': [], 'edges': [], 'chunks': []}
    published = await db.publish_source_graph(data, source={
        'id': 's', 'version': 'v1', 'coverage': {'partial': True}},
        expected_version='v1', operation_id='job')
    assert published['unchanged']
    db._write_ingest_operation.assert_awaited_once_with(
        tx, 'job', {'id': 's', 'version': 'v1', 'coverage': {'partial': True}}, 0, 0)
    tx.commit.assert_awaited_once()
    tx.rollback.assert_not_awaited()


async def test_failed_receipt_insert_rolls_back_graph_publication():
    db, _, tx = mocked_client()
    db._read_ingest_operation = AsyncMock(return_value=None)
    db._remove_source_graph = AsyncMock()
    db._write_ingest_operation = AsyncMock(side_effect=RuntimeError('failed insert'))
    result = AsyncMock()
    result.single.return_value = {'version': None}
    tx.run.return_value = result
    with pytest.raises(RuntimeError, match='failed insert'):
        await db.publish_source_graph({}, source={'id': 's', 'version': 'v1'},
                                      expected_version=None, operation_id='job')
    db._remove_source_graph.assert_awaited_once()
    tx.commit.assert_not_awaited()
    tx.rollback.assert_awaited_once()


async def test_conflicting_publication_never_records_receipt():
    db, _, tx = mocked_client()
    db._read_ingest_operation = AsyncMock(return_value=None)
    db._write_ingest_operation = AsyncMock()
    result = AsyncMock()
    result.single.return_value = {'version': 'other'}
    tx.run.return_value = result
    result = await db.publish_source_graph({}, source={'id': 's', 'version': 'v1'},
                                          expected_version=None, operation_id='job')
    assert result['conflict']
    db._write_ingest_operation.assert_not_awaited()
    tx.rollback.assert_awaited_once()
