"""Bulk imports and explicit URL refresh management."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agent.jobs import JobQueueFull
from src.api.routes import INGEST_DEP, _resources, require_api_key

router = APIRouter(prefix="/api/research", tags=["research jobs"])


class BulkInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[dict] = Field(min_length=1, max_length=10)


class RefreshInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    interval_seconds: int = Field(ge=3600, le=30 * 86400, strict=True)
    source_id: str | None = Field(default=None, max_length=500)


def _service(request):
    service = getattr(_resources(request), "research_jobs", None)
    if service is None:
        raise HTTPException(503, "research jobs unavailable")
    return service


def _principal(request):
    auth = getattr(request.state, "auth", None)
    return auth["principal_id"] if auth else None


@router.post("/bulk", status_code=202, dependencies=INGEST_DEP)
async def bulk(body: BulkInput, request: Request):
    try:
        return await _service(request).bulk(
            body.commands, request.headers.get("Idempotency-Key"), _principal(request))
    except ValueError:
        raise HTTPException(422, "invalid bulk import") from None


@router.get("/batches", dependencies=[Depends(require_api_key)])
async def batches(request: Request):
    return {"batches": await _service(request).list_batches()}


@router.post("/batches/{batch_id}/cancel", dependencies=[Depends(require_api_key)])
async def cancel_batch(batch_id: str, request: Request):
    result = await _service(request).cancel_batch(batch_id)
    if result is None:
        raise HTTPException(404, "batch unavailable")
    return result


@router.post("/refreshes", status_code=201, dependencies=INGEST_DEP)
async def create_refresh(body: RefreshInput, request: Request):
    try:
        return await _service(request).create_refresh(
            body.url, body.interval_seconds, body.source_id, _principal(request))
    except ValueError:
        raise HTTPException(422, "invalid refresh schedule") from None


@router.get("/refreshes", dependencies=[Depends(require_api_key)])
async def refreshes(request: Request):
    return {"refreshes": await _service(request).list_refreshes()}


@router.post("/refreshes/{refresh_id}/run", status_code=202, dependencies=INGEST_DEP)
async def run_refresh(refresh_id: str, request: Request):
    try:
        result = await _service(request).run_refresh(refresh_id)
    except JobQueueFull:
        raise HTTPException(429, "job queue full") from None
    if result is None:
        raise HTTPException(404, "refresh unavailable")
    if result is False:
        raise HTTPException(409, "refresh already being submitted")
    return result


@router.delete("/refreshes/{refresh_id}", dependencies=[Depends(require_api_key)])
async def delete_refresh(refresh_id: str, request: Request):
    result = await _service(request).delete_refresh(refresh_id)
    if result is None:
        raise HTTPException(404, "refresh unavailable")
    return result
