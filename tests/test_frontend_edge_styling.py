"""Guards for the force graph's edge stroke and its centring force.

Both invariants here exist because of bugs that were *invisible* rather than
loud, so a test is the only thing that keeps them true:

* The edge stroke colour is drawn from six different places (initial render,
  baseline/legend state, hover, path highlight, WebSocket append, post-merge
  redraw). While each carried its own literal, changing the colour at one site
  silently left five behind — the same failure mode as the node-radius rule that
  live-ingest and post-merge nodes kept ignoring.
* ``forceX``/``forceY`` bound how far disconnected components drift, but adding
  them without re-targeting in ``handleGraphResize`` is a half-fix that only
  shows up after a window resize, when the pull still points at the old centre.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = REPO_ROOT / "frontend" / "index.html"

#: The literals that used to be inlined at the link-draw sites. They remain
#: legitimate elsewhere in the stylesheet (``--border``, panel backgrounds),
#: so the assertions below are scoped to stroke assignments only.
LEGACY_EDGE_LITERALS = ("rgba(255,255,255,0.06)", "rgba(255,255,255,0.02)")

#: Matches any ``.attr('stroke', ...)`` / ``.attr('stroke', d => ...)`` call,
#: capturing the value expression up to the end of the line.
STROKE_ATTR_RE = re.compile(r"\.attr\(\s*'stroke'\s*,(?P<value>[^\n]*)")


@pytest.fixture(scope="module")
def html() -> str:
    assert INDEX_PATH.exists(), "frontend/index.html does not exist"
    return INDEX_PATH.read_text(encoding="utf-8")


def test_edge_stroke_constants_defined_once(html: str) -> None:
    for name in ("EDGE_STROKE", "EDGE_STROKE_DIM"):
        definitions = re.findall(rf"^const {name} = ", html, re.MULTILINE)
        assert len(definitions) == 1, (
            f"expected exactly one definition of {name}, found {len(definitions)}"
        )


def test_no_stroke_assignment_inlines_a_legacy_edge_literal(html: str) -> None:
    offenders = []
    for line_no, line in enumerate(html.splitlines(), 1):
        match = STROKE_ATTR_RE.search(line)
        if not match:
            continue
        value = match.group("value")
        for literal in LEGACY_EDGE_LITERALS:
            if literal in value:
                offenders.append((line_no, literal, line.strip()))
    assert not offenders, (
        "edge stroke colours must come from EDGE_STROKE/EDGE_STROKE_DIM, not an "
        "inline literal — otherwise changing one draw site leaves the others "
        f"behind. Offending lines: {offenders}"
    )


def test_every_draw_site_uses_the_constants(html: str) -> None:
    # Six sites draw links; the baseline state references both constants in one
    # ternary, so the constants appear across at least six stroke assignments.
    using_constants = [
        line
        for line in html.splitlines()
        if (m := STROKE_ATTR_RE.search(line))
        and ("EDGE_STROKE" in m.group("value"))
    ]
    assert len(using_constants) >= 5, (
        "expected the link-drawing paths (initial render, baseline, hover, path "
        f"highlight, WebSocket append, post-merge) to use the constants; found "
        f"{len(using_constants)} stroke assignments referencing them"
    )


def test_centring_force_is_registered(html: str) -> None:
    assert "d3.forceX(" in html and "d3.forceY(" in html, (
        "forceCenter only translates the centroid — without a per-node forceX/"
        "forceY pull, disconnected components drift out of the panel"
    )
    assert "const GRAPH_CENTER_STRENGTH = " in html, (
        "the centring strength belongs in a named constant, not inline"
    )


def test_resize_handler_retargets_the_centring_force(html: str) -> None:
    match = re.search(
        r"function handleGraphResize\(\)\s*\{(?P<body>.*?)\n\}", html, re.DOTALL
    )
    assert match, "handleGraphResize() not found in frontend/index.html"
    body = match.group("body")
    for call in ("simulation.force('x')", "simulation.force('y')"):
        assert call in body, (
            f"handleGraphResize must re-target {call} — otherwise after a window "
            "resize the pull keeps hauling nodes toward the old centre"
        )
