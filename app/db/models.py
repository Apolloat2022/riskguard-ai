"""SQLAlchemy 2.0 declarative models. Mirrors scripts/run_migration.sql exactly —
if you change one, change the other.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint("credit_score BETWEEN 300 AND 850", name="ck_customers_credit_score"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    external_ref: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    credit_score: Mapped[int | None] = mapped_column(SmallInteger)
    debt_to_income: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    loan_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    employment_duration: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    payment_events: Mapped[list["PaymentEvent"]] = relationship(back_populates="customer")
    risk_assessments: Mapped[list["RiskAssessment"]] = relationship(back_populates="customer")
    remediation_cases: Mapped[list["RemediationCase"]] = relationship(back_populates="customer")


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    paid_date: Mapped[date | None] = mapped_column(Date)
    # Postgres GENERATED ALWAYS AS ... STORED column — computed server-side,
    # never set from Python.
    days_late: Mapped[int] = mapped_column(
        Integer, Computed("COALESCE(paid_date - due_date, 0)", persisted=True)
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="payment_events")


Index("idx_payment_events_customer", PaymentEvent.customer_id, PaymentEvent.due_date.desc())


class RiskAssessment(Base):
    __tablename__ = "risk_assessments"
    __table_args__ = (
        CheckConstraint(
            "default_probability BETWEEN 0 AND 1", name="ck_risk_assessments_probability"
        ),
        CheckConstraint(
            "risk_flag IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="ck_risk_assessments_flag"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id"), nullable=False
    )
    model_version: Mapped[str] = mapped_column(Text, nullable=False)
    default_probability: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    risk_flag: Mapped[str] = mapped_column(Text, nullable=False)
    features_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    customer: Mapped["Customer"] = relationship(back_populates="risk_assessments")
    remediation_cases: Mapped[list["RemediationCase"]] = relationship(
        back_populates="risk_assessment"
    )


Index(
    "idx_risk_assessments_customer",
    RiskAssessment.customer_id,
    RiskAssessment.assessed_at.desc(),
)


class RemediationCase(Base):
    """id doubles as the LangGraph checkpointer thread_id (see app/agent/graph.py)."""

    __tablename__ = "remediation_cases"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING_CONTEXT','PLAN_DRAFTED','AWAITING_HUMAN_REVIEW',"
            "'APPROVED','REVISION_REQUESTED','REJECTED','ESCALATED')",
            name="ck_remediation_cases_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("customers.id"), nullable=False
    )
    risk_assessment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("risk_assessments.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="PENDING_CONTEXT"
    )
    remediation_plan: Mapped[str | None] = mapped_column(Text)
    compliance_notes: Mapped[str | None] = mapped_column(Text)
    revision_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped["Customer"] = relationship(back_populates="remediation_cases")
    risk_assessment: Mapped["RiskAssessment"] = relationship(back_populates="remediation_cases")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


Index(
    "idx_audit_entity", AuditLog.entity_type, AuditLog.entity_id, AuditLog.created_at.desc()
)
