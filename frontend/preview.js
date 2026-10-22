// Standalone display fixture. No API, provider, database, or browser storage is used.
(() => {
  'use strict';
  const unavailable = {detail: 'Static preview only. Backend actions are unavailable.'};
  const graph = {
    nodes: [
      {id: 'preview-project', label: 'GraphAgent Forge', type: 'Project',
        summary: 'Synthetic display node', source_docs: ['preview-source']},
      {id: 'preview-research', label: 'Research workspace', type: 'Concept',
        summary: 'Synthetic display node', source_docs: ['preview-source']}
    ],
    edges: [{source: 'preview-project', target: 'preview-research',
      type: 'EXPLORES', source_doc: 'preview-source'}]
  };
  const responses = {
    '/api/auth/status': {private: false, preview: true},
    '/api/graph/stats': {nodes: 2, edges: 1, entity_types: ['Project', 'Concept']},
    '/api/graph/data': {...graph, truncated: false, total_nodes: 2, total_edges: 1, limit: 500},
    '/api/sources': {sources: [{source_doc: 'preview-source', title: 'Synthetic preview', node_count: 2}]},
    '/api/graph/duplicates': {groups: []},
    '/api/history': {entries: []},
    '/api/jobs': {jobs: [], durable: false},
    '/api/research/batches': {batches: []},
    '/api/research/refreshes': {refreshes: []},
    '/api/research/investigations': {investigations: []},
    '/api/research/browse': {...graph, next_cursor: null}
  };
  window.fetch = async (input, options = {}) => {
    const request = new Request(input instanceof Request ? input : new URL(input, location.href), options);
    const url = new URL(request.url);
    const allowed = url.origin === location.origin && request.method === 'GET'
      && Object.hasOwn(responses, url.pathname);
    return new Response(JSON.stringify(allowed ? responses[url.pathname] : unavailable), {
      status: allowed ? 200 : 503, headers: {'Content-Type': 'application/json'}
    });
  };
  window.WebSocket = class {
    readyState = 3;
    send() {}
    close() {}
  };
  // Do not read existing credentials/history or persist data on this preview origin.
  const storage = {getItem: () => null, setItem() {}, removeItem() {}, clear() {}, key: () => null, length: 0};
  for (const name of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(window, name, {value: storage});
  }
})();
