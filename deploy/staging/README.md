# Private staging template

This is an **unbound, unexecuted deployment template**, not an accepted host or
release. Keep the public Vercel preview separate. See
[the H1 contract](../../docs/HOSTED_BACKEND_CONTRACT.md) for remaining acceptance.
`compose.yaml` uses JSON syntax (valid YAML) so offline policy tests need no new
YAML dependency. Never merge it with the development Compose file.

## Bind before running

Copy `operator.example.json` to an operator-owned mode-0700 directory outside the
repository, set the copy to mode 0600, and fill **every** binding. The example
intentionally fails validation until populated; it contains no chosen host or
credential. Keep generated probe credentials in a separate private file.

Record operator bindings in the private preflight JSON. Choose immutable app,
Neo4j **5.26** and **nginx stable** image references including `@sha256:` digests;
do not invent digest values or infer compatibility from a passing offline test.
The proxy template requires the standard nginx image's `nginx`, `envsubst`,
`/bin/sh` and `nginx` user **UID/GID 101:101**. Prove the selected digest starts
successfully and has that identity; do not substitute an image with different IDs.

Export the corresponding nonsecret Compose inputs in a private operator session:

| Input | Binding |
|---|---|
| `STAGING_PROJECT` | `compose_project` |
| `STAGING_APP_IMAGE`, `STAGING_NEO4J_IMAGE`, `STAGING_PROXY_IMAGE` | `images.app`, `.neo4j`, `.proxy` |
| `STAGING_HOSTNAME` | DNS hostname from exact HTTPS `origin` |
| `STAGING_OPERATOR_IP` | exact `probe_address`, no CIDR/range |
| `STAGING_STATE_DIR`, `STAGING_NEO4J_DIR`, `STAGING_CONTROL_DIR`, `STAGING_TLS_DIR` | matching absolute paths |
| `STAGING_USAGE_REQUEST_LIMIT`, `STAGING_USAGE_WINDOW_SECONDS` | matching positive quota/window |
| `STAGING_APP_NEO4J_ENV_FILE`, `STAGING_NEO4J_ENV_FILE` | absolute private credential files, mode 0600 |

Create these directories deliberately before deployment; Compose must not create
missing bind sources. State is local SQLite-WAL-capable storage, directory mode
0700 and UID/GID 1000; bootstrap `auth.json` and identity/state files offline,
mode 0600. Neo4j `/data` is a separate private directory owned by 7474:7474.
Admission control is mode 0700, owned by nginx's worker UID/GID 101:101; the
optional empty `admitted` marker is mode 0600 with the same owner. A root-owned
0700 control directory prevents the worker from seeing the marker and keeps
maintenance enabled. Do not loosen permissions to work around an owner mismatch.
The app credential file contains only `NEO4J_PASSWORD`; the separate Neo4j file
contains only `NEO4J_AUTH`, combining `neo4j/` with that same unique password.
Do not give Neo4j the app's password variable as an unsupported database setting.
Generate/write the files through a
private file operation, never a command argument or repository file. Never share
`docker compose config`, `docker inspect`, environment dumps or logs: they expose
resolved credentials. Rotating Neo4j credentials requires the database's actual
password rotation procedure; changing initial-auth environment alone is insufficient.

Provide `fullchain.pem` and `privkey.pem` in the private TLS directory. Bind a
domain, trusted certificate issuer, renewal owner and tested renewal/reload
procedure first. This template deliberately exposes no port 80 and performs no
ACME issuance/renewal. Prove the exact host/DNS/HTTPS origin matches the certificate.
Keep the fixed `172.30.63.0/24` subnet free; changing it requires updating the
proxy target and application's exact trusted proxy address together.

## Admission and lifecycle

Only proxy port 443 is published. Application and Neo4j use only an internal
Docker network; default provider egress is denied. Proxy joins a separate ingress
network; it cannot be used as a forward proxy. No provider credentials or test
fixture are enabled. Verify the actual host firewall, Docker isolation, DNS and
external port reachability; a manifest test does not establish these properties.

Without `/control/admitted`, nginx returns 503 to normal clients. The single
operator source IP bypasses maintenance **only**, not application authentication.
Use a separately restricted operator network/route and a dedicated scoped reader
probe credential. Never use a shared NAT address, forwarded header or secret URL
as the bypass. Confirm nginx sees the operator's actual source IP on the chosen
host; do not add trusted-proxy/real-IP wildcards to make probes pass.

Keep the marker absent during initial startup, recovery and changes. Stop
admission first, stop the old API and verify it exited, then start exactly one new
API process. The app has a 180-second graceful shutdown budget and 210-second
container stop budget; Neo4j has 120 seconds, proxy 30 seconds. Measure these on
the bound host with enabled workspace counts; resource bounds are starting limits,
not proven sizing. Restart policies are intentionally disabled.

The operator runs authenticated readiness and removes admission on failure.
Only after every mandatory readiness check passes may the operator create the
empty `admitted` marker. Remove it immediately if graph/state checks fail. This
is **manual admission**, with no automatic monitor or continuous readiness lease;
the template cannot claim H1-10 acceptance until sustained failure handling is
implemented and demonstrated. A healthcheck 200 must never create the marker.
Stop research schedules/API writers before coordinated backup; backup/restore and
rollback rehearsal remain separate unimplemented acceptance work.

## Diagnostics

Container logging and nginx access/error logging are disabled to prevent private
request/token content in artifacts. Use sanitized operator-tool reason codes and
inspect only necessary diagnostics privately. This trades routine observability
for a provider-free staging boundary; define and review safe diagnostics before
claiming operational readiness. Run preflight against the **actual** stopped or
running topology as documented by the tool, not a resolved configuration dump.
