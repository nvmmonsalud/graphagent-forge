"""Exercise generated release assets in Chromium with no backend transport."""
import json
from urllib.parse import urlsplit

from scripts.build_vercel_preview import build


def test_generated_preview_fails_closed(browser, tmp_path):
    output = tmp_path / 'public'
    build(output, 'a' * 40)
    context = browser.new_context()
    page = context.new_page()
    errors = []
    requests = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def asset(route):
        path = urlsplit(route.request.url).path
        requests.append(path)
        target = output / ('index.html' if path == '/' else path.lstrip('/'))
        if target.is_file() and target.resolve().is_relative_to(output.resolve()):
            route.fulfill(path=str(target))
        else:
            route.abort()

    page.route('**/*', asset)
    page.goto('http://preview.test/')
    page.locator('#static-preview-banner').wait_for()
    page.wait_for_function("document.querySelectorAll('#force-graph circle').length >= 2")
    result = page.evaluate("""async () => {
      const denied = [];
      for (const [url, options] of [
        ['/api/ingest/text', {method:'POST', body:JSON.stringify({text:'private sentinel'})}],
        ['/api/ask', {method:'POST'}], ['/api/health', {}],
        ['https://example.com/provider', {}],
        [new Request(location.origin + '/api/graph/data', {method:'DELETE'}), {}]
      ]) {
        const response = await fetch(url, options);
        denied.push({status:response.status, body:await response.json()});
      }
      localStorage.setItem('graphagent_api_key', 'private sentinel');
      sessionStorage.setItem('history', 'private sentinel');
      return {denied, key:localStorage.getItem('graphagent_api_key'),
        history:sessionStorage.getItem('history'), socket:new WebSocket('wss://example.com').readyState};
    }""")
    assert all(item['status'] == 503 for item in result['denied'])
    assert all('unavailable' in item['body']['detail'] for item in result['denied'])
    assert result['key'] is None and result['history'] is None
    assert result['socket'] == 3
    assert not errors
    assert set(requests) <= {'/', '/preview.js', '/vendor/d3.v7.min.js', '/favicon.ico'}
    manifest = json.loads((output / 'build-manifest.json').read_text())
    assert manifest['backend'] is False
    context.close()
