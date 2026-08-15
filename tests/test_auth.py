"""Auth-middleware tests: both public surfaces (REST + MCP) and the internal
MCP -> REST loopback.

The loopback case is the one worth having. The MCP tools reach the REST routes
back through the same ASGI app, so they pass through the auth middleware like
any external caller — if InternalAuth ever stops attaching the credential,
every tool 401s itself while the REST API keeps working, which is exactly the
kind of failure that would otherwise only show up in production.

api_client is session-scoped (one app, one lifespan) and the middleware reads
settings.api_key per request, so flipping the key with monkeypatch takes effect
immediately and unwinds after each test.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import settings

API_KEY = "test-key-6Qw8ZrTn"


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(settings, "api_key", API_KEY)
    return API_KEY


@asynccontextmanager
async def _mcp_session(api_client, headers: dict | None = None):
    transport = httpx.ASGITransport(app=api_client.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers or {}
    ) as http_client:
        async with streamable_http_client("http://test/mcp", http_client=http_client) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def test_healthz_is_reachable_without_a_key(api_client, auth_on):
    # The ALB health check can't present a credential; a 401 here would mark
    # every task unhealthy and trigger replacement.
    resp = await api_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_rest_endpoint_rejects_missing_key(api_client, auth_on):
    resp = await api_client.get("/api/v1/risk-assessment/1")
    assert resp.status_code == 401
    assert resp.json()["error_code"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_rest_endpoint_rejects_wrong_key(api_client, auth_on):
    resp = await api_client.get(
        "/api/v1/risk-assessment/1", headers={"Authorization": "Bearer not-the-key"}
    )
    assert resp.status_code == 401


async def test_docs_are_protected(api_client, auth_on):
    # /docs was the ALB health-check path before /healthz existed; it publishes
    # the full API schema, so it must not stay open once auth is on.
    assert (await api_client.get("/docs")).status_code == 401


async def test_bearer_and_x_api_key_are_both_accepted(api_client, auth_on, low_risk_customer_id):
    for headers in (
        {"Authorization": f"Bearer {auth_on}"},
        {"X-API-Key": auth_on},
    ):
        resp = await api_client.get(
            f"/api/v1/risk-assessment/{low_risk_customer_id}", headers=headers
        )
        # 404 (nothing scored for this fresh customer) proves the request got
        # past auth and into the route — the assertion is "not 401".
        assert resp.status_code == 404, headers


async def test_mcp_endpoint_rejects_missing_key(api_client, auth_on):
    resp = await api_client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 401


async def test_mcp_tool_works_with_key_and_internal_loopback_authenticates(
    api_client, auth_on, low_risk_customer_id
):
    async with _mcp_session(api_client, {"Authorization": f"Bearer {auth_on}"}) as session:
        result = await session.call_tool(
            "assess_loan_risk", {"customer_id": low_risk_customer_id}
        )
        # If InternalAuth failed to attach the key, the tool's in-process call
        # to POST /api/v1/risk-assessment/{id} would come back 401 and this
        # would be an error result rather than a scored assessment.
        assert result.isError is False
        body = json.loads(result.content[0].text)
        assert body["customer_id"] == low_risk_customer_id
        assert body["risk_flag"] in ("LOW", "MEDIUM")


async def test_unset_key_leaves_endpoints_open(api_client, monkeypatch):
    monkeypatch.setattr(settings, "api_key", None)
    assert (await api_client.get("/healthz")).status_code == 200
    assert (await api_client.get("/api/v1/risk-assessment/999999999")).status_code == 404
