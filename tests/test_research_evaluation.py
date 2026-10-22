"""Metric arithmetic and declared corpus validity, independent of retrieval output."""
import json
from pathlib import Path

import pytest

from scripts.research_evaluation import score

CORPUS = json.loads((Path(__file__).parent / 'fixtures/research_evaluation.json').read_text())


def test_corpus_covers_predeclared_research_risks():
    tags = {tag for case in CORPUS['cases'] for tag in case['tags']}
    assert {'ambiguous-label', 'duplicate-title', 'conflicting-sources', 'unicode',
            'after-50000', 'isolated-evidence', 'morphology', 'source-scope',
            'untrusted-document', 'unsupported'} <= tags
    source_ids = {source['id'] for source in CORPUS['sources']}
    for case in CORPUS['cases']:
        assert set(case['expected_sources']) <= source_ids
        assert case['question']


def test_metrics_use_unique_ranked_sources_and_keep_unsupported_denominator_separate():
    corpus = {'scope': 'arithmetic only', 'sources': [{'id': s} for s in ['a', 'b', 'c']],
              'cases': [{'id': 'positive', 'expected_sources': ['a', 'b']},
                        {'id': 'unsupported', 'expected_sources': []}]}
    result = score(corpus, {'positive': ['c', 'c', 'a', 'b'], 'unsupported': []}, k=2)
    assert result['macro_source_recall'] == .5
    assert result['mean_reciprocal_rank'] == .5
    assert result['unsupported_empty_rate'] == 1
    result = score(corpus, {'positive': [], 'unsupported': ['c']})
    assert result['macro_source_recall'] == 0
    assert result['mean_reciprocal_rank'] == 0
    assert result['unsupported_empty_rate'] == 0


@pytest.mark.parametrize('mutate', [
    lambda rows: rows.pop('unsupported'),
    lambda rows: rows.update(extra=[]),
    lambda rows: rows.update(equal_titles_planet=['unknown']),
    lambda rows: rows.update(source_filter=['element']),
])
def test_incomplete_unknown_and_scope_leaking_rankings_fail(mutate):
    rows = {case['id']: [] for case in CORPUS['cases']}
    mutate(rows)
    with pytest.raises(ValueError):
        score(CORPUS, rows)


def test_source_leak_beyond_scoring_cutoff_is_rejected():
    rows = {case['id']: [] for case in CORPUS['cases']}
    rows['source_filter'] = ['planet', 'element']
    with pytest.raises(ValueError, match='leaked another source'):
        score(CORPUS, rows, k=1)


def test_recorded_baseline_metrics_match_supplied_rankings():
    baseline = json.loads((Path(__file__).parents[1] / 'docs/research_baseline.json').read_text())
    assert baseline['corpus_version'] == CORPUS['version']
    measured = score(CORPUS, baseline['rankings'], k=baseline['k'])
    for metric in ('macro_source_recall', 'mean_reciprocal_rank', 'unsupported_empty_rate'):
        assert measured[metric] == baseline[metric]


@pytest.mark.parametrize('k', [0, -1, 101, True, 1.5])
def test_invalid_cutoff_fails(k):
    with pytest.raises(ValueError):
        score(CORPUS, {}, k=k)
