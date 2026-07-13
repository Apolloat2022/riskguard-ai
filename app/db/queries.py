"""Hand-written SQL for the risk-assessment feature CTE.

Deliberately raw SQL rather than an ORM join chain: one round trip that
assembles model features (customer attributes + rolling 24-month payment
stats) plus the customer's most recent prior assessment, if any.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_RISK_FEATURES_SQL = text(
    """
    WITH payment_stats AS (
        SELECT customer_id,
               COUNT(*) FILTER (WHERE days_late > 30)                    AS late_payment_count,
               COALESCE(AVG(days_late) FILTER (WHERE days_late > 0), 0)  AS avg_days_late
        FROM payment_events
        WHERE customer_id = :cid
          AND due_date >= now() - INTERVAL '24 months'
        GROUP BY customer_id
    ),
    latest_assessment AS (
        SELECT customer_id, default_probability, assessed_at
        FROM risk_assessments
        WHERE customer_id = :cid
        ORDER BY assessed_at DESC
        LIMIT 1
    )
    SELECT c.id, c.external_ref, c.state_code, c.credit_score, c.debt_to_income,
           c.loan_amount, c.employment_duration,
           COALESCE(ps.late_payment_count, 0) AS payment_history,
           la.default_probability AS previous_probability
    FROM customers c
    LEFT JOIN payment_stats ps      ON ps.customer_id = c.id
    LEFT JOIN latest_assessment la  ON la.customer_id = c.id
    WHERE c.id = :cid;
    """
)


@dataclass(frozen=True, slots=True)
class RiskFeatureRow:
    customer_id: int
    external_ref: str
    state_code: str
    credit_score: int | None
    debt_to_income: Decimal | None
    loan_amount: Decimal
    employment_duration: Decimal | None
    payment_history: int
    previous_probability: Decimal | None


async def fetch_risk_features(session: AsyncSession, customer_id: int) -> RiskFeatureRow | None:
    """Returns None if the customer doesn't exist (endpoint maps that to 404)."""
    result = await session.execute(_RISK_FEATURES_SQL, {"cid": customer_id})
    row = result.mappings().first()
    if row is None:
        return None
    return RiskFeatureRow(
        customer_id=row["id"],
        external_ref=row["external_ref"],
        state_code=row["state_code"],
        credit_score=row["credit_score"],
        debt_to_income=row["debt_to_income"],
        loan_amount=row["loan_amount"],
        employment_duration=row["employment_duration"],
        payment_history=row["payment_history"],
        previous_probability=row["previous_probability"],
    )
