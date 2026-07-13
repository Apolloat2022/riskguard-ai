"""End-to-end tests for the LangGraph remediation workflow.

Uses a fake Anthropic client (no real Bedrock call — see app/agent/llm.py's
set_client() test seam) but a real database, since the graph's correctness
here is inseparable from its DB-persisted status transitions and audit
trail. Rows are created against, and cleaned up from, the same DATABASE_URL
configured for the rest of the app.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import delete, select

from app.agent import llm as agent_llm
from app.agent.graph import init_agent_graph, run_case, shutdown_agent_graph
from app.db.engine import build_engine, build_session_factory
from app.db.models import AuditLog, Customer, RemediationCase, RiskAssessment


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
    def __init__(self, plans: list[str]):
        self._plans = list(plans)
        self.calls = 0

    def create(self, **kwargs):
        text = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return _FakeResponse(text)


class FakeAnthropicClient:
    """Injected via app.agent.llm.set_client() — draft_remediation_plan()
    calls client.messages.create(...) exactly like the real SDK client."""

    def __init__(self, plans: list[str]):
        self.messages = _FakeMessages(plans)


_COMPLIANT_CA_PLAN = (
    "RATE_REDUCTION_BPS: 200\n"
    "FORBEARANCE_MONTHS: 6\n"
    "TERM_EXTENSION_MONTHS: 12\n"
    "POLICY_CITATION: CA-1. Rate Reduction Cap\n"
    "SUMMARY: We are reducing your rate and offering a short forbearance period.\n"
)

# Exceeds CA's 400bps rate cap and 15-month forbearance cap (app/rag/documents/policy_CA.md).
_NONCOMPLIANT_CA_PLAN = (
    "RATE_REDUCTION_BPS: 1000\n"
    "FORBEARANCE_MONTHS: 30\n"
    "TERM_EXTENSION_MONTHS: 12\n"
    "POLICY_CITATION: CA-1. Rate Reduction Cap\n"
    "SUMMARY: An aggressive plan that exceeds policy limits.\n"
)


@pytest_asyncio.fixture(autouse=True)
async def agent_graph():
    # Function-scoped to match pytest-asyncio's per-test event loop — a
    # module-scoped engine/checkpointer would be bound to the first test's
    # loop and break once that loop closes.
    await init_agent_graph()
    yield
    await shutdown_agent_graph()


@pytest_asyncio.fixture
async def db():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def case_id(db):
    async with db() as session:
        customer = Customer(
            external_ref=f"TEST-{uuid.uuid4().hex[:10]}",
            full_name="Test Customer",
            state_code="CA",
            credit_score=550,
            debt_to_income=Decimal("0.55"),
            loan_amount=Decimal("300000.00"),
            employment_duration=Decimal("0.5"),
        )
        session.add(customer)
        await session.flush()

        assessment = RiskAssessment(
            customer_id=customer.id,
            model_version="test",
            default_probability=Decimal("0.95"),
            risk_flag="CRITICAL",
            features_snapshot={
                "credit_score": 550,
                "debt_to_income": 0.55,
                "payment_history": 8,
                "loan_amount": 300000.0,
                "employment_duration": 0.5,
            },
        )
        session.add(assessment)
        await session.flush()

        case = RemediationCase(
            customer_id=customer.id, risk_assessment_id=assessment.id, status="PENDING_CONTEXT"
        )
        session.add(case)
        await session.commit()
        cid, customer_id = str(case.id), customer.id

    yield cid

    async with db() as session:
        await session.execute(delete(AuditLog).where(AuditLog.entity_id == cid))
        await session.execute(delete(RemediationCase).where(RemediationCase.id == uuid.UUID(cid)))
        await session.execute(delete(RiskAssessment).where(RiskAssessment.customer_id == customer_id))
        await session.execute(delete(Customer).where(Customer.id == customer_id))
        await session.commit()


async def test_happy_path_pauses_then_approves(db, case_id):
    agent_llm.set_client(FakeAnthropicClient([_COMPLIANT_CA_PLAN]))
    try:
        await run_case(case_id, db)

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "AWAITING_HUMAN_REVIEW"
            assert case.remediation_plan is not None
            assert "CA-1" in case.remediation_plan

        await run_case(case_id, db, resume={"approved": True, "notes": None})

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "APPROVED"
            assert case.resolved_at is not None

            logs = (
                await session.execute(select(AuditLog).where(AuditLog.entity_id == case_id))
            ).scalars().all()
        actions = [row.action for row in logs]
        for expected in ("RETRIEVE_CONTEXT", "PLAN_REMEDIATION", "COMPLIANCE_CHECK", "FINALIZE"):
            assert expected in actions
        assert any(a.startswith("STATUS_") for a in actions)
    finally:
        agent_llm.set_client(None)


async def test_needs_revision_then_passes(db, case_id):
    agent_llm.set_client(FakeAnthropicClient([_NONCOMPLIANT_CA_PLAN, _COMPLIANT_CA_PLAN]))
    try:
        await run_case(case_id, db)

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "AWAITING_HUMAN_REVIEW"
            assert case.revision_count == 1
            assert "CA-1" in case.remediation_plan
    finally:
        agent_llm.set_client(None)


async def test_human_denial_triggers_revision_then_approves(db, case_id):
    agent_llm.set_client(FakeAnthropicClient([_COMPLIANT_CA_PLAN, _COMPLIANT_CA_PLAN]))
    try:
        await run_case(case_id, db)
        await run_case(
            case_id, db, resume={"approved": False, "notes": "Please shorten the term extension."}
        )

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "AWAITING_HUMAN_REVIEW"
            assert case.revision_count == 1

        await run_case(case_id, db, resume={"approved": True, "notes": None})

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "APPROVED"
    finally:
        agent_llm.set_client(None)


async def test_max_revisions_exhausted_escalates(db, case_id):
    agent_llm.set_client(FakeAnthropicClient([_NONCOMPLIANT_CA_PLAN]))  # never compliant
    try:
        await run_case(case_id, db)

        async with db() as session:
            case = await session.get(RemediationCase, uuid.UUID(case_id))
            assert case.status == "ESCALATED"
            assert case.resolved_at is not None
            assert case.revision_count == 3
    finally:
        agent_llm.set_client(None)
