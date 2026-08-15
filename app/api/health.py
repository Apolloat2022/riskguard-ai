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


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    risk_model = getattr(request.app.state, "risk_model", None)
    return {
        "status": "ok",
        "model_version": risk_model.model_version if risk_model is not None else None,
    }
