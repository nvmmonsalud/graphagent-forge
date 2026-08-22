"""Static text guards over frontend/index.html.

There's no JS runtime in this test environment, so this file is the only
automated check on a ~2000-line hand-rolled frontend: it reads the file as
plain text and asserts on substrings/regexes rather than executing anything.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND_PATH = Path(__file__).resolve().parents[1] / "frontend" / "index.html"

# Measured against the pre-demo-polish frontend/index.html at the time this
# test was written (66 occurrences of `esc(`) and used as a floor, per the
# WP6 brief, rather than a number invented ahead of the parallel frontend
# work.
MIN_ESC_CALLS = 66


@pytest.fixture(scope="module")
def html() -> str:
    if not FRONTEND_PATH.exists():
        pytest.fail(f"{FRONTEND_PATH} does not exist")
    return FRONTEND_PATH.read_text(encoding="utf-8")


def test_no_blocking_browser_dialogs(html: str) -> None:
    """confirm()/prompt()/alert() block the whole tab — the polished demo UI
    must use inline UI (toasts/modals) instead."""
    for banned in ("confirm(", "prompt(", "alert("):
        assert banned not in html, f"found blocking dialog call: {banned}"


def test_no_console_logging_left_in(html: str) -> None:
    for banned in re.findall(r"console\.(?:log|warn|error)\(", html):
        pytest.fail(f"found leftover console call: {banned}")


def test_double_click_to_zoom_reset_present(html: str) -> None:
    assert "dblclick.zoom" in html


def test_render_graph_function_present(html: str) -> None:
    assert "function renderGraph" in html


def test_exactly_one_result_box_error_rule(html: str) -> None:
    matches = re.findall(r"\.result-box\.error\s*\{", html)
    assert len(matches) == 1, (
        f"expected exactly one '.result-box.error {{' CSS rule, found {len(matches)}"
    )


def test_esc_helper_used_at_least_as_much_as_the_pre_polish_baseline(html: str) -> None:
    count = len(re.findall(r"esc\(", html))
    assert count >= MIN_ESC_CALLS, (
        f"expected esc( to appear at least {MIN_ESC_CALLS} times "
        f"(pre-polish baseline), got {count} — dynamic content may have "
        f"regressed to raw innerHTML"
    )


def test_flagship_query_placeholder_present_verbatim(html: str) -> None:
    assert "Who are the sponsors and what do they provide?" in html
