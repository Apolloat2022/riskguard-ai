"""LangGraph agent state. Fields beyond the plan's required set (case_id,
state_code, retrieved_policies, revision_count, ...) are what make the graph
actually function — routing and revision-loop bounding both read them."""

from __future__ import annotations

from typing import Literal, TypedDict


class AgentState(TypedDict):
    customer_id: int
    case_id: str  # == remediation_cases.id == LangGraph thread_id
    risk_score: float
    risk_features: dict
    state_code: str  # drives policy retrieval
    retrieved_policies: list[str]  # RAG output (parent chunk texts)
    generated_remediation_plan: str | None
    compliance_status: Literal["pending", "passed", "needs_revision", "rejected"]
    compliance_notes: str | None
    human_approved: bool
    revision_count: int  # guards against infinite revision loops (max 3)
