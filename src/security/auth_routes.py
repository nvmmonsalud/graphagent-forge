"""Cookie login and workspace selection; credentials never enter URLs."""
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from src.security.identity import SESSION_SECONDS

router = APIRouter(prefix="/api/auth", tags=["authentication"])


def _provider_origin(value):
    """Expose a bounded origin, never deployment URL credentials or paths."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or len(host) > 253
                or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)):
            return None
        port = parsed.port
        hostname = f"[{host}]" if ":" in host else host
        return f"{parsed.scheme}://{hostname}" + (f":{port}" if port is not None else "")
    except ValueError:
        return None


def _providers():
    return {
        "kimi": _provider_origin(os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")),
        "nosana": _provider_origin(os.getenv(
            "NOSANA_API_URL", "https://dashboard.k8s.prd.nos.ci/api")),
        "nosana_embeddings": _provider_origin(os.getenv("NOSANA_EMBEDDING_URL", "")),
        "daytona": _provider_origin(os.getenv("DAYTONA_API_URL", "https://app.daytona.io/api")),
    }


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=32, max_length=512, repr=False)
    workspace_id: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,48}$")


class WorkspaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(pattern=r"^[a-z0-9_-]{1,48}$")


def _identity(request):
    identity = getattr(request.app.state, "identity", None)
    if identity is None:
        raise HTTPException(404, "private authentication unavailable")
    return identity


def _cookie(response, identity, name, value):
    response.set_cookie(
        name, value, max_age=SESSION_SECONDS, secure=identity.secure,
        httponly=True, samesite="strict", path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.get("/status")
async def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return {"private": getattr(request.app.state, "identity", None) is not None,
            "providers": _providers()}


@router.post("/session")
async def login(body: SessionInput, request: Request, response: Response):
    identity = _identity(request)
    identity.check_csrf(request.headers, {}, force=True)
    try:
        cookie, auth = await identity.create_session(body.token)
    except HTTPException:
        await identity.audit("login", "denied")
        raise
    try:
        workspaces = await identity.workspaces(auth)
        workspace = (await identity.authorize(auth, body.workspace_id)
                     if body.workspace_id else next(iter(workspaces), None))
        if not workspace:
            raise HTTPException(403, "workspace access denied")
    except BaseException:
        await identity.revoke(auth)
        raise
    # Replace any previous browser session, preventing residual active cookies.
    try:
        previous = await identity.authenticate({}, request.cookies)
        await identity.revoke(previous)
    except HTTPException:
        pass
    _cookie(response, identity, "ga_session", cookie)
    _cookie(response, identity, "ga_workspace", workspace["id"])
    await identity.audit("login", "allowed", auth["principal_id"], workspace["id"])
    return {"workspace": workspace, "workspaces": workspaces}


@router.delete("/session")
async def logout(request: Request, response: Response):
    identity = _identity(request)
    identity.check_csrf(request.headers, {}, force=True)
    try:
        auth = await identity.authenticate(request.headers, request.cookies)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        auth = None
    if auth:
        await identity.revoke(auth)
        await identity.audit("logout", "allowed", auth["principal_id"])
    # A tool may send both Bearer and browser cookies. Authentication deliberately
    # prefers Bearer, but logout must still revoke the cookie being cleared.
    if request.headers.get("authorization") and request.cookies.get("ga_session"):
        try:
            cookie_auth = await identity.authenticate({}, request.cookies)
            await identity.revoke(cookie_auth)
        except HTTPException as error:
            if error.status_code != 401:
                raise
    for key in ("ga_session", "ga_workspace"):
        response.delete_cookie(key, path="/", secure=identity.secure,
                               httponly=True, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}


@router.get("/workspaces")
async def workspaces(request: Request, response: Response):
    identity = _identity(request)
    auth = await identity.authenticate(request.headers, request.cookies)
    memberships = await identity.workspaces(auth)
    selected = request.headers.get("x-workspace-id") or request.cookies.get("ga_workspace")
    workspace = await identity.authorize(auth, selected) if selected else None
    response.headers["Cache-Control"] = "no-store"
    return {"workspace": workspace, "workspaces": memberships}


@router.post("/workspace")
async def select_workspace(body: WorkspaceInput, request: Request, response: Response):
    identity = _identity(request)
    auth = await identity.authenticate(request.headers, request.cookies)
    identity.check_csrf(request.headers, auth, force=True)
    workspace = await identity.authorize(auth, body.workspace_id)
    _cookie(response, identity, "ga_workspace", workspace["id"])
    await identity.audit("switch", "allowed", auth["principal_id"], workspace["id"])
    return {"workspace": workspace, "workspaces": await identity.workspaces(auth)}
