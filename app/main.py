"""FastAPI application factory. The DB engine and the ML model are both
constructed once in the lifespan (not at import time) and torn down on
shutdown; route dependencies read them off app.state."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from fastapi import FastAPI

from app.agent.graph import init_agent_graph, shutdown_agent_graph
from app.api.remediation import router as remediation_router
from app.api.risk import router as risk_router
from app.config import settings
from app.db.engine import build_engine, build_session_factory
from app.exceptions import register_exception_handlers
from app.logging_conf import configure_logging
from app.mcp import build_mcp_server
from ml.predict import RiskModel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)

    engine = build_engine()
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.risk_model = RiskModel(settings.model_artifact_dir)
    await init_agent_graph()

    logger.info(
        "startup complete",
        extra={"model_version": app.state.risk_model.model_version},
    )
    try:
        yield
    finally:
        await shutdown_agent_graph()
        await engine.dispose()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    # Two-phase construction: build_mcp_server(app) needs the FastAPI
    # instance to already exist (its tools close over app.state), so the
    # combined lifespan below can't be passed to FastAPI(...) up front —
    # it's assigned onto app.router.lifespan_context afterward instead.
    app = FastAPI(title="RiskGuard AI")
    register_exception_handlers(app)
    app.include_router(risk_router)
    app.include_router(remediation_router)

    mcp_server = build_mcp_server(app)
    app.state.mcp_server = mcp_server  # lets tests reach it directly
    # Mounted at "/", not "/mcp" — mcp_server.streamable_http_app() already
    # answers at /mcp internally (FastMCP's default streamable_http_path);
    # mounting *that* app at "/mcp" would double the prefix to "/mcp/mcp".
    app.mount("/", mcp_server.streamable_http_app())

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI):
        async with lifespan(app):
            async with AsyncExitStack() as stack:
                # A mounted sub-application's lifespan never runs, so the
                # MCP session manager must be entered explicitly here.
                app.state.mcp_http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://mcp-internal",
                    )
                )
                await stack.enter_async_context(mcp_server.session_manager.run())
                yield

    app.router.lifespan_context = combined_lifespan
    return app


app = create_app()
