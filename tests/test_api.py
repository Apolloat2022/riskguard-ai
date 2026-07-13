"""API-level integration tests: httpx.AsyncClient against the real app (real
lifespan, real DB) with a fake Anthropic client injected so the remediation
flow doesn't require real Bedrock credentials. Test rows are created
directly and cleaned up per-test; nothing here touches the seeded demo data.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest_asyncio
from sqlalchemy import delete, select

from app.agent import llm as agent_llm
from app.db.engine import build_engine, build_session_factory
from app.db.models import AuditLog, Customer, RemediationCase, RiskAssessment
from app.main import app


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


@pytest_asyncio.fixture
async def api_client():
    agent_llm.set_client(FakeAnthropicClient())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
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


async def _wait_for_status(api_client, case_id: str, status: str, *, tries: int = 20) -> dict:
    body = {}
    for _ in range(tries):
        resp = await api_client.get(f"/api/v1/remediation/{case_id}")
        body = resp.json()
        if body["status"] == status:
            return body
        await asyncio.sleep(0.25)
    return body


async def test_post_risk_assessment_404_for_nonexistent_customer(api_client):
    resp = await api_client.post("/api/v1/risk-assessment/999999999")
    assert resp.status_code == 404


async def test_get_risk_assessment_404_when_none_exists(api_client, low_risk_customer_id):
    resp = await api_client.get(f"/api/v1/risk-assessment/{low_risk_customer_id}")
    assert resp.status_code == 404


async def test_post_risk_assessment_low_risk_no_remediation_case(api_client, low_risk_customer_id):
    resp = await api_client.post(f"/api/v1/risk-assessment/{low_risk_customer_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["customer_id"] == low_risk_customer_id
    assert body["remediation_case_id"] is None
    assert body["risk_flag"] in ("LOW", "MEDIUM")

    resp2 = await api_client.get(f"/api/v1/risk-assessment/{low_risk_customer_id}")
    assert resp2.status_code == 200
    # The stored value is rounded to 5 decimal places (see app/api/risk.py);
    # the POST response returns the raw unrounded probability.
    assert resp2.json()["default_probability"] == round(body["default_probability"], 5)


async def test_full_remediation_flow_via_api(api_client, high_risk_customer_id):
    resp = await api_client.post(f"/api/v1/risk-assessment/{high_risk_customer_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["risk_flag"] == "CRITICAL"
    case_id = body["remediation_case_id"]
    assert case_id is not None

    paused = await _wait_for_status(api_client, case_id, "AWAITING_HUMAN_REVIEW")
    assert paused["status"] == "AWAITING_HUMAN_REVIEW"
    assert paused["remediation_plan"] is not None

    resp = await api_client.post(f"/api/v1/remediation/{case_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "APPROVED"
    assert resp.json()["resolved_at"] is not None


async def test_reject_then_approve_flow(api_client, high_risk_customer_id):
    resp = await api_client.post(f"/api/v1/risk-assessment/{high_risk_customer_id}")
    case_id = resp.json()["remediation_case_id"]
    await _wait_for_status(api_client, case_id, "AWAITING_HUMAN_REVIEW")

    resp = await api_client.post(
        f"/api/v1/remediation/{case_id}/reject",
        json={"notes": "Please reconsider the forbearance length."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AWAITING_HUMAN_REVIEW"  # revised plan re-paused for review
    assert body["revision_count"] == 1

    resp = await api_client.post(f"/api/v1/remediation/{case_id}/approve")
    assert resp.json()["status"] == "APPROVED"


async def test_approve_404_for_nonexistent_case(api_client):
    resp = await api_client.post(f"/api/v1/remediation/{uuid.uuid4()}/approve")
    assert resp.status_code == 404


async def test_approve_409_when_not_awaiting_review(api_client, db, high_risk_customer_id):
    # Insert a case directly in a status other than AWAITING_HUMAN_REVIEW —
    # deterministic, rather than racing the background task.
    async with db() as session:
        assessment = RiskAssessment(
            customer_id=high_risk_customer_id,
            model_version="test",
            default_probability=Decimal("0.95"),
            risk_flag="CRITICAL",
            features_snapshot={},
        )
        session.add(assessment)
        await session.flush()
        case = RemediationCase(
            customer_id=high_risk_customer_id,
            risk_assessment_id=assessment.id,
            status="PENDING_CONTEXT",
        )
        session.add(case)
        await session.commit()
        case_id = str(case.id)

    resp = await api_client.post(f"/api/v1/remediation/{case_id}/approve")
    assert resp.status_code == 409
