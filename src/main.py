"""GraphAgent Forge — Main entry point."""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from src.agent.core import GraphAgent
from src.api.routes import router
from src.graph.neo4j_client import Neo4jClient

load_dotenv()


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
            try:
                self.active_connections.remove(conn)
            except ValueError:
                pass


ws_manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle — init Neo4j + agent."""
    neo4j = Neo4jClient(
        uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_USER", "neo4j"),
        password=os.getenv("NEO4J_PASSWORD", ""),
    )
    await neo4j.connect()
    await neo4j.init_schema()
    app.state.neo4j = neo4j
    app.state.agent = GraphAgent(neo4j)
    app.state.ws_manager = ws_manager
    yield
    await neo4j.close()


app = FastAPI(
    title="GraphAgent Forge",
    description="Turn scattered information into connected intelligence.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


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
        ws_manager.disconnect(websocket)


# Serve frontend HTML
@app.get("/")
async def serve_frontend():
    return FileResponse("frontend/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
    )
