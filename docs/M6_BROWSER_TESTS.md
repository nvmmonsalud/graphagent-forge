# M6 browser regressions: authentication and research interactions

Baseline: `1c3d68b41f8ec9869fb88384a916f060b35e8311`. The requested
`docs/NEXT_MILESTONES.md` was absent at this baseline; this slice follows the
explicit task scope and the M4/M5 contracts. M7–M9 and multiple API workers are
outside this change.

## Run

From the repository root with Python 3.11:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt -r requirements-browser.txt
.venv/bin/python -m playwright install --with-deps chromium
.venv/bin/python -m pytest --browser tests/browser -q
.venv/bin/ruff check src/ scripts/ tests/
.venv/bin/python -m pytest -q
```

`BROWSER_EXECUTABLE=/absolute/path/to/chromium` optionally uses a system Chromium.
CI installs Playwright's matching Chromium. Missing browser tooling fails the
explicit browser run; the default offline suite excludes browser collection
before imports and requires neither Playwright nor Chromium. No frontend build,
JavaScript package manager, API server or database is needed.

## Boundary and coverage

Playwright loads the actual `frontend/index.html` and vendored D3 at a synthetic
loopback origin. HTTP routes fulfill local assets and deterministic synthetic API
responses. WebSocket routes never connect to a server. All external page requests
are aborted, including decorative Google Fonts; unexpected API/socket requests
and uncaught JavaScript errors fail tests. No runtime app import, `.env` load,
provider call, database connection, screenshot, video, trace or storage-state
artifact is involved. Fixtures contain only invented public sample values.

Coverage executes auth boot in local, private reader and private editor modes;
checks initial and asynchronously rendered mutation controls; exercises reader
Ask/compare/browse/restore; dispatches enabled bulk and analytics controls and
checks busy recovery; attempts reader writes through the click and API helpers;
holds auth status pending and tests HTTP/network failure; switches workspaces and
logs out while an old answer is pending, checking cleared panels, fields and
legacy browser storage, graph and socket cleanup. The late-answer case runs with
normal abort and with abort deliberately ineffective to exercise the generation fence. The local boot case guards the disabled-control regression
fixed immediately before this baseline.

These are frontend behavior regressions against stub contracts. They complement
static contracts and server authorization tests; they do not establish real
cookie/CSRF enforcement, backend middleware, CSP, provider correctness, database isolation or complete M6
coverage. Disposable-Neo4j integration and live-provider checks are separate.

## Slice 2: ingestion, evidence and merge recovery

`tests/browser/test_research_flows.py` adds eight executable cases. URL submission
locks all ingest buttons until a terminal event; synthetic WebSocket messages
exercise progress, unrelated-job filtering, cancellation cleanup and late-event
rejection. Successful ingestion displays partial, truncated and graph-capped
coverage; a business failure releases the controls. A durable-job fixture survives
an actual browser page reload without resubmission, then a synthetic polling 404
releases the UI. This checks browser reattachment and lost-job handling, **not**
a real backend restart, durable database recovery or cancellation of provider work.

An answered question opens its citation through the real click handler and
refetches the current excerpt, showing version and offsets. Invented HTML in the
answer, title and excerpt remains text. A subsequent 404 removes the displayed
excerpt and requests a rerun. Separate cases check `llm_unavailable`,
`insufficient_evidence` and HTTP 503 responses, then retry successfully. These
responses are fixtures; no provider availability, citation entailment or backend
source-version validation is established.

Merge recovery submits the selected node IDs through the confirmation dialog,
recovers the control after HTTP failure and retries. On success the UI refetches
the graph and duplicate groups even without a WebSocket merge notification. The
fixture changes from two nodes to one; it does not perform or verify a Neo4j merge.

The harness accepts explicit method/path response overrides and records request
bodies only in memory for assertions. WebSocket messages are injected through
Playwright's intercepted socket. Unexpected traffic still fails teardown and no
transport connects to an API, database or external service. Complete M6 acceptance
still needs a disposable-backend end-to-end run and disposable-Neo4j CI.
