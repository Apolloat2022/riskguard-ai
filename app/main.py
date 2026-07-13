"""FastAPI application factory. The DB engine and the ML model are both
constructed once in the lifespan (not at import time) and torn down on
shutdown; route dependencies read them off app.state."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.graph import init_agent_graph, shutdown_agent_graph
from app.api.remediation import router as remediation_router
from app.api.risk import router as risk_router
from app.config import settings
from app.db.engine import build_engine, build_session_factory
from app.exceptions import register_exception_handlers
from app.logging_conf import configure_logging
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
    app = FastAPI(title="RiskGuard AI", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(risk_router)
    app.include_router(remediation_router)
    return app


app = create_app()
