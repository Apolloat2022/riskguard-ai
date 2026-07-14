"""MCP protocol-translation layer over the existing REST API.

Tools are thin adapters that call app/api/risk.py and app/api/remediation.py
in-process via an ASGI-transport httpx client (app.state.mcp_http_client,
built once in app/main.py's combined lifespan) — the same pattern
tests/test_api.py already uses against the real app. This means zero
duplicated business logic and zero risk to the already-deployed REST
endpoints: the MCP layer only translates protocol, it doesn't reimplement
scoring, persistence, or the remediation workflow.

stateless_http=True because Dockerfile already runs `uvicorn --workers 2` —
even a single ECS task has two independent worker processes with no request
affinity, so a stateful streamable-HTTP session tied to one worker would be
invisible to the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.rag.retriever import get_state_policies

if TYPE_CHECKING:
    from fastapi import FastAPI


def _raise_for_status(response) -> None:
    """Normalizes both error envelope shapes this app produces: RiskGuardError
    subclasses -> {"error_code","message","request_id"} (see app/exceptions.py),
    and plain HTTPException (404s/409s raised directly in the route handlers,
    not covered by register_exception_handlers) -> {"detail": "..."}."""
    if response.status_code < 400:
        return
    body = response.json()
    message = body.get("message") or body.get("detail") or response.text
    raise ValueError(f"{response.status_code}: {message}")


def build_mcp_server(app: "FastAPI") -> FastMCP:
    # DNS-rebinding Host-header protection defaults to rejecting every host
    # (an empty allow-list, not a permissive one) — appropriate for a local
    # stdio/localhost MCP server, but this one is reached through a public
    # ALB whose DNS name isn't knowable/stable at code-authoring time, and
    # the REST API it wraps already has no auth in front of it (see
    # AWS_DEPLOYMENT.md's known-gap note). Disabling this protection here
    # doesn't weaken that existing posture; leaving it on would have broken
    # every request, including legitimate ones.
    mcp = FastMCP(
        "riskguard-ai",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def assess_loan_risk(customer_id: int) -> dict:
        """Score a customer's default-risk probability using the production
        XGBoost pipeline. Mirrors POST /api/v1/risk-assessment/{customer_id}:
        persists the assessment, writes an audit log, and — if the
        probability exceeds the configured risk trigger threshold — creates
        a remediation case and starts the LangGraph human-in-the-loop
        workflow in the background."""
        resp = await app.state.mcp_http_client.post(f"/api/v1/risk-assessment/{customer_id}")
        _raise_for_status(resp)
        return resp.json()

    @mcp.tool()
    async def get_remediation_case(case_id: str) -> dict:
        """Fetch a remediation case's current status, drafted remediation
        plan, and compliance notes."""
        resp = await app.state.mcp_http_client.get(f"/api/v1/remediation/{case_id}")
        _raise_for_status(resp)
        return resp.json()

    @mcp.tool()
    async def resume_remediation_case(
        case_id: str, approved: bool, notes: str | None = None
    ) -> dict:
        """Approve or reject a remediation case that is awaiting human
        review. Approving finalizes the case. Rejecting does NOT terminate
        it — it re-drafts the plan via Bedrock (one more LLM call) and
        returns the case to AWAITING_HUMAN_REVIEW with an incremented
        revision count, up to a small revision cap before it escalates."""
        path = "approve" if approved else "reject"
        resp = await app.state.mcp_http_client.post(
            f"/api/v1/remediation/{case_id}/{path}", json={"notes": notes}
        )
        _raise_for_status(resp)
        return resp.json()

    @mcp.resource("compliance://regulations/{state}")
    def compliance_regulations(state: str) -> str:
        """State-scoped compliance/underwriting policy text — the same
        parent-chunk corpus the remediation agent's RAG retrieval draws
        from, returned in full (unranked) rather than query-scored."""
        parents = get_state_policies(state)
        if not parents:
            raise ValueError(f"no policy documents available for state '{state}'")
        # p.text already starts with its own "## Heading" line (see
        # _split_into_parents in app/rag/retriever.py) — don't prepend it again.
        return "\n\n---\n\n".join(p.text for p in parents)

    return mcp
