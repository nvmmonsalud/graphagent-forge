"""Authenticate and select workspace resources before any private API work."""
from __future__ import annotations

import logging
import os

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)
_READ_POSTS = {'/api/ask', '/api/graph/search', '/api/graph/path'}
_EDITOR_GETS = {'/api/graph/verify', '/api/graph/analytics'}


class WorkspaceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        app = scope['app']
        mode = getattr(app.state, 'auth_mode', os.getenv('AUTH_MODE', 'local'))
        path = scope.get('path', '')
        if mode != 'private' or not path.startswith('/api/') or path == '/api/health':
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message['type'] == 'http.response.start':
                headers = [(key, value) for key, value in message.get('headers', [])
                           if key.lower() not in (b'cache-control', b'pragma')]
                message = {**message, 'headers': headers + [
                    (b'cache-control', b'no-store'), (b'pragma', b'no-cache'),
                    (b'vary', b'Cookie, Authorization, X-Workspace-ID'),
                ]}
            await send(message)

        if scope['method'] == 'OPTIONS':
            return await self.app(scope, receive, private_send)
        if path.startswith('/api/auth/'):
            return await self.app(scope, receive, private_send)
        identity = getattr(app.state, 'identity', None)
        runtime = getattr(app.state, 'workspace_runtime', None)
        if identity is None or runtime is None:
            return await JSONResponse({'detail': 'private workspace unavailable'}, status_code=503)(
                scope, receive, private_send)
        from src.security.identity import private_authorized

        token = None
        auth = None
        workspace_id = None
        try:
            headers = Headers(scope=scope)
            request = Request(scope)
            auth = await identity.authenticate(headers, request.cookies)
            workspace_id = headers.get('X-Workspace-ID') or request.cookies.get('ga_workspace')
            method = scope['method'].upper()
            write = method not in ('GET', 'HEAD') and not (
                method == 'POST' and path in _READ_POSTS)
            write = write or path in _EDITOR_GETS
            await identity.authorize(auth, workspace_id, write=write)
            if method not in ('GET', 'HEAD', 'OPTIONS'):
                identity.check_csrf(headers, auth)
            elif write and auth.get('session_id') is not None:
                if headers.get('origin'):
                    identity.check_csrf(headers, auth)
                elif headers.get('sec-fetch-site') != 'same-origin':
                    raise HTTPException(403, 'request origin required')
            resources = await runtime.get(workspace_id)
            state = scope.setdefault('state', {})
            state.update(resources=resources, auth=auth, workspace_id=workspace_id)
            token = private_authorized.set(True)
            if write:
                await identity.audit('mutation', 'accepted', auth['principal_id'], workspace_id)
            await self.app(scope, receive, private_send)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                try:
                    await identity.audit('request', 'denied',
                                         auth['principal_id'] if auth else None, workspace_id)
                except Exception:
                    log.exception('Failed to record authorization audit')
            await JSONResponse({'detail': exc.detail}, status_code=exc.status_code)(
                scope, receive, private_send)
        except Exception:
            log.exception('Private workspace request failed')
            await JSONResponse({'detail': 'private workspace unavailable'}, status_code=503)(
                scope, receive, private_send)
        finally:
            if token is not None:
                private_authorized.reset(token)
