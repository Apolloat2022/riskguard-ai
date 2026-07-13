"""Domain exceptions and their FastAPI error-envelope handlers.

Every handled error returns the same stable shape:
    {"error_code": "...", "message": "...", "request_id": "..."}
so API consumers can branch on error_code without parsing prose.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


class RiskGuardError(Exception):
    """Base for all domain errors. Subclasses fix error_code and http_status."""

    error_code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class DatabaseUnavailableError(RiskGuardError):
    """Postgres connection/query failed (sqlalchemy.exc.OperationalError, asyncpg errors)."""

    error_code = "database_unavailable"
    http_status = 503


class ModelInferenceError(RiskGuardError):
    """The XGBoost pipeline failed to produce a prediction."""

    error_code = "model_inference_failed"
    http_status = 500


class PolicyRetrievalError(RiskGuardError):
    """The RAG retriever failed to load or score policy documents."""

    error_code = "policy_retrieval_failed"
    http_status = 500


class LLMTimeoutError(RiskGuardError):
    """The Bedrock/Anthropic call exceeded its timeout after the retry."""

    error_code = "llm_timeout"
    http_status = 502


class LLMRefusalError(RiskGuardError):
    """The model declined the request (stop_reason == 'refusal') or returned no usable content."""

    error_code = "llm_refusal"
    http_status = 502


def register_exception_handlers(app) -> None:
    """Wires RiskGuardError (and any uncaught Exception) to the stable error envelope.

    Imports FastAPI lazily so this module has no hard dependency on the [api]
    extra — it's imported by ml/agent code paths that don't need FastAPI.
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse

    def _request_id(request: Request) -> str:
        return request.headers.get("x-request-id", str(uuid.uuid4()))

    @app.exception_handler(RiskGuardError)
    async def _handle_riskguard_error(request: Request, exc: RiskGuardError) -> JSONResponse:
        request_id = _request_id(request)
        logger.error(
            "%s: %s", exc.error_code, exc.message,
            extra={"error_code": exc.error_code, "request_id": request_id},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"error_code": exc.error_code, "message": exc.message, "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = _request_id(request)
        logger.exception("unhandled_exception", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "internal_error",
                "message": "An unexpected error occurred.",
                "request_id": request_id,
            },
        )
