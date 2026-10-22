# Vercel deployment boundary

The connected `nvm-projects/graphagentforge` project publishes a **static synthetic
frontend preview**, not the GraphRAG API. It displays two fixture nodes and one
fixture edge. A permanent banner identifies that data; answers, ingestion,
verification, authentication and other backend actions remain unavailable.
No provider, database, remote sandbox or browser credential/history store is used.

## Build and verify

```bash
python3 scripts/build_vercel_preview.py --commit <full-40-character-git-sha>
python3 -m pytest -q tests/test_vercel_preview.py
python3 -m pytest --browser tests/browser/test_vercel_preview.py -q
```

The output directory must be empty. The build copies only `frontend/index.html`,
`frontend/preview.js`, vendored D3 and its attribution, and a commit manifest.
Never deploy the repository root as a static directory. `vercel.json` sets the
build command and `public` output explicitly. The preview rejects every write
and unknown API call with503; its Content Security Policy forbids connections,
including when the preview script fails to load. Generated files are release
artifacts, not source files to commit.

The native Git connection targets `nvmmonsalud/graphagent-forge`, branch `main`.
Automatic production domain assignment is disabled: native Git builds are
staged, and promotion is a separate post-CI step. This is not fully automatic
gated promotion. Native GitHub Deployment Checks can replace that manual gate
when configured in Vercel Settings → Build and Deployment → Deployment Checks;
require all CI jobs before enabling automatic assignment.

Release only the exact commit whose lint, offline, browser, security and
disposable-Neo4j CI checks passed. Stage production without assigning domains,
verify the manifest SHA and the page, script and vendored asset responses, then
promote that tested deployment. Keep existing deployment protection enabled.
A READY build alone does not verify those assets or backend availability.

## Full backend remains separate work

Current [Vercel FastAPI documentation](https://vercel.com/docs/frameworks/backend/fastapi)
supports lifespan hooks; shutdown cleanup is limited to500ms. Vercel also
[supports Python WebSockets](https://vercel.com/docs/functions/websockets), with
bounded connection duration and external state coordination across instances.

This app currently requires one API process with local persistent SQLite and
in-process queues, schedules, quota counters and broadcasts; see
[M3_UPGRADE.md](M3_UPGRADE.md). Deploying that backend as autoscaled functions
would violate these contracts. A full deployment requires a persistent backend
host with a durable volume and Neo4j, or an explicit shared-state/worker migration.
Provider credentials and paid execution require their own configured environment
and authorization. This static release does not establish production GraphRAG,
private-workspace or live-provider acceptance.
