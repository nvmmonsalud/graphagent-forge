"""Identity boundaries are exercised without providers or graph access."""
import hashlib
import json
import os
import time

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.agent.state import StateDatabase
from src.security.auth_routes import router
from src.security.identity import IdentityStore

TOKEN = "a" * 48
OTHER = "b" * 48
ORIGIN = "https://research.example"


def configuration():
    return {"version": 1,
            "workspaces": [{"id": "one", "title": "Research"},
                           {"id": "two", "title": "Other"}],
            "principals": [
                {"id": "alice", "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
                 "memberships": {"one": "editor"}},
                {"id": "bob", "token_sha256": hashlib.sha256(OTHER.encode()).hexdigest(),
                 "memberships": {"two": "reader"}}]}


@pytest.fixture
async def identity(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(configuration()))
    path.chmod(0o600)
    db = StateDatabase(tmp_path / "state.sqlite3")
    await db.initialize()
    store = IdentityStore(db, path, [ORIGIN], secure=False)
    await store.initialize()
    return store


async def test_bearer_workspace_and_role_boundaries(identity):
    alice = await identity.authenticate({"authorization": f"Bearer {TOKEN}"}, {})
    assert (await identity.authorize(alice, "one", write=True))["role"] == "editor"
    with pytest.raises(HTTPException) as denied:
        await identity.authorize(alice, "two")
    assert denied.value.status_code == 403
    bob = await identity.authenticate({"authorization": f"Bearer {OTHER}"}, {})
    assert (await identity.authorize(bob, "two"))["role"] == "reader"
    with pytest.raises(HTTPException):
        await identity.authorize(bob, "two", write=True)
    with pytest.raises(HTTPException):
        await identity.authenticate({"authorization": "Basic invalid"}, {})


async def test_session_hash_expiry_and_revocation(identity):
    cookie, auth = await identity.create_session(TOKEN)
    assert auth["session_id"] != cookie
    assert await identity.authenticate({}, {"ga_session": cookie}) == auth
    rows = await identity.db.run(lambda c: c.execute("SELECT * FROM auth_sessions").fetchall())
    assert cookie not in repr([tuple(row) for row in rows])
    assert TOKEN not in repr([tuple(row) for row in rows])
    await identity.db.run(lambda c: c.execute(
        "UPDATE auth_sessions SET expires=?", (time.time() - 1,)), write=True)
    with pytest.raises(HTTPException):
        await identity.authorize(auth, "one")
    cookie, auth = await identity.create_session(TOKEN)
    await identity.revoke(auth)
    with pytest.raises(HTTPException):
        await identity.authenticate({}, {"ga_session": cookie})


async def test_bearer_rotation_rechecked_for_delivery(identity):
    auth = await identity.authenticate({"authorization": f"Bearer {TOKEN}"}, {})
    assert (await identity.authorize(auth, "one"))["role"] == "editor"
    config = configuration()
    config["principals"][0]["token_sha256"] = "c" * 64
    identity.config_path.write_text(json.dumps(config))
    with pytest.raises(HTTPException) as denied:
        await identity.authorize(auth, "one")
    assert denied.value.status_code == 401
    # Durable authorization intentionally follows the submitting principal's
    # current membership, independent of browser session/token lifetime.
    assert (await identity.authorize_principal("alice", "one"))["role"] == "editor"


async def test_sessions_bounded_and_oldest_evicted(identity, monkeypatch):
    monkeypatch.setattr("src.security.identity.MAX_SESSIONS", 3)
    first_cookie, _ = await identity.create_session(TOKEN)
    for _ in range(3):
        await identity.create_session(TOKEN)
    count = await identity.db.run(
        lambda c: c.execute("SELECT count(*) FROM auth_sessions").fetchone()[0])
    assert count == 3
    with pytest.raises(HTTPException):
        await identity.authenticate({}, {"ga_session": first_cookie})


async def test_session_membership_and_token_reload(identity):
    cookie, auth = await identity.create_session(TOKEN)
    config = configuration()
    config["principals"][0]["memberships"] = {}
    identity.config_path.write_text(json.dumps(config))
    with pytest.raises(HTTPException):
        await identity.authorize(auth, "one")
    config["principals"][0]["token_sha256"] = "c" * 64
    identity.config_path.write_text(json.dumps(config))
    with pytest.raises(HTTPException):
        await identity.authenticate({}, {"ga_session": cookie})


@pytest.mark.parametrize("change", ["mode", "parent", "symlink", "duplicate", "unknown", "size"])
async def test_invalid_config_fails_closed(identity, change):
    path = identity.config_path
    if change == "mode":
        path.chmod(0o644)
    elif change == "parent":
        path.parent.chmod(0o755)
    elif change == "symlink":
        replacement = path.with_name("real.json")
        path.rename(replacement)
        path.symlink_to(replacement)
    elif change == "size":
        path.write_text(" " * 65537)
    else:
        config = configuration()
        if change == "duplicate":
            config["principals"][1]["token_sha256"] = config["principals"][0]["token_sha256"]
        else:
            config["principals"][0]["memberships"]["missing"] = "editor"
        path.write_text(json.dumps(config))
    with pytest.raises(HTTPException) as unavailable:
        await identity.authenticate({"authorization": f"Bearer {TOKEN}"}, {})
    assert unavailable.value.status_code == 503


async def test_csrf_exact_origin_and_authenticated_bearer(identity):
    _, auth = await identity.create_session(TOKEN)
    for origin in (None, "null", ORIGIN + ".evil", "http://research.example"):
        with pytest.raises(HTTPException):
            identity.check_csrf({"origin": origin}, auth)
    identity.check_csrf({"origin": ORIGIN}, auth)
    identity.check_csrf({}, {"session_id": None})
    with pytest.raises(HTTPException):
        identity.check_csrf({}, {"session_id": None}, force=True)


async def test_auth_routes_cookie_lifecycle_and_denials(identity):
    app = FastAPI()
    app.state.identity = identity
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.get("/api/auth/status")).json()["private"] is True
        assert (await client.post("/api/auth/session", json={"token": TOKEN})).status_code == 403
        response = await client.post("/api/auth/session", json={"token": TOKEN},
                                     headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert response.json()["workspace"]["id"] == "one"
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert (await client.get("/api/auth/workspaces")).status_code == 200
        response = await client.post("/api/auth/workspace", json={"workspace_id": "two"},
                                     headers={"Origin": ORIGIN})
        assert response.status_code == 403
        assert (await client.delete("/api/auth/session")).status_code == 403
        response = await client.delete("/api/auth/session", headers={"Origin": ORIGIN})
        assert response.status_code == 200
        assert (await client.get("/api/auth/workspaces")).status_code == 401


async def test_bounded_audit_no_research_payload(identity):
    for _ in range(1005):
        await identity.audit("mutation", "allowed", "alice", "one")
    count = await identity.db.run(
        lambda c: c.execute("SELECT count(*) FROM auth_audit").fetchone()[0])
    assert count == 1000
    with pytest.raises(ValueError):
        await identity.audit("https://private.example/document", "allowed")


async def test_logout_revokes_cookie_even_with_bearer_header(identity):
    cookie, _ = await identity.create_session(TOKEN)
    app = FastAPI()
    app.state.identity = identity
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.delete("/api/auth/session", headers={
            "Origin": ORIGIN, "Authorization": f"Bearer {OTHER}",
            "Cookie": f"ga_session={cookie}",
        })
        assert response.status_code == 200
    with pytest.raises(HTTPException):
        await identity.authenticate({}, {"ga_session": cookie})


async def test_provider_disclosure_strips_credentials_paths_and_queries(identity, monkeypatch):
    monkeypatch.setenv("KIMI_BASE_URL", "https://secret:password@kimi.example:8443/private?key=abc#token")
    monkeypatch.setenv("NOSANA_API_URL", "https://nosana.example/secret-project")
    monkeypatch.setenv("NOSANA_EMBEDDING_URL", "file:///private/key")
    monkeypatch.setenv("DAYTONA_API_URL", "https://daytona.example?secret=hidden")
    app = FastAPI()
    app.state.identity = identity
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        response = await client.get("/api/auth/status")
    assert response.json()["providers"] == {
        "kimi": "https://kimi.example:8443", "nosana": "https://nosana.example",
        "nosana_embeddings": None, "daytona": "https://daytona.example",
    }
    for secret in ("password", "private", "key=", "secret-project", "hidden"):
        # 'private' is a fixed mode field; check provider projection only.
        assert secret not in json.dumps(response.json()["providers"])
