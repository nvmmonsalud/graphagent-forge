# M4 private workspace contract

Baseline1b9233e on M3 PR22; separate M4 PR, then M5 PR. Preserve explicit default
local mode. AUTH_MODE=private requires STATE_DB_PATH and AUTH_CONFIG_PATH; missing,
invalid or unsupported settings fail startup. Local mode remains backwards
compatible, but private mode never touches or exposes legacy unscoped graph data.

## Identity

Admin-managed config JSON (owned regular file0600 in private directory):
{"version":1,"workspaces":[{"id":"research","title":"Research"}],
"principals":[{"id":"alice","token_sha256":"64 lowercase hex",
"memberships":{"research":"editor"}}]}. IDs lowercase[a-z0-9_-],1..48;<=8
workspaces,<=100principals. No duplicate IDs/hashes or unknown workspace membership.
No plaintext tokens in config/logs; operator-generated random256-bit token supplied
once through login or Authorization: Bearer. Config reloaded for every auth/role
check, bounded64KB, invalid reload fails closed. Membership removal invalidates
future HTTP/WS delivery; durable commands retain submitter for execution checks.

src/security/identity.py IdentityStore(db,config_path,allowed_origins,secure=True):
async initialize(), authenticate(headers,cookies)->dict with principal_id/session_id
(session_id mayNone forbearer), authorize(auth,workspace_id,write=False)->membership
{id,title,role}; workspaces(auth)->list memberships; config/workspace metadata
helpers as needed. HTTPException401/403 sanitized. Rootmiddleware uses thisAPI.
Session cookies ga_session opaque, hashed in SQLite; HttpOnly, SameSiteStrict,
Secure default; explicit AUTH_COOKIE_SECURE=0 only loopback development, docs.
8h expiry, bounded sessions, revoke on logout. Cookies contain no provider keys.
Bearer authentication supports nonbrowser tools. No tokens in querystrings.

src/security/auth_routes.py router mounted /api/auth: GET/status public returns
{private:bool}; POST/session {token:str,workspace_id?:str} creates cookie and
returns memberships/current workspace; DELETE/session revokes/clears bothcookies;
GET/workspaces returns authorized list; POST/workspace {workspace_id} switches
cookie ga_workspace after authorization. Resources accessed viaapp.state.identity.
Cookie-auth unsafe methods require exact configured Origin; login/logout/switch
also require Origin. Bearer calls authenticated before bypassing cookieCSRF.
Private contextvar private_authorized lets legacy require_api_key dependency
skip redundant sharedkey; privateMiddleware already enforces principal+role.

## Workspace resources and graph isolation

src/security/workspaces.py WorkspaceRuntime(identity,state_path,...) owns bounded
resource map. get(workspace_id)-> resource object {neo4j,agent,jobs,history,
ws_manager,neo4j_available,storage_mode:'sqlite'}. Independent StateDatabase in
stateparent/workspaces/<trustedID>/state.sqlite3, histories/events/keys/limits
separate. Workspace graph uses SAME server but all known node labels, relationship
types, index/constraint names and vector index literals get deterministic SHA256
workspace namespace. No legacy Entity/Source/GraphState/receipt leakage. Never
accept user Cypher or a user-defined namespace. Scoped driver rewrites only
owned static query tokens; all wildcard path traversal must stay in namespace.
Test real Neo4j5.26 with same node/source/chunk IDs in two partitions, graph reads,
export, retrieval, receipt checks, merge/delete/clear and schema/indexes. App
credentials still grant database access; namespace is application enforcement,
not a Neo4j per-user database privilege boundary.

GraphAgent(...,ws_manager=None) uses injected workspace manager for ALL broadcasts
in private mode; local lazy-global behavior preserved. Each workspace manager
checks current session+membership before every send and disconnects revoked or
expired subscriptions. WS handshake authenticates cookies orAuthorizationheaders,
uses ga_workspace cookie (noURLcredentials), exact Origin allowlist, then authorizes
beforeaccept. Receive loop rechecks within<=5sec and before ping replies. Logout
revokes sessions; no unauthenticated WS acceptance/private graphjobbroadcasts.

All /api except health/auth use privateMiddleware BEFORE route/provider work.
Workspace selected from X-Workspace-ID header or ga_workspace cookie and validated
for everyrequest. Bind resources on request.state.resources; routes use helper
selecting bound resources, fallback app.state ONLY in explicitlocalmode.
Reader permits GET plus POST/ask,/graph/search,/graph/path; sandbox reads spend
compute and requireeditor. Allother mutations requireeditor. Deny unknownmethods
and private unauthorized requests beforebody/provider/DBwork. StaticUI/status and
health contain no research content. Responses no-store, auth/workspace errors
sanitized; privateCORS scoped configured origins, credentials allowed only there.

Private commands carry optional _principal_id,_workspace_id added server-side
before durable submit. Validator accepts either BOTH or neither, preserves them;
private dispatcher authorizes current editor membership before execution and each
progress stage, then strips internalfields before legacy explicitdispatcher.
On revocation, subsequent stages abort as cancellation (not swallowed ordinary
progressException), no provider retrieval starts for unauthorized queuedcommands.
Cancellation cannot retract already committed graphpublication; documentrace.

Audit rootSQLite bounded1000 events: timestamp, principal/workspace IDs, action,
result only, no tokens/URLs/document/questiontext. Record login/logout/switch,
denials, accepted mutations and offline purge. Workspace deletion is OFFLINE
operator purge of a disabled configworkspace, under an exclusive filesystem lock;
runtime holds sharedlock for enabled resources. Purge clears namespaced graph,
SQLite history/jobs/events/commands; metadataaudit retention explicit. No unsafe
livepurge pretending to cancel all concurrently executing HTTPrequests.

## UI and acceptance

PrivateUI login token input, workspace switch, role label, logout. No private
credentials or research history in localStorage; clear legacy key/history/cache
and all visiblegraph/query/evidence panels onlogin/logout/workspacechange. Close
oldWS before switch, reconnect onlyafterauthorized selection. Reader mutationUI
controls disabled; server enforcement remains authoritative. Show configured
provider destinations/data retention beforeprivateingestion. OptionalStreamlit
supports Bearer + X-Workspace-ID viaenvironment; no cookie/tokenURLstorage.

Tests: two principals and two workspaces with guessedIDs/direct/indirectqueries,
exports, history/job/event replay, requestauth beforeLLM, readerdenials, strictCSRF,
WSunauth/revocation/expiry/crossworkspace, sharedbrowser logout/switch no cacheleak.
Independent securityreview exactcommit required. Defaultlocal tests preserved.
