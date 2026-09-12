"""Static text guards over the concurrency-sweep panel in frontend/index.html.

Same approach as tests/test_frontend_fanout_panel.py: there is no JS runtime
here, so the panel is checked as plain text — the markup hooks exist, the
WebSocket branch is wired, the chart is built from measured fields rather than
constants, and none of the frontend-wide bans were reintroduced.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FRONTEND_PATH = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


@pytest.fixture(scope="module")
def html() -> str:
    if not FRONTEND_PATH.exists():
        pytest.fail(f"{FRONTEND_PATH} does not exist")
    return FRONTEND_PATH.read_text(encoding="utf-8")


def test_sweep_panel_markup_present(html: str) -> None:
    assert 'id="sweep-card"' in html
    assert 'id="btn-sweep"' in html
    assert 'id="sweep-chart"' in html
    assert 'id="sweep-result"' in html


def test_sweep_endpoint_referenced(html: str) -> None:
    assert "/graph/verify/sweep" in html


def test_sweep_request_is_a_post(html: str) -> None:
    """The route is POST-only; a GET would 405 on stage."""
    assert "API + '/graph/verify/sweep', { method: 'POST' }" in html


def test_the_button_names_the_sizes_the_route_defaults_to(html: str) -> None:
    """The button label and the server's default sizes must not drift apart."""
    assert "Sweep 1 → 3 → 6 → 10" in html
    # No body is sent, so the endpoint's own defaults are what runs.
    assert "API + '/graph/verify/sweep', { method: 'POST' })" in html


def test_websocket_branch_handles_sweep_updates(html: str) -> None:
    assert "msg.type === 'sweep_update'" in html
    assert "msg.phase === 'size_started'" in html
    assert "msg.phase === 'size_done'" in html


def test_chart_is_built_from_measured_fields(html: str) -> None:
    """Both series come out of the response, never from a constant."""
    assert "function renderSweepChart" in html
    assert "sweepSeries(done, 'wall_ms'" in html
    assert "sweepSeries(done, 'serial_ms'" in html
    assert "function sweepPolyline" in html
    assert "function sweepYMax" in html


def test_a_refused_sandbox_keeps_a_hollow_marker(html: str) -> None:
    """A batch short of its requested size must not read as a clean point."""
    assert "short_by" in html
    assert "refused at the account ceiling" in html


def test_the_payload_is_declared_replicated_on_screen(html: str) -> None:
    """The panel says out loud that N sandboxes share one sub-graph."""
    assert "replicated into every sandbox" in html
    assert "not " in html and "different documents" in html
    assert "sweepMeta.payload_nodes" in html


def test_source_names_and_error_text_are_escaped(html: str) -> None:
    """Ingested titles are attacker-influenced; the provenance line escapes them."""
    assert "esc(sweepMeta.source_doc)" in html


def test_uses_the_shared_busy_and_skeleton_helpers(html: str) -> None:
    assert "withButtonBusy(btn, 'Sweeping…'" in html
    assert "skeletonLines(3" in html


def test_failure_degrades_to_a_notice_not_an_error_banner(html: str) -> None:
    """A sweep that can't run is infrastructure news, not a graph verdict."""
    assert "The sweep couldn't run" in html
    assert 'class="result-box notice"' in html


def test_no_blocking_dialog_calls(html: str) -> None:
    for banned in ("confirm(", "prompt(", "alert("):
        assert banned not in html, f"found blocking dialog call: {banned}"
