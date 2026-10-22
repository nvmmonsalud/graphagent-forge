"""Shipped browser UI against the real private app, without API/WS stubs."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.hosted

DOCUMENT = "Apricot built Cedar in 2042. Private marker: orchid-acceptance-739."


def login(page, hosted, principal="alice"):
    page.goto(hosted.origin)
    expect(page.locator("#private-login")).to_be_visible()
    page.locator("#private-token").evaluate(
        "(element, value) => { element.value = value; }", hosted.tokens[principal]
    )
    page.locator("#private-login-form button[type=submit]").click()
    expect(page.locator("#private-session")).to_be_visible()
    page.wait_for_function("() => document.querySelector('#private-token').value === ''")
    expect(page.locator("#private-workspace")).to_have_value("alpha")


def assert_private_storage_empty(page, tokens):
    clean = page.evaluate("""tokens => {
        const stored = JSON.stringify({...localStorage});
        return !stored.includes('orchid-acceptance-739') && !stored.includes('Apricot')
            && tokens.every(token => !stored.includes(token));
    }""", list(tokens.values()))
    assert clean, "Private content or credentials persisted in browser storage"


def test_browser_ingest_answer_citation_switch_and_logout(hosted, hosted_browser):
    with hosted_browser.new_context() as context:
        # Fonts are decorative. The app, APIs, assets and WebSocket use real network.
        context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        page = context.new_page()
        errors = []
        requests = []
        sockets = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.on("websocket", lambda socket: sockets.append(socket.url))
        login(page, hosted)
        expect(page.locator("#private-role")).to_have_text("editor")
        page.locator("#ingest-text-input").fill(DOCUMENT)
        page.locator("#ingest-source").fill("browser-connected-note")
        with page.expect_response(lambda r: r.url.endswith("/api/ingest/text")
                                  and r.request.method == "POST") as submitted:
            page.locator("#btn-ingest-text").click()
        assert submitted.value.status == 202
        expect(page.locator("#btn-ingest-text")).to_be_enabled(timeout=30000)
        expect(page.locator("#force-graph .nodes circle")).to_have_count(2, timeout=30000)
        expect(page.locator("#force-graph .labels")).to_contain_text("Apricot")
        socket_path_valid = sockets == [hosted.origin.replace("http:", "ws:") + "/ws/graph"]
        assert socket_path_valid, "Unexpected WebSocket destination"
        page.locator("#query-input").fill("What did Apricot build?")
        page.locator("#btn-query").click()
        citation = page.locator("#query-result [data-evidence-id]").first
        expect(citation).to_be_visible(timeout=30000)
        citation.click()
        expect(page.locator("#evidence-detail")).to_contain_text(DOCUMENT)
        expect(page.locator("#evidence-detail")).to_contain_text("Normalized text offsets: 0–")
        assert_private_storage_empty(page, hosted.tokens)
        page.locator("#private-workspace").select_option("beta")
        expect(page.locator("#private-workspace")).to_have_value("beta")
        expect(page.locator("#force-graph .nodes circle")).to_have_count(0)
        expect(page.locator("#query-result")).not_to_contain_text("Apricot")
        expect(page.locator("#query-input")).to_have_value("")
        expect(page.locator("#query-history")).not_to_contain_text("Apricot")
        assert_private_storage_empty(page, hosted.tokens)
        page.locator("#private-workspace").select_option("alpha")
        expect(page.locator("#force-graph .nodes circle")).to_have_count(2, timeout=30000)
        page.reload()
        expect(page.locator("#private-session")).to_be_visible()
        expect(page.locator("#force-graph .nodes circle")).to_have_count(2, timeout=30000)
        page.locator("#private-logout").click()
        expect(page.locator("#private-login")).to_be_visible()
        expect(page.locator("#force-graph .nodes circle")).to_have_count(0)
        expect(page.locator("#query-history")).not_to_contain_text("Apricot")
        assert_private_storage_empty(page, hosted.tokens)
        page.reload()
        expect(page.locator("#private-login")).to_be_visible()
        expect(page.locator("#private-session")).to_be_hidden()
        no_page_errors = not errors
        allowed_destinations = all(
            urlsplit(url).hostname in {"127.0.0.1", "fonts.googleapis.com", "fonts.gstatic.com"}
            for url in requests
        )
        credential_free_urls = all(
            token not in url for token in hosted.tokens.values() for url in requests + sockets
        )
        assert no_page_errors, "Browser raised an application error"
        assert allowed_destinations, "Browser requested an unexpected destination"
        assert credential_free_urls, "Browser exposed credentials in a URL"


def test_connected_reader_controls_and_server_enforcement(hosted, hosted_browser):
    with hosted_browser.new_context() as context:
        context.route("https://fonts.googleapis.com/**", lambda route: route.abort())
        context.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        page = context.new_page()
        login(page, hosted, "reader")
        expect(page.locator("#private-role")).to_have_text("reader")
        for selector in ("#btn-ingest-text", "#btn-ingest-file", "#btn-clear-graph",
                         "#btn-verify", "#btn-analytics"):
            expect(page.locator(selector)).to_be_disabled()
        expect(page.locator("#btn-query")).to_be_enabled()
        # Browser cookie, actual request and server role check; no route replacement.
        result = page.evaluate("""async () => {
            const response = await fetch('/api/graph/clear', {method:'POST'});
            return response.status;
        }""")
        assert result == 403
        assert hosted.provider.calls == {"extraction": 0, "answer": 0}
        assert_private_storage_empty(page, hosted.tokens)
