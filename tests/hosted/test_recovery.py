"""Real process/state recovery boundaries; no remote provider or hosting claims."""
import json
import sqlite3
import time

import httpx
import pytest
from httpx_ws import WebSocketDisconnect, connect_ws

from hosted.test_connected import DOCUMENT, QUESTION, ingest

pytestmark = pytest.mark.hosted


def test_restart_preserves_graph_saved_history_jobs_and_shared_quota(hosted):
    with hosted.client() as client:
        job = ingest(hosted, client)
        source = job['result']['source']
        response = client.post('/api/ask', json={'question': QUESTION, 'source_id': source})
        assert response.json()['status'] == 'answered'
        history_id = client.get('/api/history').json()['entries'][0]['id']
        assert client.post(f'/api/history/{history_id}/save').status_code == 200
        # Sandbox admission and Ask consume the SAME durable workspace allowance.
        assert client.get('/api/graph/analytics').status_code == 200
        for _ in range(6):
            assert client.post('/api/ask', json={
                'question': QUESTION, 'source_id': source}).json()['status'] == 'answered'
        usage = client.get('/api/usage').json()
        assert usage['ask_requests'] == 7 and usage['sandbox_requests'] == 1
        assert usage['reported_calls'] == 7
        before = dict(hosted.provider.calls)
        assert client.post('/api/ask', json={'question': QUESTION}).status_code == 429
        assert hosted.provider.calls == before
        graph = client.get('/api/graph/data').json()
    hosted.restart()
    with hosted.client() as client, hosted.client('bob', 'beta') as beta:
        assert client.get('/api/graph/data').json() == graph
        assert client.get('/api/jobs/' + job['id']).json()['status'] == 'succeeded'
        retained = client.get('/api/history').json()['entries']
        assert any(entry['id'] == history_id and entry['saved'] for entry in retained)
        assert client.get('/api/usage').json()['ask_requests'] == 7
        assert client.post('/api/ask', json={'question': QUESTION}).status_code == 429
        assert hosted.provider.calls == before
        assert beta.get('/api/usage').json()['ask_requests'] == 0
        assert beta.post('/api/ask', json={'question': QUESTION}).json()['status'] == 'no_context'
        assert beta.get('/api/usage').json()['ask_requests'] == 1


def test_cancel_paused_execution_removes_private_command_and_publishes_nothing(hosted):
    hosted.provider.mode = 'pause'
    with hosted.client() as client:
        submitted = client.post('/api/ingest/text', json={'text': DOCUMENT, 'source': 'cancelled'})
        assert submitted.status_code == 202
        job_id = submitted.json()['job_id']
        assert hosted.provider.entered.wait(10)
        assert client.post(f'/api/jobs/{job_id}/cancel').status_code == 200
        terminal = hosted.wait_job(client, job_id)
        assert terminal['status'] == 'cancelled'
        with sqlite3.connect(hosted.state / 'workspaces/alpha/state.sqlite3') as connection:
            assert connection.execute('SELECT command FROM durable_jobs WHERE id=?',
                                      (job_id,)).fetchone() == (None,)
        hosted.provider.release.set()
        assert client.get('/api/graph/data').json()['nodes'] == []
        assert client.get('/api/sources').json()['sources'] == []
        assert 'orchid-acceptance-739' not in json.dumps(terminal)


def test_queued_submitter_and_live_socket_revocation_fence_publication(hosted):
    hosted.provider.mode = 'pause'
    with httpx.Client(base_url=hosted.origin, trust_env=False,
                      headers={'Origin': hosted.origin}, timeout=30) as cookie:
        hosted.login(cookie)
        with connect_ws(hosted.origin + '/ws/graph', client=cookie,
                        headers={'Origin': hosted.origin}) as socket:
            with hosted.client() as writer, hosted.client('reader') as observer:
                jobs = [writer.post('/api/ingest/text', json={
                    'text': DOCUMENT, 'source': f'revoked-{i}'}).json()['job_id'] for i in range(3)]
                deadline = time.monotonic() + 10
                while hosted.provider.calls['extraction'] < 2 and time.monotonic() < deadline:
                    time.sleep(.05)
                assert hosted.provider.calls['extraction'] == 2
                assert writer.get('/api/jobs/' + jobs[2]).json()['status'] == 'queued'
                hosted.revoke('alice', 'alpha')
                assert writer.get('/api/graph/data').status_code == 403
                # Drain pre-revocation telemetry before requiring close. There must
                # be no graph payload after revocation and the ping forces a recheck.
                socket.send_json({'type': 'ping'})
                with pytest.raises(WebSocketDisconnect):
                    for _ in range(30):
                        message = socket.receive_json(timeout=10)
                        assert message.get('type') != 'graph_update'
                hosted.provider.release.set()
                for job_id in jobs:
                    assert hosted.wait_job(observer, job_id)['status'] == 'cancelled'
                assert hosted.provider.calls['extraction'] == 2
                assert observer.get('/api/graph/data').json()['nodes'] == []


def test_missing_provider_is_explicit_and_graph_remains_available(hosted):
    with hosted.client() as client:
        source = ingest(hosted, client)['result']['source']
    before = dict(hosted.provider.calls)
    hosted.restart(provider_enabled=False)
    with hosted.client() as client:
        answer = client.post('/api/ask', json={'question': QUESTION, 'source_id': source})
        assert answer.status_code == 200
        assert answer.json()['status'] == 'llm_unavailable'
        assert answer.json()['citations'] == []
        assert client.get('/api/graph/data').json()['nodes']
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/api/auth/status').json()['private'] is True
    assert hosted.provider.calls == before


def test_real_app_external_fetch_is_denied_without_dns_or_provider_access(hosted):
    before = dict(hosted.provider.calls)
    denied_before = hosted.network_counts()['denied']
    with hosted.client() as client:
        response = client.post('/api/ingest/url', json={'url': 'https://provider.invalid/note'})
        assert response.status_code == 202
        terminal = hosted.wait_job(client, response.json()['job_id'])
        if terminal['status'] == 'failed':
            assert terminal['error'] == 'internal error'
            assert terminal['result'] is None
        else:
            assert terminal['status'] == 'succeeded'
            assert terminal['result']['success'] is False
        assert hosted.network_counts()['denied'] > denied_before
        assert hosted.provider.calls == before
        assert client.get('/api/graph/data').json()['nodes'] == []
