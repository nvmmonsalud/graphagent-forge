"""Static text guards over the analytics panel in frontend/index.html.

Same approach as tests/test_frontend_static.py: there is no JS runtime here, so
the panel is checked as plain text — the markup hooks exist, the node-sizing
seam is wired through `nodeRadius`, the old inline radius ternary is gone from
`renderGraph`, and none of the frontend-wide bans were reintroduced.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND_PATH = Path(__file__).resolve().parents[1] / "frontend" / "index.html"

OLD_RADIUS_EXPR = (
    ".attr('r', d => d.type === 'Concept' ? 4 : d.type === 'Technology' ? 5 : 3.5)"
)


@pytest.fixture(scope="module")
def html() -> str:
    if not FRONTEND_PATH.exists():
        pytest.fail(f"{FRONTEND_PATH} does not exist")
    return FRONTEND_PATH.read_text(encoding="utf-8")


def _render_graph_body(html: str) -> str:
    """The text of `function renderGraph` up to the next top-level `function`."""
    start = html.index("function renderGraph")
    nxt = html.find("\nfunction ", start + 1)
    assert nxt > start, "could not delimit renderGraph"
    return html[start:nxt]


def test_analytics_panel_markup_present(html: str) -> None:
    assert 'id="analytics-list"' in html
    assert 'id="btn-analytics"' in html


def test_analytics_endpoint_referenced(html: str) -> None:
    assert "/graph/analytics" in html


def test_node_radius_helpers_present(html: str) -> None:
    assert "function nodeRadius" in html
    assert "function nodeBaseRadius" in html


def test_node_base_radius_reproduces_the_original_expression(html: str) -> None:
    """The baseline radii must not drift — sizing multiplies this, it doesn't
    replace it."""
    assert (
        "return d.type === 'Concept' ? 4 : d.type === 'Technology' ? 5 : 3.5;" in html
    )


def test_render_graph_uses_the_node_radius_accessor(html: str) -> None:
    body = _render_graph_body(html)
    assert ".attr('r', nodeRadius)" in body
    assert OLD_RADIUS_EXPR not in body, (
        "renderGraph still hardcodes the inline radius ternary — analytics-driven "
        "sizing would be ignored on first render"
    )


def test_fill_is_still_bound_to_type(html: str) -> None:
    """Sizing must never recolour: #legend-bar is built from types."""
    assert ".attr('fill', d => tc(d.type))" in _render_graph_body(html)


def test_metric_segments_declare_their_metric(html: str) -> None:
    """The segmented control emits a data-metric attribute per option, and the
    three metric keys are the ones the backend contract names."""
    assert "data-metric=" in html
    for metric in ("'degree'", "'pagerank'", "'betweenness'"):
        assert metric in html, f"missing metric key {metric}"


def test_metric_selection_only_touches_radius(html: str) -> None:
    """Toggling a metric re-attributes r in place — no re-render, no restart."""
    assert "gNodes.attr('r', nodeRadius)" in html


def test_skeleton_guard_and_loaded_flag(html: str) -> None:
    assert "if (!listEl.dataset.loaded) listEl.innerHTML = skeletonLines(4" in html
    # dataset.loaded is set on both the success and the error path.
    assert html.count("listEl.dataset.loaded = '1';") >= 4


def test_refresh_button_uses_shared_busy_helper(html: str) -> None:
    assert "withButtonBusy(btn, 'Analyzing" in html


def test_exactly_one_result_box_error_rule(html: str) -> None:
    matches = re.findall(r"\.result-box\.error\s*\{", html)
    assert len(matches) == 1, (
        f"expected exactly one '.result-box.error {{' CSS rule, found {len(matches)}"
    )


def test_no_blocking_browser_dialogs(html: str) -> None:
    for banned in ("confirm(", "prompt(", "alert("):
        assert banned not in html, f"found blocking dialog call: {banned}"


def test_no_console_substring_anywhere(html: str) -> None:
    assert "console" not in html, "found a console.* reference"


def test_structure_failure_is_surfaced_not_dropped(html: str) -> None:
    assert "result-box notice" in html
    assert "betweenness_reason" in html
