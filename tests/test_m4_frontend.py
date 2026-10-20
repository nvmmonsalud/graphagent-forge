"""Private frontend boundaries; interaction checks also run in Chromium."""
from pathlib import Path

HTML = (Path(__file__).parents[1] / "frontend/index.html").read_text()
APP = (Path(__file__).parents[1] / "frontend/app.py").read_text()


def test_private_boot_is_gated_before_research_requests():
    assert "let privateMode = true, authReady = false" in HTML
    assert "const status = await authCall('status')" in HTML
    assert "if (!authReady) throw new Error" in HTML
    assert "if (!privateMode) { authReady = true; applyReaderControls(); bootWorkspace();" in HTML
    assert "await apiFetch(API+'/graph/stats')" in HTML


def test_private_credentials_and_history_never_use_browser_storage():
    assert 'id="private-token" type="password" autocomplete="off"' in HTML
    assert "const token = input.value; input.value = '';" in HTML
    assert "function storedApiKey() {\n  if (privateMode) return '';" in HTML
    assert "function readHistoryLocal() {\n  if (privateMode) return [];" in HTML
    assert "function writeHistoryLocal(entries) {\n  if (privateMode) return;" in HTML
    assert "localStorage.removeItem(API_KEY_STORAGE)" in HTML
    assert "localStorage.removeItem(HISTORY_STORAGE_KEY)" in HTML
    assert "res.status === 401 && !privateMode && !retried" in HTML


def test_workspace_transition_aborts_and_fences_pending_responses():
    assert "workspaceAbort.abort(); workspaceAbort = new AbortController();" in HTML
    assert HTML.count("generation !== workspaceGeneration") >= 6
    assert "return discardedWorkspaceResponse()" in HTML
    assert "transition !== authTransition" in HTML
    assert "ws.onclose = null; ws.onmessage = null; ws.close()" in HTML
    assert "if (closeActiveDialog) closeActiveDialog(false)" in HTML
    assert "selectedIngestFile = null; duplicateGroups = []; analyticsData = null" in HTML
    assert "currentPathNodeIds.clear(); currentPathEdgeKeys.clear()" in HTML


def test_reader_disables_mutations_including_compute_gets():
    assert "path.startsWith('/api/graph/verify') || path === '/api/graph/analytics'" in HTML
    assert "This action requires an editor role." in HTML
    assert "[data-action=\"merge\"]" in HTML
    assert "event.stopImmediatePropagation(); applyReaderControls();" in HTML
    assert "'/api/graph/path', '/api/research/compare'].includes(path)" in HTML


def test_streamlit_auth_applies_to_reads_and_search():
    assert 'os.getenv("GRAPHAGENT_TOKEN")' in APP
    assert 'HEADERS = {"Authorization": f"Bearer {token}"}' in APP
    assert 'HEADERS["X-Workspace-ID"] = workspace' in APP
    assert 'timeout=5, headers=HEADERS' in APP
    assert 'json={"query": search_query},\n                    headers=HEADERS,' in APP


def test_auth_cookie_changes_are_serialized_and_provider_origins_are_text_only():
    assert "mutating ? authRequestQueue.then(execute) : execute()" in HTML
    assert "authRequestQueue = pending.catch(() => {})" in HTML
    assert "Object.entries(status.providers || {})" in HTML
    assert "appendChild(document.createTextNode(" in HTML
