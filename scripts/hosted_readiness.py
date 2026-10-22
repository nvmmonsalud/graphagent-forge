"""Authenticated HTTPS operator probe. Prints fixed result codes, never response bodies."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from scripts.hosted_config import ConfigError, load_config, read_private_file


class ProbeError(Exception):
    """A fixed diagnostic code only."""


class InputError(Exception):
    """Invalid command line; never echo supplied arguments."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError


def check(identifier, passed, reason="probe_failed"):
    return {"id": identifier, "status": "pass" if passed else "fail",
            "reason": "verified" if passed else reason}


def result(checks):
    return {"version": 1, "ok": bool(checks) and all(
        item["status"] == "pass" for item in checks), "checks": checks}


def read_token(path):
    raw = read_private_file(path, max_bytes=513)
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not 32 <= len(raw) <= 512 or any(byte < 33 or byte > 126 for byte in raw):
        raise ConfigError("invalid_credential")
    return raw.decode("ascii")


def _workspace(body, expected):
    if not isinstance(body, dict):
        return False
    selected, workspaces = body.get("workspace"), body.get("workspaces")
    return (isinstance(selected, dict) and selected.get("id") == expected
            and selected.get("role") == "reader" and isinstance(workspaces, list)
            and len(workspaces) == 1 and isinstance(workspaces[0], dict)
            and workspaces[0].get("id") == expected and workspaces[0].get("role") == "reader")


def _cookies_safe(cookies, origin):
    host = urlsplit(origin).hostname
    by_name = {cookie.name: cookie for cookie in cookies.jar}
    if set(by_name) != {"ga_session", "ga_workspace"}:
        return False
    for cookie in by_name.values():
        rest = {key.lower(): value for key, value in cookie._rest.items()}
        if (not cookie.secure or cookie.domain_specified or cookie.path != "/"
                or cookie.domain != host or "httponly" not in rest
                or str(rest.get("samesite", "")).lower() != "strict"):
            return False
    return 32 <= len(by_name["ga_session"].value) <= 256


async def probe(config, token, *, transport=None):
    """Only session creation/revocation and provider-free authenticated reads."""
    checks = []
    origin = config["origin"]
    # Ignore operator proxy/netrc variables; verify normal system TLS trust and
    # never follow redirects with a credential or cookie.
    async with httpx.AsyncClient(
        base_url=origin, timeout=httpx.Timeout(10), follow_redirects=False,
        trust_env=False, verify=True, transport=transport,
    ) as client:
        async def request(method, path, **kwargs):
            try:
                async with (asyncio.timeout(15),
                            client.stream(method, path, **kwargs) as response):
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise ProbeError("response_too_large")
                    return response.status_code, response.headers, bytes(body)
            except (httpx.HTTPError, TimeoutError):
                raise ProbeError("transport_failed") from None

        async def document(method, path, **kwargs):
            status, headers, body = await request(method, path, **kwargs)
            if status != 200:
                raise ProbeError("http_status")
            if "no-store" not in headers.get("cache-control", "").lower():
                raise ProbeError("cache_policy")
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError, RecursionError):
                raise ProbeError("invalid_response") from None
            if not isinstance(value, dict):
                raise ProbeError("invalid_response")
            return value

        current = "private_auth"
        try:
            status = await document("GET", "/api/auth/status")
            if status.get("private") is not True:
                raise ProbeError("private_auth_required")
            checks.append(check(current, True))
            current = "reader_session"
            session = await document("POST", "/api/auth/session", headers={"Origin": origin},
                                     json={"token": token, "workspace_id": config["workspace_id"]})
            if not _workspace(session, config["workspace_id"]):
                raise ProbeError("scoped_reader_required")
            if not _cookies_safe(client.cookies, origin):
                raise ProbeError("secure_cookies_required")
            checks.append(check(current, True))
            current = "identity_roundtrip"
            identity = await document("GET", "/api/auth/workspaces")
            if not _workspace(identity, config["workspace_id"]):
                raise ProbeError("identity_unavailable")
            checks.append(check(current, True))
            current = "graph_read"
            stats = await document("GET", "/api/graph/stats")
            if any(type(stats.get(key)) is not int or stats[key] < 0 for key in ("nodes", "edges")):
                raise ProbeError("graph_unavailable")
            checks.append(check(current, True))
            current = "durable_usage"
            usage = await document("GET", "/api/usage")
            if (usage.get("storage_mode") != "sqlite"
                    or usage.get("accounting_available") is not True
                    or type(usage.get("request_limit")) is not int
                    or usage["request_limit"] != config["usage_request_limit"]
                    or type(usage.get("window_seconds")) is not int
                    or usage["window_seconds"] != config["usage_window_seconds"]):
                raise ProbeError("durable_quota_unavailable")
            checks.append(check(current, True))
        except ProbeError as exc:
            checks.append(check(current, False, exc.args[0]))
        except Exception:
            checks.append(check(current, False, "probe_failed"))
        finally:
            # Save the pre-logout cookie, then verify it is rejected after revoke.
            # Cookies/headers are never included in the report or an exception.
            issued = {cookie.name: cookie.value for cookie in client.cookies.jar}
            if issued.get("ga_session"):
                cleanup_ok = False
                try:
                    code, _, body = await request("DELETE", "/api/auth/session",
                                                  headers={"Origin": origin})
                    cleanup_ok = code == 200 and json.loads(body).get("ok") is True
                    client.cookies.clear()
                    old_cookie = "; ".join(f"{name}={value}" for name, value in issued.items())
                    code, _, _ = await request("GET", "/api/auth/workspaces",
                                               headers={"Cookie": old_cookie})
                    cleanup_ok = cleanup_ok and code == 401
                except Exception:
                    cleanup_ok = False
                checks.append(check("session_revoked", cleanup_ok, "revocation_unverified"))
    return result(checks)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument("--config", required=True, type=Path)
        parser.add_argument("--credential-file", required=True, type=Path)
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv)
        config = load_config(args.config)
        token = read_token(args.credential_file)
    except (InputError, ConfigError, OSError, ValueError):
        output = result([check("configuration", False, "invalid_private_inputs")])
    else:
        try:
            output = asyncio.run(probe(config, token))
        except Exception:
            output = result([check("readiness", False, "probe_failed")])
    print(json.dumps(output, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
