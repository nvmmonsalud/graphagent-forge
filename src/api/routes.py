"""API routes — REST endpoints for the frontend."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["graphagent"])


# ------------------------------------------------------------------
# Request/Response models
# ------------------------------------------------------------------
class IngestURLRequest(BaseModel):
    url: str = Field(..., description="URL to ingest")


class IngestTextRequest(BaseModel):
    text: str = Field(..., description="Text content to ingest")
    source: str = Field(default="manual", description="Source label")


class AskRequest(BaseModel):
    question: str = Field(..., description="Question to ask the knowledge graph")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search term")


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@router.get("/health")
async def health():
    return {"status": "ok", "service": "graphagent-forge"}


@router.post("/ingest/url")
async def ingest_url(req: IngestURLRequest, request: Request):
    """Ingest a URL into the knowledge graph."""
    agent = request.app.state.agent
    try:
        result = await agent.ingest_url(req.url)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ingest/text")
async def ingest_text(req: IngestTextRequest, request: Request):
    """Ingest raw text into the knowledge graph."""
    agent = request.app.state.agent
    try:
        result = await agent.ingest_text(req.text, req.source)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ask")
async def ask(req: AskRequest, request: Request):
    """Ask a question — GraphRAG retrieves from graph + reasons with Kimi."""
    agent = request.app.state.agent
    try:
        result = await agent.ask(req.question)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/graph/stats")
async def graph_stats(request: Request):
    """Get knowledge graph statistics."""
    agent = request.app.state.agent
    try:
        return await agent.get_graph_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/graph/data")
async def graph_data(request: Request):
    """Get full graph data (nodes + edges) for visualization."""
    neo4j = request.app.state.neo4j
    try:
        return await neo4j.get_all_graph_data()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/graph/search")
async def graph_search(req: SearchRequest, request: Request):
    """Search entities in the graph."""
    agent = request.app.state.agent
    try:
        return await agent.search_graph(req.query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
