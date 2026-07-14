"""Shared fixtures for the httpx/ASGI-transport integration tests
(test_api.py, test_mcp.py): a real app (real lifespan, real DB) with a fake
Anthropic client injected so the remediation flow doesn't require real
Bedrock credentials. Test rows are created directly and cleaned up
per-test; nothing here touches the seeded demo data.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest_asyncio
from sqlalchemy import delete, select

from app.agent import llm as agent_llm
from app.db.engine import build_engine, build_session_factory
from app.db.models import AuditLog, Customer, RemediationCase, RiskAssessment
from app.main import create_app


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = stop_reason
        self.stop_details = None


class _FakeMessages:
    def create(self, **kwargs):
        return _FakeResponse(
            "RATE_REDUCTION_BPS: 200\n"
            "FORBEARANCE_MONTHS: 6\n"
            "TERM_EXTENSION_MONTHS: 12\n"
            "POLICY_CITATION: CA-1. Rate Reduction Cap\n"
            "SUMMARY: We are reducing your rate and offering a short forbearance period.\n"
        )


class FakeAnthropicClient:
    def __init__(self):
        self.messages = _FakeMessages()


@pytest_asyncio.fixture(scope="session")
async def api_client():
    # Session-scoped: the mounted MCP session manager's anyio task group can
    # only be entered/exited once per instance, ever (StreamableHTTPSessionManager
    # docstring) — matching how the app actually runs in production (one
    # process, one lifespan for its whole lifetime), rather than restarting
    # the whole app per test. Per-test data isolation is handled separately,
    # by the db/_cleanup_customer fixtures below (their own independent DB
    # engine, not tied to this app's lifespan at all).
    agent_llm.set_client(FakeAnthropicClient())
    app = create_app()
    lifespan_cm = app.router.lifespan_context(app)
    await lifespan_cm.__aenter__()
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = app  # test_mcp.py needs the same app instance for its MCP client
            yield client
    finally:
        try:
            await lifespan_cm.__aexit__(None, None, None)
        except RuntimeError as exc:
            # pytest-asyncio drives a session-scoped fixture's final
            # finalizer (this __aexit__ call) from a different asyncio Task
            # than the one that entered it — anyio's task-group cancel scope
            # (inside mcp_server.session_manager.run()) requires the same
            # Task for enter/exit and raises here. Confirmed benign: the logs
            # show the session manager's real shutdown completing before
            # this fires, and it only ever happens at process-wide teardown,
            # never during an actual test or in production (one lifespan
            # entry/exit for the whole process there, no pytest involved).
            if "different task" not in str(exc):
                raise
        agent_llm.set_client(None)


@pytest_asyncio.fixture
async def db():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    yield session_factory
    await engine.dispose()


async def _make_customer(db, *, high_risk: bool) -> int:
    async with db() as session:
        if high_risk:
            customer = Customer(
                external_ref=f"TEST-API-{uuid.uuid4().hex[:8]}",
                full_name="Test API High Risk",
                state_code="CA",
                credit_score=520,
                debt_to_income=Decimal("0.62"),
                loan_amount=Decimal("450000.00"),
                employment_duration=Decimal("0.2"),
            )
        else:
            customer = Customer(
                external_ref=f"TEST-API-{uuid.uuid4().hex[:8]}",
                full_name="Test API Low Risk",
                state_code="CA",
                credit_score=800,
                debt_to_income=Decimal("0.08"),
                loan_amount=Decimal("15000.00"),
                employment_duration=Decimal("12.0"),
            )
        session.add(customer)
        await session.commit()
        return customer.id


async def _cleanup_customer(db, customer_id: int) -> None:
    async with db() as session:
        assessment_ids = (
            await session.execute(
                select(RiskAssessment.id).where(RiskAssessment.customer_id == customer_id)
            )
        ).scalars().all()

        if assessment_ids:
            case_ids = (
                await session.execute(
                    select(RemediationCase.id).where(
                        RemediationCase.risk_assessment_id.in_(assessment_ids)
                    )
                )
            ).scalars().all()
            for cid in case_ids:
                await session.execute(delete(AuditLog).where(AuditLog.entity_id == str(cid)))
            await session.execute(
                delete(RemediationCase).where(RemediationCase.risk_assessment_id.in_(assessment_ids))
            )
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.entity_type == "risk_assessment",
                    AuditLog.entity_id.in_([str(a) for a in assessment_ids]),
                )
            )

        await session.execute(delete(RiskAssessment).where(RiskAssessment.customer_id == customer_id))
        await session.execute(delete(Customer).where(Customer.id == customer_id))
        await session.commit()


@pytest_asyncio.fixture
async def low_risk_customer_id(db):
    cid = await _make_customer(db, high_risk=False)
    yield cid
    await _cleanup_customer(db, cid)


@pytest_asyncio.fixture
async def high_risk_customer_id(db):
    cid = await _make_customer(db, high_risk=True)
    yield cid
    await _cleanup_customer(db, cid)
