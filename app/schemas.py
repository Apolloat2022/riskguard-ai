"""Pydantic request/response models for the public API."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class RiskAssessmentResponse(BaseModel):
    customer_id: int
    default_probability: float
    risk_flag: str
    model_version: str
    remediation_case_id: uuid.UUID | None = None


class RemediationDecisionRequest(BaseModel):
    notes: str | None = None


class RemediationCaseResponse(BaseModel):
    case_id: uuid.UUID
    customer_id: int
    status: str
    remediation_plan: str | None = None
    compliance_notes: str | None = None
    revision_count: int
    created_at: datetime
    resolved_at: datetime | None = None
