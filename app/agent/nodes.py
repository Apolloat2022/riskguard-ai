"""Pure LangGraph node functions — each takes AgentState and returns the
(partial) state update. No DB access here: node functions are unit-testable
in isolation, and audit logging / status persistence is handled by the
run_case wrapper in app/agent/graph.py, which observes which nodes ran via
LangGraph's per-node update stream.
"""

from __future__ import annotations

import re

from langgraph.types import interrupt

from app.agent.llm import draft_remediation_plan
from app.agent.state import AgentState
from app.rag.retriever import retrieve_policies

_MAX_REVISIONS = 3

# Mirrors the numeric limits authored in app/rag/documents/*.md. Kept as a
# small lookup (rather than regex-parsed from the markdown prose) so
# compliance_check stays cheap and deterministic; a production system might
# instead extract these as structured policy metadata.
_STATE_RATE_CAP_BPS = {"CA": 400, "NY": 350, "TX": 300}
_STATE_FORBEARANCE_CAP_MONTHS = {"CA": 15, "NY": 12, "TX": 12}
_DEFAULT_RATE_CAP_BPS = 300
_DEFAULT_FORBEARANCE_CAP_MONTHS = 12

_RETRIEVAL_QUERY = "loan restructuring rate reduction forbearance disclosure requirements"

_SECTION_PATTERN = re.compile(
    r"^(RATE_REDUCTION_BPS|FORBEARANCE_MONTHS|TERM_EXTENSION_MONTHS|POLICY_CITATION|SUMMARY):\s*(.*)$",
    re.MULTILINE,
)


def retrieve_context(state: AgentState) -> AgentState:
    parents = retrieve_policies(_RETRIEVAL_QUERY, state["state_code"], k=3)
    state["retrieved_policies"] = [f"[{p.heading}]\n{p.text}" for p in parents]
    return state


def plan_remediation(state: AgentState) -> AgentState:
    is_revision = state["generated_remediation_plan"] is not None
    plan_text = draft_remediation_plan(
        risk_features=state["risk_features"],
        policies=state["retrieved_policies"],
        prior_draft=state["generated_remediation_plan"] if is_revision else None,
        revision_feedback=state["compliance_notes"] if is_revision else None,
    )
    state["generated_remediation_plan"] = plan_text
    if is_revision:
        state["revision_count"] += 1
    state["compliance_status"] = "pending"
    state["compliance_notes"] = None
    return state


def _parse_plan(plan_text: str | None) -> dict | None:
    if not plan_text:
        return None
    fields: dict[str, str] = {
        match.group(1): match.group(2).strip() for match in _SECTION_PATTERN.finditer(plan_text)
    }
    if not fields:
        return None

    def _to_int(key: str) -> int | None:
        raw = fields.get(key)
        if raw is None:
            return None
        digits = re.search(r"-?\d+", raw)
        return int(digits.group()) if digits else None

    return {
        "rate_reduction_bps": _to_int("RATE_REDUCTION_BPS"),
        "forbearance_months": _to_int("FORBEARANCE_MONTHS"),
        "policy_citation": fields.get("POLICY_CITATION", "").strip() or None,
        "summary": fields.get("SUMMARY", "").strip() or None,
    }


def compliance_check(state: AgentState) -> AgentState:
    parsed = _parse_plan(state["generated_remediation_plan"])
    issues: list[str] = []

    if parsed is None:
        issues.append("Draft is not in the required 5-line format.")
    else:
        if not parsed.get("policy_citation"):
            issues.append("Draft is missing a POLICY_CITATION.")
        if not parsed.get("summary"):
            issues.append("Draft is missing a customer-facing SUMMARY.")

        rate_cap = _STATE_RATE_CAP_BPS.get(state["state_code"], _DEFAULT_RATE_CAP_BPS)
        rate_bps = parsed.get("rate_reduction_bps")
        if rate_bps is not None and rate_bps > rate_cap:
            issues.append(
                f"Proposed rate reduction {rate_bps}bps exceeds the {state['state_code']} "
                f"cap of {rate_cap}bps."
            )

        forbearance_cap = _STATE_FORBEARANCE_CAP_MONTHS.get(
            state["state_code"], _DEFAULT_FORBEARANCE_CAP_MONTHS
        )
        forbearance_months = parsed.get("forbearance_months")
        if forbearance_months is not None and forbearance_months > forbearance_cap:
            issues.append(
                f"Proposed forbearance {forbearance_months} months exceeds the "
                f"{state['state_code']} cap of {forbearance_cap} months."
            )

    if not issues:
        state["compliance_status"] = "passed"
        state["compliance_notes"] = None
    elif state["revision_count"] >= _MAX_REVISIONS:
        state["compliance_status"] = "rejected"
        state["compliance_notes"] = "Max revisions exhausted: " + "; ".join(issues)
    else:
        state["compliance_status"] = "needs_revision"
        state["compliance_notes"] = "; ".join(issues)

    return state


def route_compliance(state: AgentState) -> str:
    return state["compliance_status"]


def human_review(state: AgentState) -> AgentState:
    """No side effects before interrupt() — LangGraph re-runs this whole
    function from the top on resume, so anything before the interrupt call
    would otherwise execute twice."""
    decision = interrupt(
        {
            "case_id": state["case_id"],
            "customer_id": state["customer_id"],
            "generated_remediation_plan": state["generated_remediation_plan"],
        }
    )
    state["human_approved"] = bool(decision.get("approved", False))
    if not state["human_approved"]:
        state["compliance_notes"] = decision.get("notes") or "Reviewer requested revisions."
    return state


def route_human_decision(state: AgentState) -> str:
    return "approved" if state["human_approved"] else "denied"


def finalize(state: AgentState) -> AgentState:
    return state


def escalate(state: AgentState) -> AgentState:
    return state
