"""RRF, source filtering and bounded discovery behavior without providers."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.graph import graphrag
from src.graph.graphrag import GraphRAGEngine
from src.graph.neo4j_client import EvidenceResults, Neo4jClient
from src.graph.workspace_scope import namespace, scope_query


def chunk(key, source='s'):
    text = 'Grounded fact ' + key
    return {'id': key, 'source_id': source, 'version': 'v1', 'ordinal': 0,
            'start': 0, 'end': len(text), 'text': text, 'title': source, 'uri': ''}


async def test_safe_lucene_query_and_rrf_fuse_independent_candidate_ranks():
    db = Neo4jClient('', '', '')
    db._read_evidence = AsyncMock(side_effect=[
        [chunk('lexical'), chunk('both')], [chunk('both'), chunk('entity')],
    ])
    hits = await db.search_evidence('battery OR +* [] : "running" \\ secret', ['n'])
    assert [hit['id'] for hit in hits] == ['both', 'lexical', 'entity']
    assert hits[0]['retrieval']['rrf_score'] == 1/62 + 1/61
    options = db._read_evidence.await_args_list[0].kwargs
    assert options['lucene'] == '"battery" OR "running" OR "secret"'
    assert hits.diagnostics == {'method': 'fulltext_rrf', 'fallback': False, 'rrf_k': 60,
                                'candidate_count': 3, 'lexical_candidates': 2,
                                'entity_candidates': 2}


async def test_fulltext_failure_reports_fallback_and_keeps_source_filter_in_queries():
    db = Neo4jClient('', '', '')
    db._read_evidence = AsyncMock(side_effect=[RuntimeError('index unavailable'), [chunk('x')]])
    hits = await db.search_evidence('battery', source_id='s')
    assert hits.diagnostics['fallback'] is True
    assert hits.diagnostics['method'] == 'lexical_fallback'
    for call in db._read_evidence.await_args_list:
        assert call.kwargs['source_id'] == 's'
        assert '($source_id IS NULL OR s.id = $source_id)' in call.args[0]
        assert call.args[0].index('s.id = $source_id') < call.args[0].index('LIMIT 40')


async def test_source_specific_provider_input_omits_other_chunks_and_global_summaries(monkeypatch):
    allowed, forbidden = chunk('allowed', 's'), chunk('forbidden', 'other')
    db = SimpleNamespace(search_evidence=AsyncMock(return_value=EvidenceResults(
        [forbidden, allowed], diagnostics={'method': 'fulltext_rrf'})),
        validate_evidence_ids=AsyncMock(return_value=[allowed]),
        search_nodes=AsyncMock(), vector_search=AsyncMock(), get_entity_context=AsyncMock())
    provider = AsyncMock(return_value={'answer': 'Grounded', 'citations': ['allowed'],
                                      'insufficient_evidence': False, 'conflicts': []})
    monkeypatch.setattr(graphrag, 'answer_with_citations', provider)
    nosana = SimpleNamespace(get_embedding_result=AsyncMock())
    result = await GraphRAGEngine(db, nosana).query('What fact?', source_id='s')
    assert result['status'] == 'answered'
    assert provider.await_args.args[1] == [allowed]
    assert result['source_docs'] == ['s']
    assert result['evidence'] == []
    nosana.get_embedding_result.assert_not_awaited()
    db.get_entity_context.assert_not_awaited()
    db.search_nodes.assert_not_awaited()
    db.search_evidence.assert_awaited_once_with('What fact?', entity_ids=[], limit=6, source_id='s')


def test_fulltext_index_schema_and_procedure_literals_are_both_namespaced():
    prefix = namespace('a')
    ddl = scope_query('CREATE FULLTEXT INDEX evidence_fulltext IF NOT EXISTS '
                      'FOR (c:EvidenceChunk) ON EACH [c.text]', prefix)
    read = scope_query("CALL db.index.fulltext.queryNodes('evidence_fulltext', $q)", prefix)
    assert prefix+'evidence_fulltext' in ddl
    assert "'"+prefix+"evidence_fulltext'" in read
