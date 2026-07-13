"""GET /api/v1/remediation/{case_id}
POST /api/v1/remediation/{case_id}/approve
POST /api/v1/remediation/{case_id}/reject

The approve/reject endpoints *are* the compliance-officer sign-off
simulation: each resumes the paused LangGraph run via Command(resume=...).
Run synchronously (not via BackgroundTasks like the initial risk-triggered
launch) — a reject can trigger one more LLM revision, so the call may take a
few seconds, but the plan calls for a direct resume-and-respond here.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import run_case
from app.db.engine import get_session
from app.db.models import RemediationCase
from app.exceptions import DatabaseUnavailableError
from app.logging_conf import bind_context
from app.schemas import RemediationCaseResponse, RemediationDecisionRequest

router = APIRouter(prefix="/api/v1/remediation", tags=["remediation"])
logger = logging.getLogger(__name__)


def _to_response(case: RemediationCase) -> RemediationCaseResponse:
    return RemediationCaseResponse(
        case_id=case.id,
        customer_id=case.customer_id,
        status=case.status,
        remediation_plan=case.remediation_plan,
        compliance_notes=case.compliance_notes,
        revision_count=case.revision_count,
        created_at=case.created_at,
        resolved_at=case.resolved_at,
    )


async def _get_case_or_404(session: AsyncSession, case_id: uuid.UUID) -> RemediationCase:
    case = await session.get(RemediationCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"remediation case {case_id} not found")
    return case


@router.get("/{case_id}", response_model=RemediationCaseResponse)
async def get_remediation_case(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RemediationCaseResponse:
    try:
        case = await _get_case_or_404(session, case_id)
    except OperationalError as exc:
        raise DatabaseUnavailableError(f"database unavailable: {exc}") from exc
    return _to_response(case)


async def _resume_case(
    case_id: uuid.UUID,
    request: Request,
    session: AsyncSession,
    *,
    approved: bool,
    notes: str | None,
) -> RemediationCaseResponse:
    bind_context(case_id=str(case_id))
    try:
        case = await _get_case_or_404(session, case_id)
        if case.status != "AWAITING_HUMAN_REVIEW":
            raise HTTPException(
                status_code=409,
                detail=f"case {case_id} is not awaiting human review (status={case.status})",
            )
    except OperationalError as exc:
        raise DatabaseUnavailableError(f"database unavailable: {exc}") from exc

    session_factory = request.app.state.session_factory
    await run_case(str(case_id), session_factory, resume={"approved": approved, "notes": notes})

    try:
        # run_case committed via its own session — this session's copy of
        # `case` is stale (expire_on_commit=False); force a re-read.
        await session.refresh(case)
    except OperationalError as exc:
        raise DatabaseUnavailableError(f"database unavailable: {exc}") from exc
    return _to_response(case)


@router.post("/{case_id}/approve", response_model=RemediationCaseResponse)
async def approve_remediation_case(
    case_id: uuid.UUID,
    request: Request,
    body: RemediationDecisionRequest = RemediationDecisionRequest(),
    session: AsyncSession = Depends(get_session),
) -> RemediationCaseResponse:
    return await _resume_case(case_id, request, session, approved=True, notes=body.notes)


@router.post("/{case_id}/reject", response_model=RemediationCaseResponse)
async def reject_remediation_case(
    case_id: uuid.UUID,
    request: Request,
    body: RemediationDecisionRequest = RemediationDecisionRequest(),
    session: AsyncSession = Depends(get_session),
) -> RemediationCaseResponse:
    return await _resume_case(case_id, request, session, approved=False, notes=body.notes)
