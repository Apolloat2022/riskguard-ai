"""POST/GET /api/v1/risk-assessment/{customer_id}"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.graph import run_case
from app.config import settings
from app.db.engine import get_session
from app.db.models import AuditLog, RemediationCase, RiskAssessment
from app.db.queries import RiskFeatureRow, fetch_risk_features
from app.exceptions import DatabaseUnavailableError
from app.logging_conf import bind_context
from app.schemas import RiskAssessmentResponse
from ml.predict import LoanFeatures, RiskModel

router = APIRouter(prefix="/api/v1/risk-assessment", tags=["risk"])
logger = logging.getLogger(__name__)


def _classify_risk_flag(probability: float) -> str:
    if probability < 0.30:
        return "LOW"
    if probability < 0.55:
        return "MEDIUM"
    if probability < 0.70:
        return "HIGH"
    return "CRITICAL"


def _to_optional_float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _build_features_snapshot(row: RiskFeatureRow) -> dict:
    return {
        "credit_score": row.credit_score,
        "debt_to_income": _to_optional_float(row.debt_to_income),
        "payment_history": row.payment_history,
        "loan_amount": float(row.loan_amount),
        "employment_duration": _to_optional_float(row.employment_duration),
        "previous_probability": _to_optional_float(row.previous_probability),
    }


async def launch_remediation_workflow(
    case_id: str, session_factory: async_sessionmaker
) -> None:
    """Fires the LangGraph workflow for a freshly created remediation case.
    Runs in a FastAPI BackgroundTask — after the HTTP response is sent, so
    the request never blocks on LLM latency (see app/api/remediation.py for
    the resume path triggered by a human decision)."""
    logger.info("remediation workflow triggered", extra={"case_id": case_id})
    await run_case(case_id, session_factory)


@router.post("/{customer_id}", response_model=RiskAssessmentResponse)
async def create_risk_assessment(
    customer_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> RiskAssessmentResponse:
    bind_context(customer_id=str(customer_id))
    risk_model: RiskModel = request.app.state.risk_model

    try:
        row = await fetch_risk_features(session, customer_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"customer {customer_id} not found")

        features = LoanFeatures(
            credit_score=row.credit_score,
            debt_to_income=_to_optional_float(row.debt_to_income),
            payment_history=row.payment_history,
            loan_amount=float(row.loan_amount),
            employment_duration=_to_optional_float(row.employment_duration),
        )
        probability = risk_model.predict_default_probability(features)
        risk_flag = _classify_risk_flag(probability)

        assessment = RiskAssessment(
            customer_id=customer_id,
            model_version=risk_model.model_version,
            default_probability=Decimal(str(round(probability, 5))),
            risk_flag=risk_flag,
            features_snapshot=_build_features_snapshot(row),
        )
        session.add(assessment)
        await session.flush()  # populates assessment.id (IDENTITY column)

        session.add(
            AuditLog(
                entity_type="risk_assessment",
                entity_id=str(assessment.id),
                action="SCORED",
                actor="system",
                detail={"default_probability": probability, "risk_flag": risk_flag},
            )
        )

        remediation_case_id = None
        if probability > settings.risk_trigger_threshold:
            case = RemediationCase(
                customer_id=customer_id,
                risk_assessment_id=assessment.id,
                status="PENDING_CONTEXT",
            )
            session.add(case)
            await session.flush()  # populates case.id (server-side gen_random_uuid())

            session.add(
                AuditLog(
                    entity_type="remediation_case",
                    entity_id=str(case.id),
                    action="AGENT_TRIGGERED",
                    actor="system",
                    detail={"risk_assessment_id": assessment.id},
                )
            )
            remediation_case_id = case.id

        await session.commit()
    except OperationalError as exc:
        raise DatabaseUnavailableError(f"database unavailable: {exc}") from exc

    if remediation_case_id is not None:
        # Fired only after commit — the case row must exist before the
        # workflow tries to resolve it by thread_id.
        background_tasks.add_task(
            launch_remediation_workflow, str(remediation_case_id), request.app.state.session_factory
        )

    return RiskAssessmentResponse(
        customer_id=customer_id,
        default_probability=probability,
        risk_flag=risk_flag,
        model_version=risk_model.model_version,
        remediation_case_id=remediation_case_id,
    )


@router.get("/{customer_id}", response_model=RiskAssessmentResponse)
async def get_latest_risk_assessment(
    customer_id: int,
    session: AsyncSession = Depends(get_session),
) -> RiskAssessmentResponse:
    try:
        stmt = (
            select(RiskAssessment)
            .where(RiskAssessment.customer_id == customer_id)
            .order_by(RiskAssessment.assessed_at.desc())
            .limit(1)
        )
        assessment = (await session.execute(stmt)).scalar_one_or_none()
        if assessment is None:
            raise HTTPException(
                status_code=404, detail=f"no risk assessment found for customer {customer_id}"
            )

        case_stmt = (
            select(RemediationCase.id)
            .where(RemediationCase.risk_assessment_id == assessment.id)
            .limit(1)
        )
        remediation_case_id = (await session.execute(case_stmt)).scalar_one_or_none()
    except OperationalError as exc:
        raise DatabaseUnavailableError(f"database unavailable: {exc}") from exc

    return RiskAssessmentResponse(
        customer_id=customer_id,
        default_probability=float(assessment.default_probability),
        risk_flag=assessment.risk_flag,
        model_version=assessment.model_version,
        remediation_case_id=remediation_case_id,
    )
