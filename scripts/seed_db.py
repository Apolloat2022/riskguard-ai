"""Creates tables (if not present) and seeds demo customers + payment history.

Every 7th customer is deliberately engineered to be high-risk (low credit
score, high DTI, minimal tenure, several severely-late payments) so Phase 4's
risk endpoint has real candidates that cross the 0.70 remediation trigger.

    python scripts/seed_db.py
    python scripts/seed_db.py --database-url postgresql+asyncpg://... --count 50
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import date, timedelta
from decimal import Decimal

from app.db.engine import build_engine, build_session_factory
from app.db.models import Base, Customer, PaymentEvent

# Every seeded customer lands in a state with a dedicated policy doc
# (app/rag/documents/policy_{CA,NY,TX}.md) — see Phase 5.
_STATE_CODES = ["CA", "NY", "TX"]
_HIGH_RISK_EVERY = 7


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _make_customer(i: int, rng: random.Random) -> tuple[Customer, bool]:
    is_high_risk = i % _HIGH_RISK_EVERY == 0
    state_code = rng.choice(_STATE_CODES)

    if is_high_risk:
        credit_score = int(rng.uniform(500, 600))
        debt_to_income = rng.uniform(0.50, 0.65)
        loan_amount = rng.uniform(300_000, 500_000)
        employment_duration = rng.uniform(0, 1)
    else:
        credit_score = int(_clip(rng.gauss(680, 90), 300, 850))
        debt_to_income = _clip(rng.betavariate(2, 5) * 0.65, 0, 0.65)
        loan_amount = _clip(rng.lognormvariate(10.8, 0.65), 5_000, 500_000)
        employment_duration = _clip(rng.expovariate(1 / 6.0), 0, 40)

    customer = Customer(
        external_ref=f"CUST-{i:06d}",
        full_name=f"Demo Customer {i:03d}",
        state_code=state_code,
        credit_score=credit_score,
        debt_to_income=Decimal(str(round(debt_to_income, 4))),
        loan_amount=Decimal(str(round(loan_amount, 2))),
        employment_duration=Decimal(str(round(employment_duration, 2))),
    )
    return customer, is_high_risk


def _make_payment_events(
    customer_id: int, is_high_risk: bool, rng: random.Random
) -> list[PaymentEvent]:
    n_events = rng.randint(6, 10) if is_high_risk else rng.randint(4, 12)
    late_rate = 0.7 if is_high_risk else 0.10
    today = date.today()

    events = []
    for k in range(n_events):
        due = today - timedelta(days=30 * (n_events - k))
        if rng.random() < late_rate:
            late_days = rng.randint(31, 90) if is_high_risk else rng.randint(1, 20)
            paid = due + timedelta(days=late_days)
        else:
            paid = due + timedelta(days=rng.randint(-2, 5))
        events.append(
            PaymentEvent(
                customer_id=customer_id, due_date=due, paid_date=paid, amount=Decimal("500.00")
            )
        )
    return events


async def seed(database_url: str | None, count: int, seed_value: int) -> None:
    rng = random.Random(seed_value)
    engine = build_engine(database_url)
    session_factory = build_session_factory(engine)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        high_risk_count = 0
        async with session_factory() as session:
            for i in range(1, count + 1):
                customer, is_high_risk = _make_customer(i, rng)
                session.add(customer)
                await session.flush()  # populates customer.id from the IDENTITY column

                for event in _make_payment_events(customer.id, is_high_risk, rng):
                    session.add(event)

                high_risk_count += int(is_high_risk)

            await session.commit()

        print(f"seeded {count} customers ({high_risk_count} engineered high-risk)")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    asyncio.run(seed(args.database_url, args.count, args.seed))


if __name__ == "__main__":
    main()
