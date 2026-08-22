"""API routes — REST endpoints for the frontend."""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import time
from collections import deque

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

from src.agent.jobs import JobQueueFull
from src.ingestion.file_extractor import MAX_UPLOAD_BYTES, check_upload_shape, safe_filename

log = logging.getLogger(__name__)

router = APIRouter(tags=["graphagent"])

#: Generic 500 detail. Exception text is logged, never returned — raw errors
#: leak bolt URIs, credentials and upstream API metadata to the client.
INTERNAL_ERROR = "internal error"

# `File`/`Form` make FastAPI evaluate multipart support at ROUTE-DEFINITION
# time (i.e. at `import src.api.routes`) — if python-multipart isn't
# installed, registering the route below would raise on import and take the
# whole app down with it, the opposite of this project's graceful-degradation
# pattern (and this repo has already lost python-multipart to a dead-dep
# sweep once). So probe for it explicitly and only register /ingest/file when
# it's present; everything else keeps working, that one route 404s instead.
try:
    import python_multipart  # noqa: F401  (>=0.0.12 module name)

    HAS_MULTIPART = True
except ImportError:
    try:
        import multipart  # noqa: F401  (older releases)

        HAS_MULTIPART = True
    except ImportError:
        HAS_MULTIPART = False
        log.warning("python-multipart not installed — /ingest/file disabled")


# ------------------------------------------------------------------
# Dependencies: availability guard, optional auth, rate limiting
# ------------------------------------------------------------------
async def require_graph(request: Request) -> None:
    """Reject graph-touching requests while Neo4j is unreachable.

    `neo4j_available` is set by the lifespan hook in `src.main`; the app boots
    even when the database is down so /health and the frontend stay usable.
    """
    if not getattr(request.app.state, "neo4j_available", False):
        raise HTTPException(status_code=503, detail="graph database unavailable")


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Optional shared-secret auth for mutating routes.

    Zero-config by design: when the `API_KEY` env var is unset the check is
    skipped entirely. Read at request time so the key can be rotated (or set in
    tests) without re-importing the module.
    """
    expected = os.getenv("API_KEY")
    if not expected:
        return
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


#: Per-IP sliding window. NOTE: in-memory and therefore PER-PROCESS — with
#: multiple uvicorn workers each worker keeps its own counters, so the
#: effective limit is (workers x RATE_LIMIT_MAX). It is a cheap abuse brake for
#: the expensive LLM routes, not a security control; use a shared store
#: (Redis) if this ever needs to be exact.
#: Read once AT IMPORT TIME on purpose: a per-request getenv() would make
#: the limit depend on the shell env at call time, which is both a
#: surprise in tests and a per-request syscall on every hot route.
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "10"))
RATE_LIMIT_WINDOW = 60.0
_MAX_TRACKED_CLIENTS = 4096
_rate_buckets: dict[str, deque] = {}


def _rate_limit_allow(client_ip: str, now: float) -> bool:
    """Record a hit for `client_ip`, returning False when over the limit.

    Pure stdlib and side-effect-local so it can be exercised without FastAPI.
    """
    bucket = _rate_buckets.get(client_ip)
    if bucket is None:
        bucket = _rate_buckets.setdefault(client_ip, deque())
    cutoff = now - RATE_LIMIT_WINDOW
    while bucket and bucket[0] <= cutoff:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX:
        return False
    bucket.append(now)
    # Bound memory: an idle client's timestamps are only popped when that same
    # client calls again, so sweep by staleness (newest hit outside the window)
    # rather than emptiness — otherwise abandoned buckets accumulate forever.
    if len(_rate_buckets) > _MAX_TRACKED_CLIENTS:
        stale = [ip for ip, b in _rate_buckets.items() if not b or b[-1] <= cutoff]
        for ip in stale:
            del _rate_buckets[ip]
    return True


async def rate_limit(request: Request) -> None:
    """10 requests/minute per client IP on the expensive routes."""
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_allow(client_ip, time.monotonic()):
        log.warning("Rate limit exceeded for %s on %s", client_ip, request.url.path)
        raise HTTPException(status_code=429, detail="rate limit exceeded")


GRAPH_DEP = [Depends(require_graph)]
MUTATING_DEP = [Depends(require_api_key), Depends(require_graph)]
INGEST_DEP = [Depends(require_api_key), Depends(rate_limit), Depends(require_graph)]
ASK_DEP = [Depends(rate_limit), Depends(require_graph)]


# ------------------------------------------------------------------
# Request/Response models
# ------------------------------------------------------------------
class IngestURLRequest(BaseModel):
    url: HttpUrl = Field(..., description="URL to ingest")


class IngestTextRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=200_000, description="Text content to ingest"
    )
    source: str = Field(default="manual", max_length=300, description="Source label")


class AskRequest(BaseModel):
    question: str = Field(
        ..., min_length=1, max_length=1000, description="Question to ask the knowledge graph"
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000, description="Search term")


class PathRequest(BaseModel):
    from_label: str = Field(
        ..., min_length=1, max_length=500, description="Source entity label"
    )
    to_label: str = Field(
        ..., min_length=1, max_length=500, description="Target entity label"
    )


class MergeRequest(BaseModel):
    node_ids: list[str] = Field(..., min_length=2, max_length=50)
    canonical_id: str | None = Field(default=None, max_length=300)

    @field_validator("node_ids")
    @classmethod
    def _clean_node_ids(cls, value: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw in value:
            node_id = raw.strip()
            if not node_id or len(node_id) > 300:
                raise ValueError("node_ids entries must be 1..300 characters")
            if node_id not in seen:
                seen.add(node_id)
                deduped.append(node_id)
        if len(deduped) < 2:
            raise ValueError("node_ids must contain at least 2 unique ids")
        return deduped

    @model_validator(mode="after")
    def _canonical_in_node_ids(self) -> MergeRequest:
        if self.canonical_id is not None and self.canonical_id not in self.node_ids:
            raise ValueError("canonical_id must be one of node_ids")
        return self


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------
@router.get("/health")
async def health(request: Request):
    """Always 200 — the body reports whether the graph backend is usable."""
    available = bool(getattr(request.app.state, "neo4j_available", False))
    return {
        "status": "ok" if available else "degraded",
        "service": "graphagent-forge",
        "neo4j": available,
    }


@router.post("/ingest/url", status_code=202, dependencies=INGEST_DEP)
async def ingest_url(req: IngestURLRequest, request: Request, wait: bool = Query(default=False)):
    """Submit a URL ingest job. Returns 202 with a job id, or the ingest
    result synchronously (200) when `wait=true`.
    """
    agent = request.app.state.agent
    jobs = request.app.state.jobs
    # HttpUrl is a pydantic object — the agent expects a plain string.
    url = str(req.url)

    async def run(progress):
        return await agent.ingest_url(url, progress=progress)

    try:
        record = jobs.submit("url", {"url": url}, run)
    except JobQueueFull:
        raise HTTPException(status_code=429, detail="job queue full") from None
    except Exception:
        log.exception("Failed to submit ingest job")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

    if not wait:
        content = {"job_id": record["id"], "status": record["status"]}
        return JSONResponse(status_code=202, content=content)

    try:
        final = await jobs.wait(record["id"])
    except Exception:
        log.exception("Failed waiting for ingest job")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
    if final["status"] != "succeeded":
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR)
    return JSONResponse(status_code=200, content=final["result"])


@router.post("/ingest/text", status_code=202, dependencies=INGEST_DEP)
async def ingest_text(req: IngestTextRequest, request: Request, wait: bool = Query(default=False)):
    """Submit a text ingest job. Returns 202 with a job id, or the ingest
    result synchronously (200) when `wait=true`.
    """
    agent = request.app.state.agent
    jobs = request.app.state.jobs
    text = req.text
    source = req.source

    async def run(progress):
        return await agent.ingest_text(text, source, progress=progress)

    try:
        # The raw text must NOT go into the job's public params — only a
        # length hint, so job listings stay small and don't leak content.
        record = jobs.submit("text", {"source": source, "text_chars": len(text)}, run)
    except JobQueueFull:
        raise HTTPException(status_code=429, detail="job queue full") from None
    except Exception:
        log.exception("Failed to submit ingest job")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

    if not wait:
        content = {"job_id": record["id"], "status": record["status"]}
        return JSONResponse(status_code=202, content=content)

    try:
        final = await jobs.wait(record["id"])
    except Exception:
        log.exception("Failed waiting for ingest job")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
    if final["status"] != "succeeded":
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR)
    return JSONResponse(status_code=200, content=final["result"])


async def _read_upload_capped(file: UploadFile) -> bytes | None:
    """Read an upload up to MAX_UPLOAD_BYTES; never materialize more.

    Honest limit: by the time this handler runs, Starlette has already
    buffered the entire multipart body (spooling to a temp file past 1MB) —
    so this cap bounds our in-process bytes and the job's payload, not the
    network transfer itself. A reverse-proxy body limit is the real front
    door against an oversized upload saturating the connection.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


if HAS_MULTIPART:

    @router.post("/ingest/file", status_code=202, dependencies=INGEST_DEP)
    async def ingest_file(
        request: Request,
        file: UploadFile = File(...),  # noqa: B008 (FastAPI DI pattern; UploadFile isn't a ruff-recognized immutable type)
        source: str | None = Form(default=None, max_length=300),
        wait: bool = Query(default=False),
    ):
        """Submit a file ingest job. Returns 202 with a job id, or the ingest
        result synchronously (200) when `wait=true`.
        """
        agent = request.app.state.agent
        jobs = request.app.state.jobs

        # Validation before reading bytes — reject cheaply on filename/type
        # before paying for the (capped) body read.
        name = safe_filename(file.filename or "")
        if name is None:
            # 422: same meaning pydantic body-validation failures already use
            # in this file — a malformed/missing filename, not a content issue.
            raise HTTPException(status_code=422, detail="invalid filename")
        declared = (file.content_type or "").split(";", 1)[0].strip().lower() or None
        type_error = check_upload_shape(name, declared)
        if type_error:
            # 415: purpose-built "unsupported media type" — the frontend maps
            # this status to a friendly "we can't read that file type" message.
            raise HTTPException(status_code=415, detail=type_error)
        data = await _read_upload_capped(file)
        if data is None:
            # 413: oversize payload.
            raise HTTPException(status_code=413, detail="file too large (max 3 MB)")
        if not data:
            raise HTTPException(status_code=422, detail="empty file")
        source = (source or "").strip() or None

        # The UploadFile is closed once the response is sent — long before a
        # queued job's worker runs — so `run` must close over the bytes
        # already read (`data`), never `file` itself.
        async def run(progress):
            return await agent.ingest_file(
                data, name, content_type=declared, source=source, progress=progress
            )

        try:
            # Job params carry only filename/content_type/size_bytes — never
            # the bytes themselves, since the record is public via GET
            # /api/jobs.
            record = jobs.submit(
                "file", {"filename": name, "content_type": declared, "size_bytes": len(data)}, run
            )
        except JobQueueFull:
            raise HTTPException(status_code=429, detail="job queue full") from None
        except Exception:
            log.exception("Failed to submit ingest job")
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

        if not wait:
            content = {"job_id": record["id"], "status": record["status"]}
            return JSONResponse(status_code=202, content=content)

        try:
            final = await jobs.wait(record["id"])
        except Exception:
            log.exception("Failed waiting for ingest job")
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
        if final["status"] != "succeeded":
            raise HTTPException(status_code=500, detail=INTERNAL_ERROR)
        return JSONResponse(status_code=200, content=final["result"])


# ------------------------------------------------------------------
# Job queue — read endpoints
#
# Deliberately NO dependencies: in-memory only (nothing to gate on Neo4j
# availability), must keep working in degraded mode, and must not consume
# the shared per-IP rate bucket that ingest/ask polling would otherwise
# starve. Unauthenticated like every other GET read in this file.
# ------------------------------------------------------------------
@router.get("/jobs")
async def list_jobs(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    """List submitted jobs, newest first."""
    return {"jobs": request.app.state.jobs.list(limit=limit)}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    """Fetch a single job's status/result by id."""
    record = request.app.state.jobs.get(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="job not found")
    return record


@router.post("/ask", dependencies=ASK_DEP)
async def ask(req: AskRequest, request: Request):
    """Ask a question — GraphRAG retrieves from graph + reasons with Kimi."""
    agent = request.app.state.agent
    try:
        return await agent.ask(req.question)
    except Exception:
        log.exception("Failed to answer question")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.get("/graph/stats", dependencies=GRAPH_DEP)
async def graph_stats(request: Request):
    """Get knowledge graph statistics."""
    agent = request.app.state.agent
    try:
        return await agent.get_graph_stats()
    except Exception:
        log.exception("Failed to read graph stats")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.get("/graph/data", dependencies=GRAPH_DEP)
async def graph_data(
    request: Request,
    source_doc: str | None = Query(default=None, max_length=500),
    # Node cap for the all-sources view. Ignored on the per-source branch,
    # which is already bounded by the document and must stay uncapped to keep
    # its boundary-node guarantee.
    limit: int = Query(default=500, ge=1, le=5000),
):
    """Get graph data (nodes + edges) for visualization, optionally per source."""
    neo4j = request.app.state.neo4j
    try:
        if source_doc:
            return await neo4j.get_graph_data_by_source(source_doc)
        return await neo4j.get_all_graph_data(limit=limit)
    except Exception:
        log.exception("Failed to read graph data (source_doc=%r)", source_doc)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.post("/graph/search", dependencies=GRAPH_DEP)
async def graph_search(req: SearchRequest, request: Request):
    """Search entities in the graph."""
    agent = request.app.state.agent
    try:
        return await agent.search_graph(req.query)
    except Exception:
        log.exception("Failed to search graph")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.get("/graph/verify", dependencies=GRAPH_DEP)
async def graph_verify(request: Request):
    """Run graph-integrity verification inside a Daytona sandbox.

    Fetches the latest graph data from Neo4j, validates every edge's
    source/target exist as node IDs, and computes quality metrics.
    """
    neo4j = request.app.state.neo4j
    agent = request.app.state.agent
    try:
        graph_data = await neo4j.get_all_graph_data(limit=None)
        return await agent.daytona.verify_graph(graph_data)
    except Exception:
        log.exception("Graph verification failed")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.post("/graph/path", dependencies=GRAPH_DEP)
async def graph_path(req: PathRequest, request: Request):
    """Find shortest path between two entities."""
    agent = request.app.state.agent
    try:
        path = await agent.find_path(req.from_label, req.to_label)
        return {"path": path}
    except Exception:
        log.exception("Failed to find path")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.get("/graph/duplicates", dependencies=GRAPH_DEP)
async def graph_duplicates(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    """Find candidate duplicate entity groups for merge review."""
    agent = request.app.state.agent
    try:
        return await agent.find_duplicates(limit=limit)
    except Exception:
        log.exception("Failed to find duplicates")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None


@router.post("/graph/merge", dependencies=MUTATING_DEP)
async def graph_merge(req: MergeRequest, request: Request):
    """Merge a set of duplicate entity nodes into one canonical node."""
    agent = request.app.state.agent
    try:
        result = await agent.merge_entities(req.node_ids, req.canonical_id)
    except Exception:
        log.exception("Failed to merge entities")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

    if result.get("error") == "not_found":
        raise HTTPException(
            status_code=404, detail="nodes not found: " + ", ".join(result.get("missing", []))
        )
    return result


# ------------------------------------------------------------------
# Source management
# ------------------------------------------------------------------
@router.get("/sources", dependencies=GRAPH_DEP)
async def list_sources(request: Request):
    """List ingested source documents with their entity counts."""
    neo4j = request.app.state.neo4j
    try:
        sources = await neo4j.get_sources()
    except Exception:
        log.exception("Failed to list sources")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
    return {"sources": sources}


@router.delete("/sources", dependencies=MUTATING_DEP)
async def delete_source(
    request: Request,
    # Query param, not a path segment: source names are titles/URLs with slashes.
    source_doc: str = Query(..., min_length=1, max_length=500),
):
    """Delete every entity that came from one source document."""
    neo4j = request.app.state.neo4j
    try:
        result = await neo4j.delete_source(source_doc)
    except Exception:
        log.exception("Failed to delete source %r", source_doc)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

    keys = ("deleted_nodes", "removed_memberships", "deleted_edges")
    touched = sum(int(result.get(k, 0)) for k in keys)
    if touched == 0:
        raise HTTPException(status_code=404, detail="source not found")
    return result


@router.post("/graph/clear", dependencies=MUTATING_DEP)
async def clear_graph(request: Request):
    """Delete every entity in the graph."""
    neo4j = request.app.state.neo4j
    try:
        result = await neo4j.clear_graph()
    except Exception:
        log.exception("Failed to clear graph")
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None
    return {"deleted_nodes": int(result.get("deleted_nodes", 0))}


# ------------------------------------------------------------------
# Export
# ------------------------------------------------------------------
CSV_HEADER = ["source_id", "source_label", "type", "target_id", "target_label", "context"]


def _edges_to_csv(nodes: list[dict], edges: list[dict]) -> str:
    """Render edges as CSV, resolving endpoint ids to labels via the node list."""
    labels = {n.get("id"): n.get("label", "") for n in nodes}
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_HEADER)
    for edge in edges:
        source = edge.get("source", "")
        target = edge.get("target", "")
        writer.writerow([
            source,
            labels.get(source, ""),
            edge.get("type", ""),
            target,
            labels.get(target, ""),
            edge.get("context", ""),
        ])
    return buffer.getvalue()


@router.get("/graph/export", dependencies=GRAPH_DEP)
async def graph_export(
    request: Request,
    # Invalid values are rejected as 422 by the pattern constraint.
    fmt: str = Query(default="json", alias="format", pattern="^(json|csv)$"),
):
    """Download the whole graph as JSON (nodes + edges) or CSV (edge list)."""
    neo4j = request.app.state.neo4j
    try:
        data = await neo4j.get_all_graph_data(limit=None)
    except Exception:
        log.exception("Failed to export graph (format=%s)", fmt)
        raise HTTPException(status_code=500, detail=INTERNAL_ERROR) from None

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    if fmt == "csv":
        return Response(
            content=_edges_to_csv(nodes, edges),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=graph-export.csv"},
        )

    return JSONResponse(
        content={"nodes": nodes, "edges": edges},
        headers={"Content-Disposition": "attachment; filename=graph-export.json"},
    )


# ------------------------------------------------------------------
# Exception handlers
# ------------------------------------------------------------------
#: How many field errors to surface. Enough to be useful, few enough that a
#: toast/inline banner stays readable.
_MAX_VALIDATION_ERRORS = 3


def _format_validation_errors(exc: RequestValidationError) -> str:
    """Flatten pydantic's list-of-dicts into one human-readable line.

    FastAPI's default 422 body is `{"detail": [{"loc": [...], "msg": ...}, ...]}`
    and the frontend renders `String(e.detail)` — which stringifies a list of
    dicts to "[object Object]". Only `loc` and `msg` are kept: pydantic's
    `input`/`url` fields echo the submitted value straight back at the client.
    """
    parts: list[str] = []
    for err in exc.errors()[:_MAX_VALIDATION_ERRORS]:
        loc = ".".join(str(part) for part in err.get("loc", ()) if part is not None)
        msg = str(err.get("msg") or "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "invalid request"


def install_exception_handlers(app) -> None:
    """Register app-wide exception handlers on `app`.

    Lives here rather than in `src/main.py` so the handler is reachable from a
    bare `FastAPI()` + this router (how the route tests build their app);
    `src/main.py` calls it right after `include_router`.
    """

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError):
        # Status stays 422 — only the shape of `detail` changes (list -> str).
        return JSONResponse(
            status_code=422,
            content={"detail": _format_validation_errors(exc)},
        )
