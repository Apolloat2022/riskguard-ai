"""GET /healthz — unauthenticated liveness probe for the ALB target group.

Deliberately does not touch Postgres or Bedrock. This is a liveness check, not
a readiness check: a transient Neon blip should surface as a 503 on the actual
endpoints (see DatabaseUnavailableError), not cause ECS to kill and replace
otherwise-healthy tasks.

Exempt from auth (app/auth.py EXEMPT_PATHS) because the ALB health check can't
present a credential. It is therefore public — keep it free of anything
sensitive; model_version is already published in the startup logs.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


# Registered on both spellings on purpose. FastAPI would normally redirect
# "/healthz/" to "/healthz", but app/main.py mounts the MCP app at "/", and the
# mount catches every unmatched path before the redirect can happen — so the
# trailing-slash form 404s instead. The ALB health-check path is typed by hand
# into the target group, and a stray slash there would fail every probe and
# have ECS replace healthy tasks. One extra decorator removes that failure mode.
@router.get("/healthz")
@router.get("/healthz/", include_in_schema=False)
async def healthz(request: Request) -> dict:
    risk_model = getattr(request.app.state, "risk_model", None)
    return {
        "status": "ok",
        "model_version": risk_model.model_version if risk_model is not None else None,
    }
