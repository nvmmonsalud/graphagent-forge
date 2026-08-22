"""Static text guards over the query-history panel in frontend/index.html.

Same approach as tests/test_frontend_static.py: there is no JS runtime here, so
the panel is checked as plain text — markup hooks exist, the localStorage
mirror is wrapped defensively, doQuery calls loadQueryHistory() from its own
body without being restructured, and none of the frontend-wide bans were
reintroduced.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND_PATH = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    if not FRONTEND_PATH.exists():
        pytest.fail(f"{FRONTEND_PATH} does not exist")
    return FRONTEND_PATH.read_text(encoding="utf-8")


def _do_query_body(html: str) -> str:
    """The text of `async function doQuery` up to the next top-level `function`."""
    start = html.index("async function doQuery")
    nxt = html.find("\nfunction ", start + 1)
    assert nxt > start, "could not delimit doQuery"
    return html[start:nxt]


def test_history_panel_markup_present(html: str) -> None:
    assert 'id="query-history"' in html


def test_history_panel_is_sibling_of_query_result(html: str) -> None:
    assert (
        '<div id="query-result" style="display:none"></div>\n'
        '    <div id="query-history" style="display:none;margin-top:1.4rem"></div>'
        in html
    )


def test_load_query_history_function_present(html: str) -> None:
    assert "function loadQueryHistory" in html


def test_history_endpoint_referenced(html: str) -> None:
    assert "/history" in html


def test_history_save_and_delete_endpoints_referenced(html: str) -> None:
    assert "/history/" in html
    assert "/save" in html


def test_local_storage_mirror_present_and_versioned(html: str) -> None:
    assert "localStorage" in html
    assert "gaf.history.v1" in html


def test_local_storage_calls_are_try_catch_guarded(html: str) -> None:
    """Every localStorage.getItem/setItem/removeItem call must sit inside a
    try block nearby — private mode and quota exhaustion both throw."""
    for call in ("localStorage.getItem", "localStorage.setItem", "localStorage.removeItem"):
        idx = html.index(call)
        window = html[max(0, idx - 200) : idx]
        assert "try" in window, f"{call} does not appear to be try/catch guarded"


def test_history_merge_is_by_id_server_wins(html: str) -> None:
    assert "serverIds" in html
    assert "__restored" in html


def test_restored_rows_omit_save_and_delete_actions(html: str) -> None:
    assert 'data-action="save"' in html
    assert 'data-action="delete"' in html
    assert 'data-action="rerun"' in html
    assert "restored" in html


def test_saved_limit_conflict_renders_dedicated_toast(html: str) -> None:
    assert "Saved limit reached — unsave one first." in html
    assert "e.status === 409" in html


def test_empty_state_copy_is_honest(html: str) -> None:
    assert (
        "No answers yet. Server history resets when the server restarts; "
        "this browser keeps a local copy." in html
    )


def test_clear_local_affordance_present_and_uses_confirm_dialog(html: str) -> None:
    assert 'data-action="clear-local"' in html
    assert "confirmDialog(" in html


def test_skeleton_guard_and_loaded_flag(html: str) -> None:
    assert "if (!panel.dataset.loaded) {" in html
    assert "panel.dataset.loaded = '1';" in html
    assert html.count("panel.dataset.loaded = '1';") >= 2


def test_do_query_calls_load_query_history_within_its_own_body(html: str) -> None:
    body = _do_query_body(html)
    assert "loadQueryHistory()" in body


def test_do_query_history_call_is_wrapped_defensively(html: str) -> None:
    body = _do_query_body(html)
    idx = body.index("loadQueryHistory()")
    window = body[max(0, idx - 120) : idx]
    assert "try" in window, "loadQueryHistory() call in doQuery is not defensively wrapped"


def test_do_query_five_state_chain_untouched(html: str) -> None:
    body = _do_query_body(html)
    assert "if (status === 'no_context')" in body
    assert "else if (status === 'llm_unavailable')" in body
    assert "else if (!d || !d.answer)" in body
    assert "} else {" in body


def test_drive_result_state_comment_present_verbatim(html: str) -> None:
    assert (
        "// Drive the result state off the server's `status`, NEVER off the answer text."
        in html
    )


def test_exactly_one_result_box_error_rule(html: str) -> None:
    matches = re.findall(r"\.result-box\.error\s*\{", html)
    assert len(matches) == 1, (
        f"expected exactly one '.result-box.error {{' CSS rule, found {len(matches)}"
    )


def test_reuses_existing_result_box_classes(html: str) -> None:
    assert "result-box notice" in html
    assert "result-box error" in html


def test_no_blocking_browser_dialogs(html: str) -> None:
    for banned in ("confirm(", "prompt(", "alert("):
        assert banned not in html, f"found blocking dialog call: {banned}"


def test_no_console_substring_anywhere(html: str) -> None:
    assert "console" not in html, "found a console.* reference"


def test_status_pill_driven_off_status_field_not_answer_text(html: str) -> None:
    assert "function historyStatusClass" in html
    assert "function historyStatusLabel" in html
    assert "entry && entry.status" in html


def test_context_nodes_count_helper_present(html: str) -> None:
    assert "function historyContextCount" in html


def test_with_button_busy_used_for_history_actions(html: str) -> None:
    assert "withButtonBusy(btn, 'Deleting" in html
    assert "withButtonBusy(btn, entry.saved" in html
    assert "withButtonBusy(btn, 'Re-running" in html


def test_event_delegation_by_index_not_serialized_ids(html: str) -> None:
    assert 'data-entry="${esc(i)}"' in html
