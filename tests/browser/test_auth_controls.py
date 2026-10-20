"""M6 browser boundary: execute the shipped JS; assert DOM and transport effects."""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser

WRITE_CONTROLS = [
    '#btn-ingest-url', '#btn-ingest-text', '#btn-ingest-file', '#ingest-url-input',
    '#ingest-text-input', '#btn-clear-graph', '#btn-verify', '#btn-fanout', '#btn-sweep',
    '#btn-analytics', '#sources-list [data-action=delete]',
    '#duplicates-list [data-action=merge]', '#query-history [data-action=save]',
    '#query-history [data-action=delete]', '[data-research=bulk]',
    '[data-research=refresh-create]', '[data-research=view-create]', '[data-research=annotate]',
]


def click(page, action):
    page.locator(f'[data-research={action}]').click()


def check_controls(page, enabled):
    for selector in WRITE_CONTROLS:
        control = page.locator(selector)
        expect(control).to_be_enabled() if enabled else expect(control).to_be_disabled()
    for selector in ['#btn-query', '#btn-search', '#btn-path', '[data-research=compare]',
                     '[data-research=views]', '#research-browse-next']:
        expect(page.locator(selector)).to_be_enabled()


@pytest.mark.parametrize('mode', ['local', 'reader', 'editor'])
def test_boot_and_dynamic_role_controls(frontend, mode):
    app = frontend(mode=mode)
    app.open()
    app.ready()
    page = app.page
    check_controls(page, enabled=mode != 'reader')
    for action in ['batches', 'refresh-list', 'views']:
        click(page, action)
    for action in ['batch-cancel', 'refresh-run', 'refresh-delete', 'view-delete']:
        control = page.locator(f'[data-research={action}]')
        expect(control).to_be_enabled() if mode != 'reader' else expect(control).to_be_disabled()
    expect(page.locator('[data-research=view-restore]')).to_be_enabled()
    if mode != 'local':
        expect(page.locator('#private-role')).to_have_text(mode)
        assert all(workspace == 'alpha' for _, path, workspace in app.requests
                   if not path.startswith('/api/auth/'))
    else:
        expect(page.locator('#private-session')).to_be_hidden()
    # Reader-allowed POST and saved-view restore execute through real click handlers.
    click(page, 'view-restore')
    expect(page.locator('#query-input')).to_have_value('Restored synthetic query')
    assert not any(path == '/api/ask' for _, path, _ in app.requests)
    page.locator('#btn-query').click()
    expect(page.locator('#query-result')).to_contain_text('Synthetic answer')
    page.locator('#research-entities').fill('synthetic-node')
    click(page, 'compare')
    expect(page.locator('#research-compare')).to_contain_text('synthetic-node')
    click(page, 'browse')
    expect(page.locator('#research-browse-next')).to_be_disabled()


@pytest.mark.parametrize('mode', ['local', 'editor'])
def test_enabled_controls_dispatch_and_recover_from_busy(frontend, mode):
    app = frontend(mode=mode)
    app.open()
    app.ready()
    page = app.page
    page.locator('#bulk-urls').fill('https://example.invalid/synthetic')
    click(page, 'bulk')
    expect(page.locator('#research-batches')).to_contain_text('batch-1')
    expect(page.locator('[data-research=bulk]')).to_be_enabled()
    page.locator('#btn-analytics').click()
    expect(page.locator('#analytics-list')).to_contain_text('Synthetic unavailable')
    expect(page.locator('#btn-analytics')).to_be_enabled()
    assert ('POST', '/api/research/bulk', 'alpha' if mode == 'editor' else None) in app.requests
    assert any(path == '/api/graph/analytics' for _, path, _ in app.requests)


def test_reader_cannot_dispatch_mutations_even_if_control_is_reenabled(frontend):
    app = frontend(mode='reader')
    app.open()
    app.ready()
    page = app.page
    before = list(app.requests)
    # Exercise capture guard and apiFetch defense as well as disabled attributes.
    page.evaluate("""() => {
        const button = document.querySelector('[data-research=bulk]');
        button.disabled = false; button.click();
    }""")
    expect(page.locator('[data-research=bulk]')).to_be_disabled()
    denied = page.evaluate("""async () => {
        const paths = [
          ['/api/ingest/text','POST'], ['/api/graph/analytics','GET'],
          ['/api/graph/verify','GET'], ['/api/research/refreshes','POST'],
          ['/api/research/investigations','POST'], ['/api/history/history-1','DELETE']
        ];
        return Promise.all(paths.map(async ([url,method]) => {
          try { await apiFetch(url,{method}); return false; }
          catch (error) { return error.message.includes('editor role'); }
        }));
    }""")
    assert all(denied)
    assert app.requests == before


@pytest.mark.parametrize('mode', ['local', 'reader'])
def test_delayed_status_gates_data_and_socket_boot(frontend, mode):
    app = frontend(mode=mode, hold='/api/auth/status')
    app.open()
    page = app.page
    expect(page.locator('#btn-ingest-url')).to_be_disabled()
    assert app.requests == [('GET', '/api/auth/status', None)]
    assert not app.sockets
    assert page.evaluate('authReady') is False
    app.release('/api/auth/status', {'private': mode != 'local'})
    app.ready()
    check_controls(page, enabled=mode == 'local')


@pytest.mark.parametrize('failure', ['http', 'network'])
def test_failed_auth_status_stays_closed(frontend, failure):
    app = frontend(failure=failure)
    app.open()
    expect(app.page.locator('#private-login-error')).to_contain_text('Unable to check')
    assert app.page.evaluate('authReady') is False
    assert app.requests == [('GET', '/api/auth/status', None)]
    assert not app.sockets
    expect(app.page.locator('#btn-ingest-url')).to_be_disabled()


@pytest.mark.parametrize('abort_effective', [True, False])
def test_workspace_switch_and_logout_clear_data_and_fence_late_answer(frontend, abort_effective):
    app = frontend(mode='editor')
    page = app.page
    if not abort_effective:
        page.add_init_script("""const originalFetch = window.fetch;
            window.fetch = (url, options) => originalFetch(url,
                url === '/api/ask' ? {...options, signal:undefined} : options);""")
    page.add_init_script("""window.closedSockets = 0;
        const OriginalSocket = window.WebSocket;
        window.WebSocket = class extends OriginalSocket {
            constructor(...args) { super(...args);
                this.addEventListener('close', () => window.closedSockets++); }
        };
        localStorage.setItem('graphagent_api_key','synthetic-obsolete');
        localStorage.setItem('gaf.history.v1', JSON.stringify([
            {id:'synthetic-old', question:'Synthetic old history'}]));""")
    app.open()
    app.ready()
    app.hold = '/api/ask'
    page.locator('#query-input').fill('Synthetic delayed question')
    page.locator('#btn-query').click()
    expect(page.locator('#query-result .loader')).to_be_visible()
    page.locator('#bulk-urls').fill('https://example.invalid/old')
    page.locator('#annotation-note').fill('Synthetic old note')
    click(page, 'views')
    click(page, 'view-restore')
    expect(page.locator('#research-notes')).to_contain_text('Synthetic view')
    old_generation = page.evaluate('workspaceGeneration')
    page.locator('#private-workspace').select_option('beta')
    app.ready()
    expect(page.locator('#private-workspace')).to_have_value('beta')
    assert page.evaluate('workspaceGeneration') > old_generation
    for selector in ['#query-input', '#bulk-urls', '#annotation-note', '#investigation-title']:
        expect(page.locator(selector)).to_have_value('')
    expect(page.locator('#research-notes')).to_be_empty()
    expect(page.locator('#query-result')).to_be_empty()
    # Real fetch abort plus generation fence must prevent the old answer resurfacing.
    app.release('/api/ask', {'status': 'answered', 'answer': 'STALE SYNTHETIC ANSWER'})
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
    expect(page.locator('#query-result')).to_be_empty()
    assert page.evaluate('localStorage.length') == 0
    assert len(app.sockets) == 2
    page.wait_for_function('closedSockets === 1')
    page.locator('#private-logout').click()
    expect(page.locator('#private-login')).to_be_visible()
    expect(page.locator('#private-session')).to_be_hidden()
    assert page.evaluate('authReady') is False
    page.wait_for_function('closedSockets === 2')
    expect(page.locator('#force-graph')).to_be_empty()
    expect(page.locator('#sources-list')).to_be_empty()
    expect(page.locator('#query-history')).to_be_empty()
    expect(page.locator('#btn-ingest-url')).to_be_disabled()


@pytest.mark.parametrize('login_status', [200, 401])
def test_private_session_recovery_and_failed_login(frontend, login_status):
    app = frontend(mode='editor', hold='/api/auth/workspaces')
    app.open()
    app.page.wait_for_function('privateMode && !authReady')
    # The public mode response alone must not start workspace/provider work.
    assert not app.sockets
    assert all(path.startswith('/api/auth/') for _, path, _ in app.requests)
    app.release('/api/auth/workspaces', {'detail': 'Unauthorized'}, status=401)
    page = app.page
    expect(page.locator('#private-login')).to_be_visible()
    app.hold = '/api/auth/session'
    page.locator('#private-token').fill('synthetic-test-token-not-a-credential')
    page.locator('#private-login-form button').click()
    expect(page.locator('#private-token')).to_have_value('')
    app.release('/api/auth/session', app.identity(), status=login_status)
    if login_status == 200:
        app.ready()
        check_controls(page, enabled=True)
        expect(page.locator('#private-login')).to_be_hidden()
    else:
        expect(page.locator('#private-login-error')).to_contain_text('Unable to authorize')
        assert page.evaluate('authReady') is False
        assert not app.sockets
    assert page.evaluate('localStorage.length') == 0
