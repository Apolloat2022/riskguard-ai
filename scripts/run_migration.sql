-- RiskGuard AI schema. Mirrors app/db/models.py exactly — if you change one,
-- change the other. Idempotent (safe to re-run against an existing database).

-- gen_random_uuid() is built into Postgres 13+ core; the extension is kept
-- for compatibility with older engines (harmless no-op if already core).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS customers (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_ref        TEXT UNIQUE NOT NULL,          -- e.g. "CUST-000123"
    full_name           TEXT NOT NULL,
    state_code          CHAR(2) NOT NULL,              -- drives RAG policy lookup
    credit_score        SMALLINT CHECK (credit_score BETWEEN 300 AND 850),
    debt_to_income      NUMERIC(5,4),
    loan_amount         NUMERIC(12,2) NOT NULL,
    employment_duration NUMERIC(5,2),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payment_events (      -- history lives in its own table,
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id   BIGINT NOT NULL REFERENCES customers(id),
    due_date      DATE NOT NULL,
    paid_date     DATE,                                -- NULL = unpaid
    days_late     INT GENERATED ALWAYS AS
                    (COALESCE(paid_date - due_date, 0)) STORED,
    amount        NUMERIC(12,2) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payment_events_customer
    ON payment_events (customer_id, due_date DESC);

CREATE TABLE IF NOT EXISTS risk_assessments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id         BIGINT NOT NULL REFERENCES customers(id),
    model_version       TEXT NOT NULL,                 -- "v1"
    default_probability NUMERIC(6,5) NOT NULL CHECK (default_probability BETWEEN 0 AND 1),
    risk_flag           TEXT NOT NULL CHECK (risk_flag IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    features_snapshot   JSONB NOT NULL,                -- exact inputs at scoring time (audit)
    assessed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_risk_assessments_customer
    ON risk_assessments (customer_id, assessed_at DESC);

CREATE TABLE IF NOT EXISTS remediation_cases (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),  -- doubles as LangGraph thread_id
    customer_id         BIGINT NOT NULL REFERENCES customers(id),
    risk_assessment_id  BIGINT NOT NULL REFERENCES risk_assessments(id),
    status              TEXT NOT NULL DEFAULT 'PENDING_CONTEXT'
                        CHECK (status IN ('PENDING_CONTEXT','PLAN_DRAFTED',
                                          'AWAITING_HUMAN_REVIEW','APPROVED',
                                          'REVISION_REQUESTED','REJECTED','ESCALATED')),
    remediation_plan    TEXT,
    compliance_notes    TEXT,
    revision_count      SMALLINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at         TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type  TEXT NOT NULL,        -- 'risk_assessment' | 'remediation_case' | ...
    entity_id    TEXT NOT NULL,
    action       TEXT NOT NULL,        -- 'SCORED' | 'AGENT_TRIGGERED' | 'PLAN_DRAFTED' | 'HUMAN_APPROVED' | ...
    actor        TEXT NOT NULL,        -- 'system' | 'agent' | user identity
    detail       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_entity
    ON audit_logs (entity_type, entity_id, created_at DESC);
