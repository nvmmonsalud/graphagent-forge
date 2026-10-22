"""Real Chromium, real frontend assets, synthetic transport; never start the API."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = 'http://127.0.0.1:8765'


@pytest.fixture(scope='session')
def browser():
    # Import only when explicitly selecting browser tests. Missing tooling must fail
    # that job, rather than silently turn executable coverage into skips.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        executable = os.getenv('BROWSER_EXECUTABLE')
        instance = pw.chromium.launch(**({'executable_path': executable} if executable else {}))
        yield instance
        instance.close()


class Frontend:
    def __init__(self, page, mode='local', hold=None, failure=None, responses=None):
        self.page = page
        self.mode = mode
        self.hold = hold
        self.failure = failure
        self.responses = responses or {}
        self.pending = {}
        self.socket_routes = []
        self.bodies = []
        self.requests = []
        self.unexpected = []
        self.errors = []
        self.sockets = []
        self.workspace = 'alpha'
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.route('**/*', self.route)
        page.route_web_socket('**/*', self.socket)

    def socket(self, socket):
        self.sockets.append(socket.url)
        self.socket_routes.append(socket)
        if socket.url != ORIGIN.replace('http:', 'ws:') + '/ws/graph':
            self.unexpected.append(socket.url)
        # No connect_to_server: no network WebSocket or provider can be reached.
        socket.on_message(lambda message: None)

    def identity(self):
        workspaces = [dict(id=name, title=name.title(), role=self.mode)
                      for name in ('alpha', 'beta')]
        return {'workspaces': workspaces,
                'workspace': next(w for w in workspaces if w['id'] == self.workspace)}

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        if url.netloc != '127.0.0.1:8765':
            # Google Fonts are decorative; block them without network access.
            if url.hostname not in ('fonts.googleapis.com', 'fonts.gstatic.com'):
                self.unexpected.append(request.url)
            route.abort()
            return
        if path in ('/', '/vendor/d3.v7.min.js'):
            asset = 'index.html' if path == '/' else path.lstrip('/')
            route.fulfill(path=str(ROOT / 'frontend' / asset),
                          content_type='text/html' if path == '/' else 'application/javascript')
            return
        self.requests.append((request.method, path, request.headers.get('x-workspace-id')))
        self.bodies.append((request.method, path, request.post_data))
        response = self.responses.get((request.method, path))
        if response is not None:
            status, payload = response
            route.fulfill(status=status, json=payload)
            return
        if path == self.hold:
            self.pending[path] = route
            return
        if path == '/api/auth/status':
            if self.failure == 'network':
                route.abort('failed')
            else:
                route.fulfill(status=503 if self.failure else 200,
                              json={'private': self.mode != 'local'})
            return
        if path in ('/api/auth/workspaces', '/api/auth/session', '/api/auth/workspace'):
            if path.endswith('/workspace'):
                self.workspace = request.post_data_json['workspace_id']
            route.fulfill(json=self.identity())
            return
        payloads = {
            '/api/graph/stats': {'nodes': 0, 'edges': 0, 'entity_types': []},
            '/api/graph/data': {'nodes': [{'id': 'n1', 'label': 'Synthetic node',
                                            'type': 'Concept', 'summary': 'Synthetic summary',
                                            'source_docs': ['source-1']}], 'edges': []},
            '/api/sources': {'sources': [{'source_doc': 'source-1',
                                          'title': 'Synthetic source', 'node_count': 0}]},
            '/api/graph/duplicates': {'groups': [{'tier': 1, 'norm_label': 'synthetic',
                                                 'nodes': [{'id': 'n1', 'label': 'Synthetic',
                                                            'type': 'Concept'}]}]},
            '/api/history': {'entries': [{'id': 'history-1', 'question': 'Synthetic question',
                                         'status': 'no_context', 'saved': False}]},
            '/api/jobs': {'jobs': [], 'durable': False},
            '/api/ask': {'status': 'answered', 'answer': 'Synthetic answer', 'context_nodes': []},
            '/api/graph/analytics': {'totals': {}, 'structure': {'ok': False,
                                                              'error': 'Synthetic unavailable'}},
            '/api/research/bulk': {'id': 'batch-1', 'items': []},
            '/api/research/batches': {'batches': [{'id': 'batch-1', 'items': []}]},
            '/api/research/refreshes': {'refreshes': [{'id': 'refresh-1',
                                                      'display_uri': 'https://example.invalid/'}]},
            '/api/research/investigations': {'investigations': [{'id': 'view-1',
                                                               'title': 'Synthetic view'}]},
            '/api/research/investigations/view-1': {
                'id': 'view-1', 'title': 'Synthetic view', 'query': 'Restored synthetic query',
                'selected_entity_ids': [], 'annotations': [], 'revision': 1},
            '/api/research/compare': {'contexts': [], 'missing_ids': ['synthetic-node']},
            '/api/research/browse': {'nodes': [], 'edges': [], 'next_cursor': None},
        }
        post_paths = {'/api/ask', '/api/research/compare', '/api/research/bulk'}
        expected_method = 'POST' if path in post_paths else 'GET'
        if path not in payloads or request.method != expected_method:
            self.unexpected.append((request.method, path))
            route.fulfill(status=500, json={'detail': 'Unexpected test request'})
        else:
            route.fulfill(json=payloads[path])

    def emit(self, payload):
        import json

        self.socket_routes[-1].send(json.dumps(payload))

    def open(self):
        self.page.goto(ORIGIN, wait_until='domcontentloaded')

    def ready(self):
        self.page.wait_for_function('authReady')
        self.page.locator('#query-history[data-loaded="1"]').wait_for(state='attached')
        self.page.locator('#sources-list[data-loaded="1"]').wait_for(state='attached')
        self.page.locator('#duplicates-list[data-loaded="1"]').wait_for(state='attached')
        self.page.locator('#force-graph .nodes circle').first.wait_for(state='attached')

    def release(self, path, payload, status=200):
        self.page.wait_for_function('true')  # pump pending Playwright route callbacks
        self.pending.pop(path).fulfill(status=status, json=payload)
        self.hold = None


@pytest.fixture
def frontend(browser):
    instances = []
    contexts = []

    def create(**kwargs):
        context = browser.new_context(viewport={'width': 1440, 'height': 1000},
                                      reduced_motion='reduce', service_workers='block')
        contexts.append(context)
        instance = Frontend(context.new_page(), **kwargs)
        instances.append(instance)
        return instance

    yield create
    for context in contexts:
        context.close()
    for instance in instances:
        assert not instance.unexpected, instance.unexpected
        assert not instance.errors, instance.errors
