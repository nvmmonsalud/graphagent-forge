"""Deterministic UI capture: drive the live frontend with Playwright and write PNGs.

Why this exists: the demo UI is a single 2700-line page whose interesting states
(a settled force graph, a duplicate-review list, a keyless GraphRAG answer) only
exist *after* a real server has answered real requests. Hand-cropped images rot
the moment the layout moves, so this script re-derives them from a running app.

Usage::

    python scripts/capture_ui.py --base-url http://localhost:8000 --out assets/ui \\
           --width 1440 --dsf 1 [--only hero,graph,query,duplicates,verify,path]

Exit codes: 0 every requested capture written · 1 bad arguments · 2 Playwright not
importable (the install line is printed) · 3 a precondition wait timed out (the
failing wait is named).

Expected server state
---------------------
Point this at a server started with **no API keys** and a seeded graph
(``python -m scripts.seed_graph --reset`` → 56 nodes / 84 edges / 3 tier-1
duplicate groups). Keyless is the honest demo state and the one the captures are
meant to show: ``query`` renders the amber "No LLM configured — GraphRAG still
retrieved N entities" notice rather than a fabricated answer, and ``duplicates``
shows exact-label (tier-1) groups with semantic matching off. The node-count
waits below are pinned to that fixture, so re-seed before capturing.

How determinism is achieved (and where it stops)
------------------------------------------------
The hero canvas seeds 70 particles with ``Math.random()`` on every load, so an
``add_init_script`` installs a seeded LCG over ``Math.random`` *before* any page
script runs. Playwright's ``animations="disabled"`` only freezes CSS animations —
it does not touch ``requestAnimationFrame`` loops — so the D3 simulation is
additionally waited to ``alpha() < 0.005`` and then explicitly stopped.

Once stopped, the graph is fitted to its panel (see ``FIT_GRAPH_JS``). The app's
default view now holds all 56 seed nodes on its own — ``GRAPH_CENTER_STRENGTH``
bounds how far the three disconnected components drift — so the fit is framing
rather than rescue: it zooms the layout up to fill the panel instead of leaving it
adrift in the middle. It applies the same transform a user would reach for with
the page's existing zoom control, and the run aborts rather than emitting a
capture with any node outside the frame.

Measured over two consecutive runs, ``graph``, ``duplicates``, ``query`` and
``path`` came out **pixel-identical**. Two captures carry a little residual
variance, accepted rather than fought:

* **Hero particles drift a few pixels** (~0.3% of pixels differ). Their *initial*
  positions are identical thanks to the seeded LCG, but each rAF frame advances
  them by up to 0.25 px and the number of frames elapsing before the capture
  depends on machine speed. Composition, colours and counters are stable.
* **The verify capture prints a live duration** ("verified in 45ms"), so those
  few glyphs change per run. That number is measured with ``perf_counter()`` at
  call time by design — it is a real measurement, not a constant to pin.

Force-graph placement is *not* on that list: it is seeded through the same LCG, so
it reproduced exactly here. Treat that as reliable-in-practice rather than
guaranteed — the settle loop is rAF-driven, so a very different machine could take
a different number of ticks to cross the alpha threshold.

Everything else — fonts loaded, WebSocket badge reading ``live``, counter tweens
finished, every ``.reveal`` section faded in, no spinner mid-flight — is waited on
explicitly.

A PNG is rendered to memory and then moved into place, so an interrupted or failed
run never leaves a truncated file behind.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "playwright not installed: pip install playwright  "
        "(browsers already at PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers)",
        file=sys.stderr,
    )
    sys.exit(2)

# Pillow is required for the same reason playwright is: without the palette pass
# below, a raw capture of the graph panel lands around 700 KB and the whole set
# near 2 MB, which blows the caps tests/test_readme_assets.py enforces. Emitting
# images that fail the repo's own guard is worse than refusing to run, so this
# fails fast with an install line rather than degrading.
try:
    from PIL import Image
except ImportError:
    print("Pillow not installed: pip install Pillow", file=sys.stderr)
    sys.exit(2)

EXIT_OK = 0
EXIT_BAD_ARGS = 1
EXIT_NO_PLAYWRIGHT = 2
EXIT_PRECONDITION = 3

#: Per-image ceiling, kept below the cap in tests/test_readme_assets.py so the
#: script reports a problem before CI does.
MAX_IMAGE_BYTES = 400 * 1024

#: Nodes in seed/graph.json. Both the hero counter and the graph badge are waited
#: on against this, which doubles as proof the 800ms animNum tween has settled.
SEED_NODE_COUNT = 56

#: Asked verbatim in the pitch; the input's own placeholder uses the same text.
DEMO_QUESTION = "Who are the sponsors and what do they provide?"

#: Seeded LCG over Math.random, installed before any page script evaluates.
#: Without it the 70 hero particles land somewhere new on every single load.
SEED_RANDOM_JS = """
(() => {
  let s = 0x2f6e2b1 >>> 0;
  Math.random = function () {
    s = (Math.imul(1664525, s) + 1013904223) >>> 0;
    return s / 4294967296;
  };
})();
"""

#: Page-level preconditions, in the order they must hold. Each entry is
#: (human-readable name, JS predicate polled by wait_for_function).
PAGE_WAITS = [
    (
        "webfonts loaded",
        "document.fonts.status === 'loaded'",
    ),
    (
        "websocket badge reads 'live'",
        "document.getElementById('conn-label')"
        " && document.getElementById('conn-label').textContent.trim() === 'live'",
    ),
    (
        f"hero node counter reached {SEED_NODE_COUNT} (counter tween settled)",
        "document.getElementById('hero-nodes')"
        " && document.getElementById('hero-nodes').textContent.trim() === "
        f"'{SEED_NODE_COUNT}'",
    ),
    (
        f"graph badge reports {SEED_NODE_COUNT} nodes",
        "document.getElementById('vis-counts')"
        f" && /{SEED_NODE_COUNT}/.test(document.getElementById('vis-counts').textContent)",
    ),
]

#: `simulation` is a top-level `let` in the page script: that is a script-scoped
#: binding, NOT a property of window. `window.simulation` is undefined and would
#: hang forever — the bare identifier is deliberate.
SIMULATION_SETTLED_JS = (
    "typeof simulation !== 'undefined' && simulation && simulation.alpha() < 0.005"
)

#: The IntersectionObserver (threshold 0.1) leaves every un-scrolled section at
#: opacity:0. Without this every capture below the fold is blank.
REVEAL_ALL_JS = "document.querySelectorAll('.reveal').forEach(e => e.classList.add('visible'))"

#: Fit the settled force graph to its viewport.
#:
#: The seed fixture is three disconnected components that `forceManyBody(-80)`
#: pushes apart. The page's `forceX`/`forceY` pull keeps them inside the 1098x498
#: panel (before it, only 23 of 56 nodes landed in frame), but a settled layout
#: still occupies roughly 416x434 of that panel, so it reads small and
#: off-balance. Scaling it up to fill the frame is what this does — the same
#: transform the page's own zoom control applies interactively.
#:
#: Reachability: `svgEl`, `gLabels` and `currentZoomK` are top-level `let`s, i.e.
#: global-lexical bindings that a bare identifier resolves. `zoomBehavior` and the
#: zoomed `<g>` are `const`s local to `renderGraph()` and are NOT reachable, so the
#: transform goes onto the `<g>` directly (the documented fallback). d3-zoom keeps
#: its state in the `__zoom` property of the SVG node, so that is written too —
#: otherwise the next interactive zoom would snap back to the unfitted view.
FIT_GRAPH_JS = """
(() => {
  if (typeof simulation === 'undefined' || !simulation || !svgEl) return null;
  const svgNode = svgEl.node();
  const g = svgNode && svgNode.querySelector(':scope > g');
  const pts = simulation.nodes().filter(n => isFinite(n.x) && isFinite(n.y));
  if (!g || !pts.length) return null;

  const W = +svgEl.attr('width'), H = +svgEl.attr('height');
  if (!(W > 0 && H > 0)) return null;

  const xs = pts.map(n => n.x), ys = pts.map(n => n.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  // Node radius maxes out at 5px and the glow filter blurs ~3px past that;
  // pad the box so the outermost dots are not shaved by the panel edge.
  const m = 12;
  const bw = Math.max((x1 - x0) + 2 * m, 1), bh = Math.max((y1 - y0) + 2 * m, 1);

  const PAD = 0.08;
  let k = Math.min((W * (1 - 2 * PAD)) / bw, (H * (1 - 2 * PAD)) / bh);
  k = Math.max(0.2, Math.min(5, k));  // the page's own scaleExtent

  const tx = W / 2 - k * ((x0 + x1) / 2);
  const ty = H / 2 - k * ((y0 + y1) / 2);
  const t = d3.zoomIdentity.translate(tx, ty).scale(k);

  g.setAttribute('transform', t.toString());
  svgNode.__zoom = t;
  currentZoomK = k;

  // Edge stroke width at low zoom is handled by the page's own CSS
  // (#force-graph .links line { vector-effect: non-scaling-stroke }), so the
  // capture inherits it rather than injecting a property the app lacks.
  // Mirror the page's own zoom handler: labels live below 1.2.
  if (typeof gLabels !== 'undefined' && gLabels) {
    gLabels.attr('opacity', k > 1.2 ? 1 : 0);
  }

  const inside = pts.filter(n => {
    const px = n.x * k + tx, py = n.y * k + ty;
    return px >= 0 && px <= W && py >= 0 && py <= H;
  }).length;
  return { k: k, inside: inside, total: pts.length, width: W, height: H };
})()
"""

#: One dict per capture, in capture order. Append an entry here to add one.
#:   key           output filename stem, and the --only token
#:   locator       CSS selector of the element to capture
#:   fill          optional [(selector, text), ...] typed before the click
#:   click         optional selector clicked to trigger the panel
#:   wait_visible  optional selector that must become visible afterwards
#:   wait_js       optional (name, JS predicate) extra precondition
#:   wait_settled  optional container selector that must hold no `.loader`
#: `path` is captured last on purpose: it highlights nodes in the shared force
#: graph, so it must not run before the plain `graph` capture.
SHOTS = [
    {
        "key": "hero",
        "locator": "section.hero",
    },
    {
        # The Analytics card sits inside this section and rests un-run, so
        # without triggering it the capture shows grey skeleton bars.
        "key": "graph",
        "locator": "#graph .section-inner",
        "click": "#btn-analytics",
        "wait_js": (
            "analytics rendered",
            "!!document.querySelector('#analytics-list .an-strip')",
        ),
        "wait_settled": "#analytics-list",
    },
    {
        "key": "analytics",
        "locator": "#analytics-card",
        "wait_js": (
            "analytics rendered",
            "!!document.querySelector('#analytics-list .an-strip')",
        ),
        "wait_settled": "#analytics-list",
    },
    {
        "key": "duplicates",
        "locator": "#duplicates-card",
        "wait_js": (
            "duplicate groups rendered",
            "document.querySelectorAll('#duplicates-list .mini-btn').length >= 1",
        ),
    },
    {
        "key": "verify",
        "locator": "#tools-card",
        "click": "#btn-verify",
        "wait_visible": "#verify-result .result-box",
        "wait_settled": "#verify-result",
    },
    {
        "key": "query",
        "locator": "#query .section-inner",
        "fill": [("#query-input", DEMO_QUESTION)],
        "click": "#btn-query",
        "wait_visible": "#query-result .result-box",
        "wait_settled": "#query-result",
    },
    {
        "key": "path",
        "locator": "#path .section-inner",
        "fill": [("#path-from", "SSRF Guard"), ("#path-to", "D3 Force Graph")],
        "click": "#btn-path",
        "wait_visible": "#path-result",
        "wait_settled": "#path-result",
    },
]


class PreconditionTimeout(RuntimeError):
    """A named wait did not come true in time. Carries the wait's name."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class _Parser(argparse.ArgumentParser):
    """Argparse exits 1 on usage errors here, leaving 2 to mean 'no Playwright'."""

    def error(self, message: str):  # noqa: D102 - argparse hook
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_BAD_ARGS)


def wait_for(page, name: str, expression: str, timeout: int) -> None:
    """Poll `expression` in the page, raising PreconditionTimeout(name) on failure."""
    try:
        page.wait_for_function(expression, timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise PreconditionTimeout(name) from exc


def wait_visible(page, name: str, selector: str, timeout: int) -> None:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise PreconditionTimeout(name) from exc


def prepare_page(page, base_url: str, timeout: int) -> dict:
    """Load the page, hold it until every precondition is true, and fit the graph.

    Returns the fit diagnostics so the caller can report the scale that was used.
    """
    try:
        page.goto(base_url, wait_until="networkidle", timeout=timeout)
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        raise PreconditionTimeout(f"navigate to {base_url} (is the server running?)") from exc

    for name, expression in PAGE_WAITS:
        wait_for(page, name, expression, timeout)

    page.evaluate(REVEAL_ALL_JS)

    wait_for(page, "force simulation settled (alpha < 0.005)", SIMULATION_SETTLED_JS, timeout)
    page.evaluate("simulation.stop()")

    fit = page.evaluate(FIT_GRAPH_JS)
    if not fit:
        raise PreconditionTimeout("force graph fit-to-content (no settled node positions)")
    if fit["inside"] < fit["total"]:
        raise PreconditionTimeout(
            f"force graph fit-to-content left {fit['total'] - fit['inside']} "
            f"of {fit['total']} nodes outside the {fit['width']}x{fit['height']} panel"
        )
    return fit


def quantize(data: bytes) -> bytes:
    """Shrink a screenshot to a 256-colour palette PNG.

    The UI is flat dark surfaces, a handful of accent colours and text — well
    under 256 distinct colours everywhere except the hero's particle gradients
    and the graph's glow filter, where the loss is imperceptible at these sizes.
    Typically a 4-5x saving, which is the difference between the capture set
    fitting the README budget and not.
    """
    with Image.open(io.BytesIO(data)) as img:
        palette = img.convert("RGB").quantize(colors=256, method=Image.Quantize.MEDIANCUT)
        buf = io.BytesIO()
        palette.save(buf, format="PNG", optimize=True)
    shrunk = buf.getvalue()
    # A capture that somehow compresses worse keeps the original.
    return shrunk if len(shrunk) < len(data) else data


def capture(page, shot: dict, out_dir: Path, timeout: int) -> Path:
    """Run one shot's pre-actions and write `<out_dir>/<key>.png` atomically."""
    key = shot["key"]

    for selector, value in shot.get("fill", []):
        page.fill(selector, value)
    if shot.get("click"):
        page.click(shot["click"])

    if shot.get("wait_visible"):
        wait_visible(page, f"[{key}] {shot['wait_visible']} visible", shot["wait_visible"], timeout)
    if shot.get("wait_js"):
        wait_name, expression = shot["wait_js"]
        wait_for(page, f"[{key}] {wait_name}", expression, timeout)
    if shot.get("wait_settled"):
        container = shot["wait_settled"]
        # The result boxes are shown immediately with a spinner inside; capturing
        # on visibility alone would freeze the loader instead of the answer.
        wait_for(
            page,
            f"[{key}] {container} finished loading",
            f"document.querySelector({container!r})"
            f" && !document.querySelector({container + ' .loader'!r})",
            timeout,
        )

    target = page.locator(shot["locator"]).first
    try:
        target.wait_for(state="visible", timeout=timeout)
        data = target.screenshot(animations="disabled", timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise PreconditionTimeout(f"[{key}] element {shot['locator']} ready to capture") from exc

    # Render to memory, then move into place: a failed run never leaves a
    # truncated PNG that a later agent would embed.
    final = out_dir / f"{key}.png"
    staging = out_dir / f".{key}.png.part"
    staging.write_bytes(quantize(data))
    os.replace(staging, final)
    return final


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        prog="capture_ui.py",
        description="Capture deterministic PNGs of the GraphAgent Forge UI.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000", help="running server URL")
    parser.add_argument("--out", default="assets/ui", help="output directory for the PNGs")
    parser.add_argument("--width", type=int, default=1440, help="viewport width in CSS pixels")
    parser.add_argument("--dsf", type=float, default=1.0, help="device scale factor")
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated subset of: " + ",".join(s["key"] for s in SHOTS),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
        help="per-wait timeout in milliseconds (default 30000)",
    )
    return parser.parse_args(argv)


def select_shots(only: str) -> list[dict]:
    if not only.strip():
        return list(SHOTS)
    wanted = [token.strip() for token in only.split(",") if token.strip()]
    known = {shot["key"] for shot in SHOTS}
    unknown = [token for token in wanted if token not in known]
    if unknown:
        print(
            f"unknown --only key(s): {', '.join(unknown)} "
            f"(known: {', '.join(shot['key'] for shot in SHOTS)})",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_BAD_ARGS)
    # Keep SHOTS order regardless of the order given on the command line: `path`
    # highlights the force graph, so it must stay after `graph`.
    return [shot for shot in SHOTS if shot["key"] in set(wanted)]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    shots = select_shots(args.only)
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    fit: dict = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(
                viewport={"width": args.width, "height": 1000},
                device_scale_factor=args.dsf,
                reduced_motion="reduce",
                color_scheme="dark",
            )
            # Must be registered on the context BEFORE the first navigation.
            context.add_init_script(SEED_RANDOM_JS)
            page = context.new_page()
            try:
                fit = prepare_page(page, args.base_url, args.timeout)
                for shot in shots:
                    written.append(capture(page, shot, out_dir, args.timeout))
            except PreconditionTimeout as exc:
                print(f"precondition wait failed: {exc.name}", file=sys.stderr)
                return EXIT_PRECONDITION
            finally:
                context.close()
        finally:
            browser.close()

    if fit:
        print(
            f"graph fit: scale {fit['k']:.3f} · all {fit['total']} nodes inside "
            f"{fit['width']}x{fit['height']}"
        )
    oversize = []
    for path in written:
        size = path.stat().st_size
        flag = ""
        if size > MAX_IMAGE_BYTES:
            oversize.append(path.name)
            flag = "  ← OVER BUDGET"
        print(f"{path}  ({size / 1024:.0f} KB){flag}")
    print(f"{len(written)} capture(s) written to {out_dir}")
    if oversize:
        # Say so here rather than letting tests/test_readme_assets.py be the
        # first thing that mentions it, several minutes later in CI.
        print(
            f"{len(oversize)} capture(s) over the {MAX_IMAGE_BYTES // 1024} KB "
            f"per-image budget: {', '.join(oversize)}",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
