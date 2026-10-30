# Private local backend on an Apple Silicon Mac

Run from a checkout on the Mac, with Docker Desktop running and Python 3.11+
installed. Allocate at least 6 GB of Docker Desktop memory; 8 GB is preferable.
The images select the host architecture automatically; no amd64 override is used.
Initial builds download public packages/images. No paid provider keys are enabled.

```sh
./deploy/macos/start.sh
```

Open **http://127.0.0.1:8000**. In Finder, use Go → Go to Folder and open:

`~/Library/Application Support/GraphAgent Forge/local-backend`

Use the contents of `editor-token.txt` in the login form. Do not paste the token
into this chat or commit it. The launcher never prints tokens/passwords, and
restarts preserve credentials. Partial/unsafe config is refused, not repaired by
silently generating a new database password.

This runs the original private UI, HTTP API and WebSockets together on loopback,
with one API process, one editor workspace, and a separate Neo4j container.
Docker-managed named volumes keep SQLite on the Docker VM's Linux disk rather
than a macOS bind mount. A one-shot initializer sets UID 1000 and private modes
inside the state volume; the running app is non-root. Neo4j ports are not published.
App/database network is internal; remote ingestion and providers are unavailable.
Missing Kimi is reported as unavailable, not a successful answer. Local graph
analytics and hash embeddings remain available. No synthetic preview shim is used.

Cookies use the existing explicit HTTP loopback development exception. **Do not
publish this setup on a LAN or the Internet.** This is not H1 hosted/TLS acceptance.
The independent Vercel static preview remains unchanged. A sleeping Mac is offline;
Docker Desktop storage and credentials need backups. Coordinated SQLite/Neo4j
backup/restore is not implemented by this launcher. Never delete a volume to
repair startup; `down --volumes` destroys retained research and identity state.

For the status/stop commands, set the nonsecret configuration path first:

```sh
export GRAPHAGENT_LOCAL_CONFIG="$HOME/Library/Application Support/GraphAgent Forge/local-backend"
```

Stop (data retained):

```sh
docker compose -p graphagent-mac-private -f deploy/macos/compose.yaml stop
```

Check container status without dumping configuration or secrets:

```sh
docker compose -p graphagent-mac-private -f deploy/macos/compose.yaml ps
```

Run `start.sh` again to rebuild/start. It does not use a developer .env file or
persist provider credentials. Do not run `docker compose config` without `--quiet`,
`docker inspect`, or share logs: resolved credential environment can be exposed.

Local Linux validation of preparation/manifest does not prove Docker Desktop
ARM64 image builds or actual Mac browser behavior. Run login/logout, restart
persistence and local graph health checks on the Mac before adding research data.
