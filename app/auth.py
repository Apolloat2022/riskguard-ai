"""Shared-secret authentication for every public surface.

Why a pure-ASGI middleware rather than router dependencies or BaseHTTPMiddleware:

* Router dependencies would miss `/mcp`. app/main.py mounts the MCP app at
  "/", so it serves every path the routers don't claim — only middleware sits
  in front of both.
* BaseHTTPMiddleware wraps the response stream, which is exactly what the MCP
  streamable-HTTP/SSE transport needs left alone. This middleware never touches
  the response: on success it delegates untouched, and it only constructs a
  response when it is rejecting the request outright.

The internal MCP -> REST loopback is authenticated like any other caller (see
InternalAuth below). The tools in app/mcp/server.py reach the REST routes
through the same ASGI app via app.state.mcp_http_client, so those calls pass
through this middleware too — without a credential, every MCP tool would 401
itself.

Auth is active only when settings.api_key is set. Leaving it unset disables
enforcement (this is what keeps local runs and the test suite working) and logs
a warning at startup from app/main.py — deployments must set API_KEY.
"""

from __future__ import annotations

import logging
import secrets
import uuid

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Unauthenticated by necessity: the ALB target group health-checks this path,
# and a 401 here would mark every task unhealthy and trigger replacement.
# Keep it to endpoints that expose nothing — /healthz reports liveness only.
EXEMPT_PATHS = frozenset({"/healthz"})

_BEARER_PREFIX = "bearer "


def _header(scope, name: bytes) -> str | None:
    """ASGI scope headers are a list of (lowercased-name, value) byte pairs."""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _presented_key(scope) -> str | None:
    """`Authorization: Bearer <key>` is canonical (it is what MCP clients send);
    `X-API-Key: <key>` is accepted as a convenience for curl and probes."""
    authorization = _header(scope, b"authorization")
    if authorization and authorization[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
        return authorization[len(_BEARER_PREFIX) :].strip()
    return _header(scope, b"x-api-key")


class ApiKeyAuthMiddleware:
    """Rejects requests without a valid shared secret before they reach routing."""

    def __init__(self, app, *, exempt_paths=EXEMPT_PATHS):
        self.app = app
        self.exempt_paths = frozenset(exempt_paths)

    def _is_exempt(self, scope) -> bool:
        """Exempt-path match, tolerant of a trailing slash.

        Paired with the matching route alias in app/api/health.py: exempting
        `/healthz/` here only turns a 401 into a 404 unless the route also
        exists. Both are needed for a hand-typed health-check path with a stray
        slash to actually work rather than fail a different way.
        """
        path = scope.get("path") or ""
        return path in self.exempt_paths or path.rstrip("/") in self.exempt_paths

    async def __call__(self, scope, receive, send):
        # Only "http" is guarded. "lifespan" must pass through, and there are
        # no websocket routes — see test_auth.py's canary, which fails if one
        # is ever added, because a websocket would bypass this entirely.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read the key per-request rather than capturing it at construction:
        # the middleware is built once, but tests flip settings.api_key per case.
        expected = settings.api_key
        if not expected or self._is_exempt(scope):
            await self.app(scope, receive, send)
            return

        presented = _presented_key(scope)
        if presented is not None and secrets.compare_digest(
            # Compared as bytes, not str: compare_digest raises TypeError on a
            # str containing non-ASCII, and a header value is attacker-supplied
            # bytes. Comparing as str let an unauthenticated caller turn a 401
            # into a 500 with a stack trace. Bytes have no such restriction, and
            # the constant-time property is unchanged.
            presented.encode("utf-8", "surrogateescape"),
            expected.encode("utf-8", "surrogateescape"),
        ):
            await self.app(scope, receive, send)
            return

        await self._reject(scope, receive, send)

    async def _reject(self, scope, receive, send) -> None:
        # Imported lazily so this module stays importable without the [api]
        # extra, matching app/exceptions.py's convention.
        from fastapi.responses import JSONResponse

        request_id = _header(scope, b"x-request-id") or str(uuid.uuid4())
        logger.warning(
            "unauthenticated request rejected",
            extra={"path": scope.get("path"), "request_id": request_id},
        )
        response = JSONResponse(
            status_code=401,
            content={
                "error_code": "unauthorized",
                "message": "Missing or invalid API key.",
                "request_id": request_id,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


class InternalAuth(httpx.Auth):
    """Credential for the in-process MCP -> REST client.

    Reads settings.api_key inside auth_flow rather than capturing it when the
    client is constructed: app/main.py builds that client once per lifespan,
    so a captured value would go stale the moment the key changed (which is
    what the auth tests do).
    """

    def auth_flow(self, request):
        key = settings.api_key
        if key:
            request.headers["Authorization"] = f"Bearer {key}"
        yield request
