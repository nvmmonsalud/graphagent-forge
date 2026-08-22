"""GraphAgent Forge — Main entry point."""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load .env before importing app modules so any environment read at import
# time (model names, API endpoints) sees the configured values.
load_dotenv()

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from src.agent.core import GraphAgent  # noqa: E402
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


# ------------------------------------------------------------------
# WebSocket broadcast manager
# ------------------------------------------------------------------
class ConnectionManager:
    """Manages active WebSocket connections for live graph updates."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Idempotent — safe to call from a `finally` after any failure path."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, data: dict[str, Any]):
        """Send graph update to all connected clients."""
        message = json.dumps(data)
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)


# Module-level singleton: src.agent.core lazily imports this name to broadcast
# ingestion updates without a circular import. Do not rename or move it.
ws_manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — init Neo4j + agent.

    Neo4j failures are non-fatal: the app still boots in a degraded mode where
    `/api/health` reports the outage and graph-touching routes answer 503.
    """
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

    # Background ingest queue. In-memory and per-process: jobs are lost on
    # restart, and every job_update is pushed over the same /ws/graph socket.
    jobs = JobManager(broadcast=ws_manager.broadcast)
    jobs.start()
    app.state.jobs = jobs

    try:
        yield
    finally:
        # Drain jobs FIRST: in-flight work still holds the Neo4j driver and the
        # shared httpx clients, so this has to finish before they are closed.
        try:
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


app = FastAPI(
    title="GraphAgent Forge",
    description="Turn scattered information into connected intelligence.",
    version="0.1.0",
    lifespan=lifespan,
)

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

app.include_router(router, prefix="/api")
# Flattens FastAPI's list-of-dicts 422 body into a plain string so the
# frontend's String(detail) never renders "[object Object]".
install_exception_handlers(app)


# ------------------------------------------------------------------
# WebSocket endpoint for live graph growth
# ------------------------------------------------------------------
@app.websocket("/ws/graph")
async def websocket_graph(websocket: WebSocket):
    """Push real-time graph updates to connected frontend clients."""
    await ws_manager.connect(websocket)
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
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        # Off by default: a reload restarts the process, so any in-flight
        # ingest job in the in-memory queue is lost (it never reports back).
        # Opt in with DEV_RELOAD=1 while editing code.
        reload=os.getenv("DEV_RELOAD", "0") == "1",
    )
