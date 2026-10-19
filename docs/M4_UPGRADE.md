# M4: private workspace mode

M4 adds opt-in authenticated workspaces; explicit local mode remains the default.
Private mode isolates every graph/source/evidence/receipt, job command/event,
history and live broadcast before retrieval/provider work. It does not migrate
or expose the old unscoped graph. Keep one API process and local SQLite disk;
M3's remaining token/dollar accounting and shared-worker delivery are unchanged.

## Configure identities and storage

Set `AUTH_MODE=private`, `STATE_DB_PATH=data/identity.sqlite3`, and
`AUTH_CONFIG_PATH=data/auth.json`. Use HTTPS with exact `ALLOWED_ORIGINS` such as
`https://research.example.com`; wildcard origins, userinfo, paths and empty lists
are rejected. Secure cookies are the default. `AUTH_COOKIE_SECURE=0` is accepted
only with HTTP loopback origins for development. Secure cookies require HTTPS.
Private startup fails closed on missing/invalid settings or identity state.

Provision an initial workspace and principal offline:

```bash
python -m scripts.manage_workspaces init --config data/auth.json \
  --token-output data/bootstrap-token.txt --workspace research --principal alice
# Review dry-run output, then repeat with --apply.
```

The command creates mode0600 config and a separate one-time token file in a
private mode0700 directory. It never prints the token and refuses overwrite.
Protect the token file; give the token to its intended principal through your
normal secure channel. The config stores SHA256 hashes, never plaintext tokens.
For Compose, provision INSIDE the app-state volume (`docker compose run --rm app
python -m scripts.manage_workspaces ...`); a host data directory is not that volume.
The app image includes this operator CLI. Never commit config, token or state files.

Config schema1 contains up to eight workspaces and100 principals:

```json
{"version":1,"workspaces":[{"id":"research","title":"Research"}],
 "principals":[{"id":"alice","token_sha256":"<64 lowercase hex characters>",
 "memberships":{"research":"editor"}}]}
```

Add principals with separately generated random256-bit tokens/hashes. IDs are
lowercase letters/digits/underscore/hyphen,1–48 characters. `reader` may inspect,
search, ask and export within its memberships. `editor` may also ingest, edit,
save/delete history and execute sandbox analytics. The server enforces roles;
disabled UI controls are only an aid. Update configuration atomically with the
same0600 permissions. Each request and broadcast checks current configuration;
removing membership or rotating a token revokes subsequent access, including
persistent WebSockets. Invalid reloads deny access instead of retaining old grants.

## Sessions, browser and tools

The primary UI shows a token login form and authorized workspace selector. Browser
sessions use opaque expiring HttpOnly/SameSiteStrict cookies (eight hours); only
hashes are stored server-side. Cookie mutations require configured Origin. Sandbox
GETs also require an allowed Origin or browser `Sec-Fetch-Site: same-origin`.
Private WebSockets require authenticated membership and a configured Origin;
credentials are never placed in URLs. Session logout revokes the server session.

Nonbrowser clients use `Authorization: Bearer <principal-token>` and
`X-Workspace-ID: research`. Bearer requests do not use cookie CSRF. Optional
Streamlit accepts `GRAPHAGENT_TOKEN` and `GRAPHAGENT_WORKSPACE` environment values.
Legacy API_KEY is for local mode; it does not replace private authentication.

Private login/logout/switch clears legacy browser credentials, history, graph,
query/evidence and job panels; private history is not mirrored in localStorage.
Old requests and sockets are fenced before the new workspace renders. Auth-changing
requests are serialized to prevent delayed Set-Cookie responses restoring an old
session after logout. The login screen discloses sanitized configured provider
origins. Planned ingestion chunks/answer excerpts may go to Kimi; semantic text
may go to configured Nosana, and verification/analytics may use Daytona. Private
commands retain submitter identity and recheck editor membership at execution,
each stage, each chunk launch and before publication. Already-started provider
work or a graph commit winning a revocation/cancellation race cannot be retracted.

## Isolation and operations

Each workspace has a private SQLite file below the identity database's parent,
`workspaces/<workspace-id>/state.sqlite3`. Bounds, idempotency keys, history and
events are separate. The graph client namespaces all owned labels, relationship
types, indexes/constraints and vector index names using a server-derived SHA256
workspace namespace. Traversals constrain intermediate nodes, including malformed
cross-partition links. No arbitrary Cypher is accepted. Database credentials are
still privileged operator credentials: this is application enforcement, not Neo4j
per-user database authorization. Do not give untrusted users direct database access.

The root identity database stores bounded1000 audit events containing only IDs,
action/result and time. No token, URL, document or question text is audited.
Workspace resources hold a shared filesystem lock while running. To delete a
workspace, remove ALL its memberships, stop affected workers, and run:

```bash
python -m scripts.manage_workspaces purge --config data/auth.json \
  --state data/identity.sqlite3 --workspace research
# Then use --apply after checking the disabled workspace and backups.
```

Purge refuses enabled memberships or a live runtime lock. It removes namespaced
graph data (including receipts) and known workspace SQLite files. The root audit
keeps a metadata-only purge entry; config entries/credential hashes are managed
separately. Logical deletion is not secure erasure of WAL/free pages/backups.
Coordinate backups of identity/session data, each workspace state and Neo4j;
restore together with matching configuration. Never run old unscoped workers
against private workspace data or switch back to local mode expecting private
workspaces to appear. This release does not add SSO, invitations, password reset,
row-level principal histories within a shared workspace, or multi-host operation.

## Validation

Two-principal tests cover guessed IDs, exports, indirect graph queries, evidence,
history/job/events, reader mutation denials, strict CSRF, expired/revoked sessions,
token rotation and WebSocket delivery. Real Neo4j5.26 tests use identical IDs in
two namespaces and exercise publication, retrieval, traversal, merge/delete/clear,
receipts and injected crosslinks. Chromium checks login gating, workspace switches,
no private cache, reader controls, stale response fencing and delayed-cookie logout.
Independent code and security reviews gate the release PR; no paid providers are
used for validation.
