"""Static text guards over the fan-out audit panel in frontend/index.html.

Same approach as tests/test_frontend_analytics_panel.py: there is no JS runtime
here, so the panel is checked as plain text — the markup hooks exist, the
WebSocket branch is wired, the shared busy/skeleton helpers are reused, and
none of the frontend-wide bans were reintroduced.
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


def test_fanout_panel_markup_present(html: str) -> None:
    assert 'id="fanout-card"' in html
    assert 'id="btn-fanout"' in html
    assert 'id="fanout-list"' in html
    assert 'id="fanout-result"' in html


def test_fanout_endpoint_referenced(html: str) -> None:
    assert "/graph/verify/fanout" in html


def test_fanout_request_is_a_post(html: str) -> None:
    """The route is POST-only; a GET would 405 on stage."""
    assert "API + '/graph/verify/fanout', { method: 'POST' }" in html


def test_websocket_branch_handles_fanout_updates(html: str) -> None:
    assert "msg.type === 'fanout_update'" in html
    assert "msg.phase === 'started'" in html
    assert "msg.phase === 'done'" in html


def test_live_row_states_are_rendered(html: str) -> None:
    """A row must exist while its sandbox is still booting, not only after."""
    assert "booting…" in html
    assert "fan-row pending" in html


def test_timeline_bars_scale_against_the_measured_wall_clock(html: str) -> None:
    assert "function fanoutBar" in html
    assert "fanoutWallMs" in html
    assert "fan-boot" in html and "fan-run" in html


def test_uses_the_shared_busy_and_skeleton_helpers(html: str) -> None:
    assert "withButtonBusy(btn, 'Fanning out…'" in html
    assert "skeletonLines(4" in html


def test_source_names_are_escaped_before_rendering(html: str) -> None:
    """Ingested titles are attacker-influenced; every row writes them via esc()."""
    assert 'title="\' + esc(source) + \'"' in html


def test_failure_degrades_to_a_notice_not_an_error_banner(html: str) -> None:
    """A fan-out that can't run is infrastructure news, not a graph verdict."""
    assert "Fan-out audit couldn't run" in html


def test_no_blocking_browser_dialogs(html: str) -> None:
    for banned in ("confirm(", "prompt(", "alert("):
        assert banned not in html, f"found blocking dialog call: {banned}"


def test_no_console_substring_anywhere(html: str) -> None:
    assert "console" not in html, "found a console.* reference"


def test_exactly_one_result_box_error_rule(html: str) -> None:
    matches = re.findall(r"\.result-box\.error\s*\{", html)
    assert len(matches) == 1, (
        f"expected exactly one '.result-box.error {{' CSS rule, found {len(matches)}"
    )
