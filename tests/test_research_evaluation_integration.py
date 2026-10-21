"""Opt-in actual Neo4j measurement; expected sources precede the first run."""
import json
import os
from pathlib import Path

import pytest

from scripts.research_evaluation import score
from src.graph.workspace_scope import WorkspaceNeo4jClient
from src.ingestion.chunks import plan_chunks

CORPUS = json.loads((Path(__file__).parent / 'fixtures/research_evaluation.json').read_text())
pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('RESEARCH_DISPOSABLE_NEO4J') != '1',
    reason='Requires RESEARCH_DISPOSABLE_NEO4J=1 against a disposable Neo4j')]


async def test_expanded_corpus_actual_retrieval_measurement():
    client = WorkspaceNeo4jClient(os.environ['NEO4J_URI'], os.environ['NEO4J_USER'],
                                  os.environ['NEO4J_PASSWORD'], 'research_evaluation')
    documents = {s['id']: 'x' * s.get('prefix_chars', 0) + '\n' + s['text']
                 for s in CORPUS['sources']}
    await client.connect()
    try:
        await client.init_schema()
        await client.clear_graph()
        for source in CORPUS['sources']:
            sid = source['id']
            prior = await client.get_source(sid)
            await client.publish_source_graph(
                {'nodes': [], 'edges': [], 'chunks': plan_chunks(documents[sid], sid, 'v1')},
                source={'id': sid, 'version': 'v1', 'title': source['title']},
                expected_version=prior['version'] if prior else None,
            )
        rankings = {}
        for case in CORPUS['cases']:
            hits = await client.search_evidence(case['question'], entity_ids=[], limit=3,
                                                source_id=case.get('source_id'))
            assert hits.diagnostics['method'] == 'fulltext_rrf'
            assert len(hits) <= 3
            for hit in hits:
                assert documents[hit['source_id']][hit['start']:hit['end']] == hit['text']
            rankings[case['id']] = [hit['source_id'] for hit in hits]
        # Expanded corpus has no post-hoc aggregate pass threshold. Record its
        # first real result before agreeing any future quality release gate.
        print(json.dumps({'corpus_version': CORPUS['version'],
                          'retrieval': 'Neo4j fulltext_rrf; no embeddings or LLM',
                          **score(CORPUS, rankings)}, ensure_ascii=False, indent=2))
    finally:
        try:
            await client.clear_graph()
        finally:
            await client.close()
