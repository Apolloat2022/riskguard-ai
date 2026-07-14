"""API-level integration tests: httpx.AsyncClient against the real app (real
lifespan, real DB) with a fake Anthropic client injected so the remediation
flow doesn't require real Bedrock credentials. Test rows are created
directly and cleaned up per-test; nothing here touches the seeded demo data.

Fixtures (api_client, db, high_risk_customer_id, low_risk_customer_id) live
in conftest.py, shared with test_mcp.py.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from app.db.models import RemediationCase, RiskAssessment


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
