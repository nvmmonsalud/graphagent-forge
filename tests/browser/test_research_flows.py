"""M6 interaction regressions using invented responses, never a live backend."""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

JOB = {'id': 'job-sample', 'kind': 'url', 'status': 'running', 'stage': 'extracting',
       'params': {'url': 'https://example.invalid/sample'}}
CITATION = {'id': 'evidence-sample', 'source_id': 'source-1', 'version': 'v1',
            'title': 'Invented <source>', 'start': 0, 'end': 26}


def boot(frontend, responses=None):
    app = frontend(responses=responses)
    app.open()
    app.ready()
    return app, app.page


def submit_url(page):
    page.locator('#ingest-url-input').fill('https://example.invalid/sample')
    page.locator('#btn-ingest-url').click()
    expect(page.locator('#ingest-result')).to_contain_text('Queued')


def assert_ingest_ready(page):
    for kind in ('url', 'text', 'file'):
        expect(page.locator(f'#btn-ingest-{kind}')).to_be_enabled()
    assert page.evaluate('activeIngestJob === null && jobPollInterval === null')


def test_ingest_socket_progress_cancel_and_late_event(frontend):
    app, page = boot(frontend, {
        ('POST', '/api/ingest/url'): (200, {'job_id': JOB['id'], 'cancel_supported': True}),
        ('POST', '/api/jobs/job-sample/cancel'): (200, {**JOB, 'status': 'cancelled'}),
    })
    submit_url(page)
    for kind in ('url', 'text', 'file'):
        expect(page.locator(f'#btn-ingest-{kind}')).to_be_disabled()
    # Unrelated jobs must never hijack the active ingest panel.
    app.emit({'type': 'job_update', 'job': {**JOB, 'id': 'other-job', 'stage': 'unrelated'}})
    app.emit({'type': 'job_update', 'job': JOB})
    expect(page.locator('#ingest-result')).to_contain_text('Running — extracting')
    expect(page.locator('#ingest-result')).not_to_contain_text('unrelated')
    page.locator('[data-cancel-job]').click()
    expect(page.locator('#ingest-result')).to_contain_text(
        'publication completed before cancellation')
    assert_ingest_ready(page)
    app.emit({'type': 'job_update', 'job': {**JOB, 'status': 'succeeded',
                                          'result': {'success': True, 'nodes': 9, 'edges': 0}}})
    page.evaluate('() => new Promise(requestAnimationFrame)')
    expect(page.locator('#ingest-result')).to_contain_text('Cancelled')
    assert len([p for method, p, _ in app.requests
                if method == 'POST' and p == '/api/ingest/url']) == 1


def test_durable_reload_resumes_then_recovers_when_job_is_evicted(frontend):
    app, page = boot(frontend, {
        ('GET', '/api/jobs'): (200, {'durable': True, 'jobs': [JOB]}),
        ('GET', '/api/jobs/job-sample'): (404, {'detail': 'Job not found'}),
    })
    expect(page.locator('#ingest-result')).to_contain_text('Running — extracting')
    page.reload(wait_until='domcontentloaded')
    app.ready()
    expect(page.locator('#btn-ingest-url')).to_be_disabled()
    expect(page.locator('#ingest-result')).to_contain_text('no longer retained', timeout=6000)
    assert_ingest_ready(page)
    assert not any(method == 'POST' for method, _, _ in app.requests)


def test_ingest_success_reports_coverage_and_business_failure_releases_controls(frontend):
    app, page = boot(frontend, {
        ('POST', '/api/ingest/url'): (200, {'job_id': JOB['id']}),
    })
    submit_url(page)
    app.emit({'type': 'job_update', 'job': {**JOB, 'status': 'succeeded', 'result': {
        'success': True, 'nodes': 2, 'edges': 1,
        'coverage': {'partial': True, 'processed_chars': 8000, 'skipped_chars': 4000,
                     'input_truncated': True, 'graph_capped': True},
    }}})
    expect(page.locator('#ingest-result')).to_contain_text('Ingested')
    expect(page.locator('#ingest-result')).to_contain_text(
        '8000 characters processed; 4000 skipped')
    expect(page.locator('#ingest-result')).to_contain_text('Input truncated')
    expect(page.locator('#ingest-result')).to_contain_text('Graph limit reached')
    assert_ingest_ready(page)
    submit_url(page)
    app.emit({'type': 'job_update', 'job': {**JOB, 'status': 'succeeded',
                                          'result': {'success': False,
                                                     'error': 'KIMI_API_KEY not configured'}}})
    expect(page.locator('#ingest-result')).to_contain_text('KIMI_API_KEY not configured')
    assert_ingest_ready(page)


def test_cited_answer_refetches_current_excerpt_and_handles_stale_evidence(frontend):
    excerpt = '<img src=x onerror="window.injected=true"> Invented excerpt'
    app, page = boot(frontend, {
        ('POST', '/api/ask'): (200, {'status': 'answered', 'answer': 'Invented <answer>',
                                   'citations': [CITATION]}),
        ('GET', '/api/evidence/evidence-sample'): (200, {**CITATION, 'text': excerpt,
                                                       'uri': 'https://example.invalid/sample'}),
    })
    page.locator('#query-input').fill('What does the invented source say?')
    page.locator('#btn-query').click()
    expect(page.locator('#query-result')).to_contain_text('Invented <answer>')
    page.locator('[data-evidence-id=evidence-sample]').click()
    expect(page.locator('#evidence-detail')).to_contain_text(excerpt)
    expect(page.locator('#evidence-detail')).to_contain_text('Version: v1')
    expect(page.locator('#evidence-detail')).to_contain_text('Normalized text offsets: 0–26')
    assert page.locator('#query-result img').count() == 0
    assert page.evaluate('window.injected === undefined')
    app.responses[('GET', '/api/evidence/evidence-sample')] = (404, {'detail': 'Not found'})
    page.locator('[data-evidence-id=evidence-sample]').click()
    expect(page.locator('#evidence-detail')).to_have_text(
        'This evidence was deleted or replaced. Re-run the question.')
    assert page.locator('#evidence-detail pre').count() == 0
    assert len([p for _, p, _ in app.requests if p == '/api/evidence/evidence-sample']) == 2


@pytest.mark.parametrize(('status', 'payload', 'message'), [
    (200, {'status': 'llm_unavailable', 'answer': 'Never display this answer',
           'context_nodes': ['Invented context']}, 'No LLM configured'),
    (200, {'status': 'insufficient_evidence', 'answer': 'Never display this answer'},
     'Insufficient current original evidence'),
    (503, {'detail': 'Graph database unavailable'}, 'Graph database unavailable'),
])
def test_answer_failure_states_recover_for_retry(frontend, status, payload, message):
    app, page = boot(frontend, {('POST', '/api/ask'): (status, payload)})
    page.locator('#query-input').fill('Invented question')
    page.locator('#btn-query').click()
    expect(page.locator('#query-result')).to_contain_text(message)
    expect(page.locator('#query-result')).not_to_contain_text('Never display this answer')
    expect(page.locator('#btn-query')).to_be_enabled()
    app.responses[('POST', '/api/ask')] = (200, {'status': 'answered', 'answer': 'Retry succeeded'})
    page.locator('#btn-query').click()
    expect(page.locator('#query-result')).to_contain_text('Retry succeeded')
    expect(page.locator('#btn-query')).to_be_enabled()


def test_merge_failure_retry_refetches_graph_without_socket_delivery(frontend):
    nodes = [{'id': node_id, 'label': 'Invented duplicate', 'type': 'Concept',
              'source_docs': [f'source-{index}']} for index, node_id in enumerate(('n1', 'n2'))]
    app, page = boot(frontend, {
        ('GET', '/api/graph/data'): (200, {'nodes': nodes, 'edges': []}),
        ('GET', '/api/graph/duplicates'): (200, {'groups': [
            {'tier': 1, 'norm_label': 'invented duplicate', 'nodes': nodes}]}),
        ('POST', '/api/graph/merge'): (409, {'detail': 'Merge temporarily unavailable'}),
    })
    expect(page.locator('#force-graph .nodes circle')).to_have_count(2)
    merge = page.locator('#duplicates-list [data-action=merge]')
    merge.click()
    page.locator('#modal-confirm').click()
    expect(page.locator('#toast-stack')).to_contain_text('Merge temporarily unavailable')
    expect(merge).to_be_enabled()
    expect(page.locator('#force-graph .nodes circle')).to_have_count(2)
    app.responses[('POST', '/api/graph/merge')] = (200, {'merged': True})
    app.responses[('GET', '/api/graph/data')] = (200, {'nodes': [nodes[0]], 'edges': []})
    app.responses[('GET', '/api/graph/duplicates')] = (200, {'groups': []})
    merge.click()
    page.locator('#modal-confirm').click()
    expect(page.locator('#force-graph .nodes circle')).to_have_count(1)
    expect(page.locator('#duplicates-list [data-action=merge]')).to_have_count(0)
    merges = [json.loads(body) for method, path, body in app.bodies
              if method == 'POST' and path == '/api/graph/merge']
    assert merges == [{'node_ids': ['n1', 'n2']}] * 2
