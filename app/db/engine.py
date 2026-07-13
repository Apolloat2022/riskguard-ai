"""Async engine/session factories.

The engine itself is *not* created at import time — it's built by the FastAPI
lifespan (app/main.py, Phase 4) and disposed on shutdown, and stored on
app.state so it survives across requests without leaking connections at
process start for every module that happens to import this file (tests,
scripts, CLI tools). Standalone scripts (scripts/seed_db.py) call
build_engine() themselves and manage their own lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


def build_engine(database_url: str | None = None) -> AsyncEngine:
    """Neon-appropriate defaults: pool_pre_ping guards against Neon's idle
    connection recycling, and the pool is intentionally modest — Neon's own
    pooler (pgbouncer) handles burst concurrency."""
    return create_async_engine(
        database_url or settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — pulls the session factory the lifespan stashed on
    app.state, so tests can swap in a different engine/DB without touching
    this module."""
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session
