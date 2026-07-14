"""MCP-layer integration tests: drives the real streamable-HTTP protocol
in-process over the same ASGITransport api_client already uses (no real
sockets), mirroring test_api.py's REST scenarios through the MCP tools/
resource instead. Fixtures (api_client, db, high_risk_customer_id,
low_risk_customer_id) live in conftest.py.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult
from pydantic import AnyUrl


def _tool_result_json(result: CallToolResult) -> dict:
    """This SDK version doesn't populate structuredContent for a plain dict
    return from a tool — the dict is still there as JSON text content."""
    return json.loads(result.content[0].text)


@asynccontextmanager
async def _mcp_session(api_client):
    """A ClientSession wired to the same app api_client already stood up,
    over the same in-process ASGI transport (no real sockets)."""
    app = api_client.app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        async with streamable_http_client("http://test/mcp", http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _wait_for_status(session: ClientSession, case_id: str, status: str, *, tries: int = 20) -> dict:
    body = {}
    for _ in range(tries):
        result = await session.call_tool("get_remediation_case", {"case_id": case_id})
        body = _tool_result_json(result)
        if body["status"] == status:
            return body
        await asyncio.sleep(0.25)
    return body


async def test_assess_loan_risk_tool_matches_rest_response(api_client, low_risk_customer_id):
    rest_resp = await api_client.get(f"/api/v1/risk-assessment/{low_risk_customer_id}")
    assert rest_resp.status_code == 404  # nothing scored yet for this fresh customer

    async with _mcp_session(api_client) as session:
        result = await session.call_tool("assess_loan_risk", {"customer_id": low_risk_customer_id})
        assert result.isError is False
        body = _tool_result_json(result)
        assert body["customer_id"] == low_risk_customer_id
        assert body["risk_flag"] in ("LOW", "MEDIUM")
        assert body["remediation_case_id"] is None

        # Now the REST GET agrees with what the tool just persisted.
        rest_resp = await api_client.get(f"/api/v1/risk-assessment/{low_risk_customer_id}")
        assert rest_resp.status_code == 200
        assert rest_resp.json()["risk_flag"] == body["risk_flag"]


async def test_assess_loan_risk_tool_404_for_nonexistent_customer(api_client):
    async with _mcp_session(api_client) as session:
        result = await session.call_tool("assess_loan_risk", {"customer_id": 999999999})
        assert result.isError is True
        assert "404" in result.content[0].text


async def test_full_remediation_flow_via_mcp_tools(api_client, high_risk_customer_id):
    async with _mcp_session(api_client) as session:
        result = await session.call_tool(
            "assess_loan_risk", {"customer_id": high_risk_customer_id}
        )
        body = _tool_result_json(result)
        assert body["risk_flag"] == "CRITICAL"
        case_id = body["remediation_case_id"]
        assert case_id is not None

        paused = await _wait_for_status(session, case_id, "AWAITING_HUMAN_REVIEW")
        assert paused["status"] == "AWAITING_HUMAN_REVIEW"
        assert paused["remediation_plan"] is not None

        result = await session.call_tool(
            "resume_remediation_case", {"case_id": case_id, "approved": True}
        )
        assert result.isError is False
        assert _tool_result_json(result)["status"] == "APPROVED"


async def test_resume_remediation_case_409_when_not_awaiting_review(
    api_client, db, high_risk_customer_id
):
    from decimal import Decimal

    from app.db.models import RemediationCase, RiskAssessment

    async with db() as session_db:
        assessment = RiskAssessment(
            customer_id=high_risk_customer_id,
            model_version="test",
            default_probability=Decimal("0.95"),
            risk_flag="CRITICAL",
            features_snapshot={},
        )
        session_db.add(assessment)
        await session_db.flush()
        case = RemediationCase(
            customer_id=high_risk_customer_id,
            risk_assessment_id=assessment.id,
            status="PENDING_CONTEXT",
        )
        session_db.add(case)
        await session_db.commit()
        case_id = str(case.id)

    async with _mcp_session(api_client) as mcp_session:
        result = await mcp_session.call_tool(
            "resume_remediation_case", {"case_id": case_id, "approved": True}
        )
        assert result.isError is True
        assert "409" in result.content[0].text
        assert "not awaiting human review" in result.content[0].text


async def test_compliance_resource_returns_ca_and_general_docs(api_client):
    async with _mcp_session(api_client) as session:
        result = await session.read_resource(AnyUrl("compliance://regulations/CA"))
        text = result.contents[0].text
        assert "policy_CA" in text or "Rate Reduction" in text
        assert len(text) > 0


async def test_compliance_resource_unsupported_state_falls_back_to_general_only(api_client):
    async with _mcp_session(api_client) as session:
        result = await session.read_resource(AnyUrl("compliance://regulations/FL"))
        text = result.contents[0].text
        assert len(text) > 0
