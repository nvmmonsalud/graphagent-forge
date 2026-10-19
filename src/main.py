"""GraphAgent Forge — Main entry point."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load .env before importing app modules so any environment read at import
# time (model names, API endpoints) sees the configured values.
load_dotenv()

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from src.agent.core import GraphAgent  # noqa: E402
from src.agent.history import QueryHistory  # noqa: E402
from src.agent.jobs import JobManager  # noqa: E402
from src.api.routes import install_exception_handlers, router  # noqa: E402
from src.graph.neo4j_client import Neo4jClient  # noqa: E402


# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
# basicConfig is a no-op once handlers exist, so under uvicorn (which installs
# its own handlers first) this simply does nothing — it only takes effect when
# the app is imported bare (tests, `python -c`, embedding in another runner).
def _configure_logging() -> None:
    level = logging.getLevelName(os.getenv("LOG_LEVEL", "INFO").strip().upper())
    if not isinstance(level, int):  # unknown name -> getLevelName returns a str
        level = logging.INFO
    logging.basicConfig(level=level)


_configure_logging()
log = logging.getLogger(__name__)

# Resolve assets against the repo root, not the process CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_INDEX = REPO_ROOT / "frontend" / "index.html"
FRONTEND_VENDOR = REPO_ROOT / "frontend" / "vendor"


# ------------------------------------------------------------------
# WebSocket broadcast manager
# ------------------------------------------------------------------
#: Hard cap on concurrent /ws/graph sockets; extra clients are refused with 1013.
MAX_WS_CONNECTIONS = int(os.getenv("MAX_WS_CONNECTIONS", "100"))
#: A client that doesn't drain its socket within this many seconds is dropped,
#: so one stalled reader can't hold up job progress or merge broadcasts.
WS_SEND_TIMEOUT = 2.0
#: Bound close handshakes as well as sends when evicting an unhealthy client.
WS_CLOSE_TIMEOUT = 2.0


class ConnectionManager:
    """Manages active WebSocket connections for live graph updates."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._pending_connections = 0

    def is_full(self) -> bool:
        return len(self.active_connections) + self._pending_connections >= MAX_WS_CONNECTIONS

    async def connect(self, websocket: WebSocket) -> bool:
        # Check and reserve without yielding: concurrent handshakes must count
        # toward capacity even while accept() is waiting on the transport.
        if self.is_full():
            return False
        self._pending_connections += 1
        try:
            await websocket.accept()
            self.active_connections.append(websocket)
            return True
        finally:
            # Includes failed and cancelled handshakes.
            self._pending_connections -= 1

    def disconnect(self, websocket: WebSocket):
        """Idempotent — safe to call from a `finally` after any failure path."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict[str, Any]):
        """Send graph update to all connected clients."""
        message = json.dumps(data)
        connections = list(self.active_connections)

        async def _send(connection: WebSocket) -> None:
            try:
                await asyncio.wait_for(connection.send_text(message), WS_SEND_TIMEOUT)
            except Exception:
                try:
                    await asyncio.wait_for(connection.close(code=1013), WS_CLOSE_TIMEOUT)
                except Exception:
                    log.debug("Failed to close unhealthy WebSocket", exc_info=True)
                finally:
                    self.disconnect(connection)

        # Concurrent with a per-socket timeout: total cost is bounded by the
        # slowest healthy client, not the sum of every client's latency.
        await asyncio.gather(*(_send(c) for c in connections))


# Module-level singleton: src.agent.core lazily imports this name to broadcast
# ingestion updates without a circular import. Do not rename or move it.
ws_manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — init Neo4j + agent.

    Neo4j failures are non-fatal: the app still boots in a degraded mode where
    `/api/health` reports the outage and graph-touching routes answer 503.
    """
    if not os.getenv("API_KEY"):
        log.warning(
            "API_KEY is not set: ingest, ask, delete/clear and the Daytona sandbox "
            "routes are open to anyone who can reach this server. Keep it bound to "
            "loopback (APP_HOST=127.0.0.1, the default) or set API_KEY."
        )
    neo4j = Neo4jClient(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", ""),
    )

    app.state.neo4j_available = False
    try:
        await neo4j.connect()
        await neo4j.init_schema()
        app.state.neo4j_available = True
        log.info("Neo4j connected and schema initialized")
    except Exception:
        log.exception(
            "Neo4j unavailable — starting in DEGRADED mode. "
            "Graph endpoints will return 503 until the database is reachable."
        )

    # The agent only touches Neo4j on use, so it is always constructed.
    app.state.neo4j = neo4j
    app.state.agent = GraphAgent(neo4j)
    app.state.ws_manager = ws_manager

    # Durable state is opt-in. Initialization failure must stop startup rather
    # than silently replacing persisted work with an empty memory store.
    app.state.storage_mode = "memory"
    app.state.jobs = None
    try:
        state_path = os.getenv("STATE_DB_PATH", "").strip()
        if state_path:
            from src.agent.commands import command_handler
            from src.agent.durable_history import SQLiteQueryHistory
            from src.agent.durable_jobs import DurableJobManager
            from src.agent.state import StateDatabase

            database = StateDatabase(state_path)
            await database.initialize()
            history = SQLiteQueryHistory(database)
            await history.initialize()
            jobs = DurableJobManager(
                database, command_handler(app.state.agent), broadcast=ws_manager.broadcast,
                attempt_budget=int(os.getenv("INGEST_ATTEMPT_BUDGET", "100")),
            )
            app.state.jobs = jobs
            await jobs.initialize()
            await jobs.start()
            app.state.history = history
            app.state.storage_mode = "sqlite"
        else:
            jobs = JobManager(broadcast=ws_manager.broadcast)
            app.state.jobs = jobs
            jobs.start()
            app.state.history = QueryHistory()
        yield
    finally:
        # Drain jobs FIRST: in-flight work still holds the Neo4j driver and the
        # shared httpx clients, so this has to finish before they are closed.
        try:
            if app.state.jobs is not None:
                await app.state.jobs.shutdown()
        except Exception:
            log.exception("Error while shutting down the ingest job queue")
        # Close whenever a driver object was actually created — `connect()` can
        # build the driver and then fail connectivity verification.
        if getattr(neo4j, "driver", None) is not None:
            try:
                await neo4j.close()
            except Exception:
                log.exception("Error while closing the Neo4j driver")
        # Release the shared outbound HTTP clients.
        try:
            from src.ingestion.extractor import aclose_client

            await aclose_client()
            await app.state.agent.nosana.aclose()
        except Exception:
            log.exception("Error while closing HTTP clients")


#: Hard cap on any request body. The largest legitimate body is a 3MB upload
#: plus multipart framing; /ingest/text's 200k chars stays under 1.2MB as JSON.
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class BodySizeLimitMiddleware:
    """Reject request bodies over MAX_REQUEST_BYTES before they're buffered.

    Pydantic's `max_length` and the upload reader's cap only run after
    Starlette has already read the whole body (or spooled the multipart parts
    to disk), and FastAPI parses the body before the API-key dependency. So an
    unauthenticated client could push an arbitrarily large body. A declared
    Content-Length over the cap gets 413 immediately; chunked or lying bodies
    are counted as they stream and aborted with 413 at the cap.
    """

    def __init__(self, app, max_bytes: int = MAX_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.max_bytes:
                    await self._reject(send)
                    return
                break

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await self._reject(send)

    @staticmethod
    async def _reject(send) -> None:
        body = b'{"detail":"request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(HTTPException):
    """Raised from inside `receive`. Subclassing HTTPException matters: FastAPI
    wraps arbitrary body-read errors into a 400, but re-raises HTTPException,
    so the app's exception handling turns this into a proper 413."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail="request body too large")


#: Interactive API docs publish the full route surface and pull Swagger/ReDoc
#: assets from a CDN, so they're opt-in: ENABLE_API_DOCS=1.
API_DOCS_ENABLED = os.getenv("ENABLE_API_DOCS", "0") == "1"

app = FastAPI(
    title="GraphAgent Forge",
    description="Turn scattered information into connected intelligence.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if API_DOCS_ENABLED else None,
    redoc_url="/redoc" if API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if API_DOCS_ENABLED else None,
)

# The UI is one file with inline <script>/<style> and inline onclick handlers,
# so 'unsafe-inline' is unavoidable for now. Everything else is pinned: no
# third-party scripts (D3 is vendored), fonts only from Google Fonts, network
# only to this origin, and the page can't be framed (clickjacking on "Clear graph").
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self' ws: wss:",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
]
_DOCS_PATHS = ("/docs", "/redoc")


class SecurityHeadersMiddleware:
    """Add SECURITY_HEADERS to every HTTP response.

    The CSP is skipped on the opt-in docs pages, which load Swagger/ReDoc from
    a CDN; the other headers still apply there.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        skip_csp = scope.get("path", "").startswith(_DOCS_PATHS)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in SECURITY_HEADERS:
                    if name in present or (skip_csp and name == b"content-security-policy"):
                        continue
                    headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SecurityHeadersMiddleware)
# Added last so it is the outermost middleware and runs before anything reads the body.
app.add_middleware(BodySizeLimitMiddleware)

app.include_router(router, prefix="/api")
# Flattens FastAPI's list-of-dicts 422 body into a plain string so the
# frontend's String(detail) never renders "[object Object]".
install_exception_handlers(app)

# Vendored third-party browser assets (D3). Served locally so the graph
# renders on a network that blocks public CDNs — the visualization is the
# product. Mounted defensively: StaticFiles raises at construction if the
# directory is missing, and a partial checkout must not take the app down.
if FRONTEND_VENDOR.is_dir():
    app.mount("/vendor", StaticFiles(directory=FRONTEND_VENDOR), name="vendor")
else:
    log.warning("Vendored asset directory missing: %s", FRONTEND_VENDOR)


def _ws_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    if origin in ALLOWED_ORIGINS:
        return True
    # Same-origin is always fine (e.g. the UI opened via a LAN IP).
    host = websocket.headers.get("host", "")
    return bool(host) and urlparse(origin).netloc == host


# ------------------------------------------------------------------
# WebSocket endpoint for live graph growth
# ------------------------------------------------------------------
@app.websocket("/ws/graph")
async def websocket_graph(websocket: WebSocket):
    """Push real-time graph updates to connected frontend clients."""
    # Browsers always send Origin on a WebSocket handshake and CORS does not
    # apply to WebSockets, so without this any website could subscribe to job
    # and graph broadcasts. Non-browser clients (no Origin) are allowed.
    if not _ws_origin_allowed(websocket):
        log.warning("Rejected /ws/graph from origin %s", websocket.headers.get("origin"))
        await websocket.close(code=1008)
        return
    if not await ws_manager.connect(websocket):
        log.warning("Rejected /ws/graph: %d connections already open", MAX_WS_CONNECTIONS)
        await websocket.close(code=1013)
        return
    try:
        while True:
            # Keep connection alive; client can send pings or messages
            data = await websocket.receive_text()
            # Echo back or handle client messages if needed
            if data == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        log.debug("WebSocket client disconnected from /ws/graph")
    except Exception:
        log.exception("WebSocket error on /ws/graph")
    finally:
        # Any exit path must drop the socket, or broadcast() keeps retrying it.
        ws_manager.disconnect(websocket)


# Serve frontend HTML
@app.get("/")
async def serve_frontend():
    if not FRONTEND_INDEX.is_file():
        log.error("Frontend entry point missing: %s", FRONTEND_INDEX)
        raise HTTPException(status_code=404, detail="frontend not found")
    return FileResponse(FRONTEND_INDEX)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        # Off by default: a reload restarts the process, so any in-flight
        # ingest job in the in-memory queue is lost (it never reports back).
        # Opt in with DEV_RELOAD=1 while editing code.
        reload=os.getenv("DEV_RELOAD", "0") == "1",
    )
