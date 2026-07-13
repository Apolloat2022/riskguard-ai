# RiskGuard AI — Enterprise Risk Analytics & Automated Remediation Platform

**Architectural Blueprint & Implementation Plan** (plan only — no code has been written yet)

This document is the single source of truth for the build. It specifies the architecture, database schema, module contracts, and phased build order. It is written to be handed to an implementing agent (Claude Code) and executed phase by phase.

---

## 0. Corrections to the original spec (read first)

1. **"Claude 3.5 Sonnet" is retired.** `claude-3-5-sonnet-20241022` was retired from the Anthropic API in October 2025. The current Sonnet-tier model is **Claude Sonnet 5**. On Amazon Bedrock the model ID is **`anthropic.claude-sonnet-5`** (Bedrock IDs carry an `anthropic.` prefix), accessed via the **`AnthropicBedrockMantle`** client from the official `anthropic` Python SDK (`pip install "anthropic[bedrock]"`). Do not use the legacy `boto3 bedrock-runtime InvokeModel` path or LangChain's `ChatBedrock` wrapper — the Mantle client speaks the standard Messages API and keeps the code portable to the first-party API.
2. **Sonnet 5 API constraints** (differ from 3.5-era code you may find in tutorials): no `temperature`/`top_p`/`top_k` (400 error), no assistant-message prefill (400 error), no `budget_tokens` thinking config. Thinking is adaptive by default. Use `output_config={"format": {...}}` (JSON schema) when structured output is needed instead of prefills.
3. **Vercel is the wrong deploy target for this backend.** XGBoost + scikit-learn + pandas blow past Vercel's serverless bundle limits, and the LangGraph human-in-the-loop workflow needs a process that survives between requests. **Deploy the API to AWS Fargate** (containerized). Vercel is fine for an optional dashboard frontend only.
4. **`MemorySaver` is demo-only.** It satisfies the spec for local runs, but on Fargate (ephemeral tasks) paused workflows would be lost on restart. The plan isolates the checkpointer behind a factory so production can switch to `langgraph-checkpoint-postgres` (same Neon database) with a one-line config change. Interviewers notice this distinction — keep it in the README.

---

## 1. System architecture

```
                        ┌──────────────────────────────────────────────────────┐
                        │                    RiskGuard AI                      │
                        └──────────────────────────────────────────────────────┘

  OFFLINE (batch)                 ONLINE (request path)              AGENTIC (async, stateful)
┌──────────────────┐   joblib   ┌──────────────────────┐  p > 0.70  ┌─────────────────────────┐
│ generate_dataset  │  artifact  │ FastAPI (async)      │──────────▶│ LangGraph workflow      │
│ train.py          │──────────▶│ /api/v1/risk-        │  case_id   │                         │
│  - impute/scale   │           │  assessment/{id}     │            │ A. retrieve_context     │
│  - class imbal.   │           │        │             │            │    (parent-child RAG)   │
│  - XGBoost        │           │   CTE query          │            │ B. plan_remediation     │
│  - F1/P/R/ROC-AUC │           │        │             │            │    (Bedrock Sonnet 5)   │
└──────────────────┘           │   Neon Postgres      │◀──────────│ C. compliance_check     │
                                │  - customers         │  persist   │ D. human_review         │
                                │  - risk_assessments  │  state +   │    ⏸ interrupt()        │
                                │  - remediation_cases │  audit     │    (MemorySaver ckpt)   │
                                │  - audit_logs        │            │ E. finalize / escalate  │
                                └──────────────────────┘            └─────────────────────────┘
                                         ▲                                     ▲
                                         │  POST /remediation/{case}/approve   │
                                         └────────── compliance officer ───────┘
                                                    resumes the graph
```

Skills demonstrated, mapped to résumé claims:
- **Traditional ML / DS**: feature engineering, imputation, scaling, class imbalance, XGBoost, threshold tuning, ROC-AUC — `ml/`
- **Backend engineering**: async FastAPI, SQLAlchemy 2.0 async, optimized CTE, Neon Postgres — `app/`
- **GenAI / RAG**: parent-child chunking retriever over policy documents — `app/rag/`
- **Agentic AI**: stateful LangGraph with conditional edges, revision loop, checkpointer, human-in-the-loop interrupt — `app/agent/`

---

## 2. Repository layout

```
riskguard-ai/
├── PLAN.md                        # this file
├── README.md                      # architecture diagram + quickstart (Phase 5)
├── pyproject.toml                 # single project; deps grouped [ml], [api], [agent]
├── .env.example                   # every env var, no values
├── Dockerfile                     # multi-stage, uvicorn, for Fargate
├── docker-compose.yml             # local Postgres for dev (optional; Neon otherwise)
│
├── ml/
│   ├── generate_dataset.py        # synthetic loan data → data/loans.csv
│   ├── train.py                   # pipeline build, train, evaluate, save artifacts
│   ├── predict.py                 # load-once inference wrapper used by the API
│   └── artifacts/v1/              # model.joblib, metrics.json, roc_curve.png, threshold.json
│
├── app/
│   ├── main.py                    # FastAPI app factory + lifespan (model + DB engine)
│   ├── config.py                  # pydantic-settings; ALL secrets from env
│   ├── logging_conf.py            # structured JSON logging (dictConfig)
│   ├── exceptions.py              # domain exceptions + FastAPI handlers
│   ├── db/
│   │   ├── engine.py              # async engine/session factory (asyncpg, Neon SSL)
│   │   ├── models.py              # SQLAlchemy 2.0 declarative models
│   │   └── queries.py             # the risk-assessment CTE lives here
│   ├── api/
│   │   ├── risk.py                # GET/POST /api/v1/risk-assessment/{customer_id}
│   │   └── remediation.py         # GET case, POST /approve, POST /reject
│   ├── rag/
│   │   ├── documents/             # policy markdown files (per state regulation)
│   │   └── retriever.py           # parent-child chunking retriever (mock, in-memory)
│   ├── agent/
│   │   ├── state.py               # AgentState TypedDict
│   │   ├── nodes.py               # node functions (retrieve, plan, compliance, finalize)
│   │   ├── graph.py               # graph wiring, conditional edges, checkpointer factory
│   │   └── llm.py                 # AnthropicBedrockMantle client, timeout/retry wrapper
│   └── schemas.py                 # Pydantic request/response models
│
├── scripts/
│   ├── seed_db.py                 # create tables + seed 50 demo customers
│   └── run_migration.sql          # DDL (also mirrored in models.py)
│
└── tests/
    ├── test_ml_pipeline.py        # trains on tiny sample, asserts metrics exist
    ├── test_api.py                # httpx AsyncClient against test DB
    └── test_agent_graph.py        # graph runs to interrupt, resumes, reaches END
```

One repo, one `pyproject.toml`. The `ml/` package is imported by `app/` (no duplication of feature logic).

---

## 3. Component 1 — Traditional ML & Data Science pipeline

### 3.1 `ml/generate_dataset.py`
- NumPy-based synthetic generator, seeded (`--seed 42`), `--rows 25000`.
- Features and realistic distributions:

| feature | distribution | notes |
|---|---|---|
| `credit_score` | int, clipped normal(680, 90), range 300–850 | |
| `debt_to_income` | beta(2,5) scaled to 0–0.65 | |
| `payment_history` | count of late payments, zero-inflated Poisson(λ=1.2) | 0 = clean |
| `loan_amount` | lognormal, ~$5k–$500k | |
| `employment_duration` | exponential, years, clipped 0–40 | |

- **Label generation**: latent logistic score = weighted combination (negative weight on credit_score and employment_duration, positive on DTI, late payments, loan_amount) + Gaussian noise → sigmoid → Bernoulli draw calibrated to **~8% default rate** (real imbalance, not 50/50).
- **Inject missingness deliberately**: ~6% of `employment_duration` and ~4% of `debt_to_income` set to NaN (missing-at-random) so the imputer in the pipeline is load-bearing, not decorative.
- Output: `data/loans.csv` + printed class balance summary.

### 3.2 `ml/train.py`
- **Pipeline** (sklearn `Pipeline` + `ColumnTransformer` so preprocessing ships inside the artifact):
  1. `SimpleImputer(strategy="median")` on all numeric features
  2. `StandardScaler()`
  3. `XGBClassifier` with `scale_pos_weight = n_neg / n_pos`
- **Class imbalance decision (document in README)**: `scale_pos_weight` is the primary mechanism — for gradient-boosted trees it outperforms SMOTE and avoids leaking synthetic samples into validation. Include a commented/flagged `--use-smote` path via `imblearn.pipeline.Pipeline` + `SMOTE` (train folds only) to demonstrate awareness of both.
- Stratified 80/20 split; early stopping on a validation slice (`eval_set`, `early_stopping_rounds=50`).
- **Threshold tuning**: don't report metrics at the naive 0.5 cut — sweep thresholds, pick the F1-optimal threshold on validation, persist it to `threshold.json`. (The 0.70 agent trigger is a *business* threshold on the raw probability and is independent.)
- **Evaluation outputs**:
  - `metrics.json`: precision, recall, F1 (at tuned threshold), ROC-AUC, PR-AUC, confusion matrix, class balance, train date, git sha.
  - `roc_curve.png` via matplotlib (ROC curve with AUC annotation) — spec requirement.
- **Artifacts**: `ml/artifacts/v1/model.joblib` (the *entire* pipeline), `metrics.json`, `threshold.json`, `feature_names.json`. Versioned directory = poor-man's model registry; README notes MLflow as the production upgrade.

### 3.3 `ml/predict.py`
```python
class RiskModel:
    """Loads the joblib pipeline once; thread-safe read-only inference."""
    def __init__(self, artifact_dir: Path): ...
    def predict_default_probability(self, features: LoanFeatures) -> float:
        """Returns p(default) in [0.0, 1.0]. Raises ModelInferenceError on failure."""
```
- Loaded once in the FastAPI lifespan, stored on `app.state.risk_model`. XGBoost inference is CPU-bound and fast (<1 ms/row) — call it directly in the endpoint (no thread-pool ceremony needed for single-row scoring; note this reasoning in a comment).

---

## 4. Component 2 — FastAPI backend & database layer

### 4.1 Database schema (Neon Postgres)

```sql
CREATE TABLE customers (
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

CREATE TABLE payment_events (                          -- history lives in its own table,
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id   BIGINT NOT NULL REFERENCES customers(id),
    due_date      DATE NOT NULL,
    paid_date     DATE,                                -- NULL = unpaid
    days_late     INT GENERATED ALWAYS AS
                    (COALESCE(paid_date - due_date, 0)) STORED,
    amount        NUMERIC(12,2) NOT NULL
);
CREATE INDEX idx_payment_events_customer ON payment_events (customer_id, due_date DESC);

CREATE TABLE risk_assessments (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id         BIGINT NOT NULL REFERENCES customers(id),
    model_version       TEXT NOT NULL,                 -- "v1"
    default_probability NUMERIC(6,5) NOT NULL CHECK (default_probability BETWEEN 0 AND 1),
    risk_flag           TEXT NOT NULL CHECK (risk_flag IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    features_snapshot   JSONB NOT NULL,                -- exact inputs at scoring time (audit)
    assessed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_risk_assessments_customer ON risk_assessments (customer_id, assessed_at DESC);

CREATE TABLE remediation_cases (
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

CREATE TABLE audit_logs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_type  TEXT NOT NULL,        -- 'risk_assessment' | 'remediation_case' | ...
    entity_id    TEXT NOT NULL,
    action       TEXT NOT NULL,        -- 'SCORED' | 'AGENT_TRIGGERED' | 'PLAN_DRAFTED' | 'HUMAN_APPROVED' | ...
    actor        TEXT NOT NULL,        -- 'system' | 'agent' | user identity
    detail       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_entity ON audit_logs (entity_type, entity_id, created_at DESC);
```

Design notes for the README: `features_snapshot JSONB` gives point-in-time reproducibility of every score; `payment_events` normalized so the CTE aggregation is genuine; `remediation_cases.id` (UUID) is reused as the LangGraph `thread_id`, tying DB state to checkpointer state.

### 4.2 SQLAlchemy layer
- SQLAlchemy **2.0 style**, `AsyncEngine` + `async_sessionmaker`, driver `postgresql+asyncpg`.
- Neon specifics: require SSL (`ssl=require` in connect args), `pool_pre_ping=True`, modest pool (`pool_size=5, max_overflow=5`) — Neon's pooler handles the rest. Connection string entirely from `DATABASE_URL` env var.
- Engine created in FastAPI **lifespan**, disposed on shutdown. Sessions via `Depends(get_session)`.

### 4.3 The risk-assessment endpoint

`POST /api/v1/risk-assessment/{customer_id}` (POST — it creates an assessment row; a GET returns the latest stored one).

Flow:
1. **One round trip** to Postgres using a CTE that assembles model features:

```sql
WITH payment_stats AS (
    SELECT customer_id,
           COUNT(*) FILTER (WHERE days_late > 30)          AS late_payment_count,
           COALESCE(AVG(days_late) FILTER (WHERE days_late > 0), 0) AS avg_days_late
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
```

2. 404 if no customer; feed features into `RiskModel.predict_default_probability`.
3. Map probability → `risk_flag` (LOW <0.30 ≤ MEDIUM <0.55 ≤ HIGH <0.70 ≤ CRITICAL).
4. In one transaction: insert `risk_assessments` row (+ `features_snapshot`) and an `audit_logs` row.
5. **If probability > 0.70**: create a `remediation_cases` row, commit, then launch the LangGraph workflow via FastAPI `BackgroundTasks` with `thread_id=case.id` (the HTTP response must not block on LLM latency).
6. Response: `{customer_id, default_probability, risk_flag, model_version, remediation_case_id | null}`.

### 4.4 Enterprise plumbing
- `app/config.py`: `pydantic-settings` `Settings` class — `DATABASE_URL`, `AWS_REGION`, `BEDROCK_MODEL_ID` (default `anthropic.claude-sonnet-5`), `MODEL_ARTIFACT_DIR`, `RISK_TRIGGER_THRESHOLD=0.70`, `LOG_LEVEL`, `CHECKPOINTER_BACKEND=memory|postgres`. **No secret ever appears in code**; `.env.example` documents all of them.
- `app/logging_conf.py`: `logging.config.dictConfig`, JSON formatter (timestamp, level, logger, message, plus contextual `customer_id`/`case_id` via `logging.LoggerAdapter` or contextvars). Uvicorn access logs routed through the same config.
- `app/exceptions.py`: `RiskGuardError` base → `DatabaseUnavailableError`, `ModelInferenceError`, `PolicyRetrievalError`, `LLMTimeoutError`, `LLMRefusalError`. FastAPI exception handlers map them to 503/500/502 with a stable error envelope `{error_code, message, request_id}`. DB calls wrapped to catch `sqlalchemy.exc.OperationalError`/`asyncpg` errors; LLM calls catch `anthropic.APITimeoutError`, `anthropic.RateLimitError`, `anthropic.APIStatusError` (typed exceptions, never string matching).

---

## 5. Component 3 — Stateful LangGraph agentic workflow

### 5.1 State (`app/agent/state.py`)

```python
class AgentState(TypedDict):
    customer_id: int
    case_id: str                      # == remediation_cases.id == thread_id
    risk_score: float
    risk_features: dict               # features_snapshot from the assessment
    state_code: str                   # drives policy retrieval
    retrieved_policies: list[str]     # RAG output (parent chunks)
    generated_remediation_plan: str | None
    compliance_status: Literal["pending", "passed", "needs_revision", "rejected"]
    compliance_notes: str | None
    human_approved: bool
    revision_count: int               # guard against infinite revision loops
```

(Spec's required fields — customer_id, risk_score, compliance_status, generated_remediation_plan, human_approved — all present; the extras make the graph actually function.)

### 5.2 Graph topology (`app/agent/graph.py`)

```
START
  └─▶ retrieve_context        (Node A — parent-child RAG over state policies)
        └─▶ plan_remediation  (Node B — Bedrock Sonnet 5 drafts restructuring contract)
              └─▶ compliance_check      (rule-based/LLM-lite validation of the draft)
                    ├─ passed ────────▶ human_review   (Node C — interrupt())
                    ├─ needs_revision ─▶ plan_remediation   (loop, max 3 via revision_count)
                    └─ rejected ──────▶ escalate ──▶ END
human_review (resumed with officer decision)
  ├─ approved ──▶ finalize ──▶ END     (persist plan, status=APPROVED, audit log)
  └─ denied  ───▶ plan_remediation     (one more revision with officer feedback)
```

- **Conditional edges**: `add_conditional_edges("compliance_check", route_compliance, {"passed": "human_review", "needs_revision": "plan_remediation", "rejected": "escalate"})` — deterministic routing off `compliance_status` + `revision_count` (spec requirement: route to END or a Revision loop).
- **Human-in-the-loop (Node C)**: use LangGraph's **`interrupt()`** primitive inside `human_review` (modern API, ≥ v0.2.31) rather than the older `interrupt_before=[...]` compile flag. The node calls `interrupt({...plan payload...})`; execution pauses, state persists to the checkpointer, `remediation_cases.status → 'AWAITING_HUMAN_REVIEW'`.
- **Checkpointer**: `MemorySaver` per spec, behind a factory:
  ```python
  def build_checkpointer(settings) -> BaseCheckpointSaver:
      # memory: demo/local. postgres: langgraph-checkpoint-postgres against Neon (prod).
  ```
- **Resume path**: `POST /api/v1/remediation/{case_id}/approve` (body: `{approved: bool, notes: str}`) → `graph.invoke(Command(resume={"approved": ..., "notes": ...}), config={"configurable": {"thread_id": case_id}})`. This endpoint *is* the compliance-officer sign-off simulation.
- Every node writes an `audit_logs` row (action = node name) — procedural integrity requirement.

### 5.3 Node A — mock Advanced RAG (`app/rag/retriever.py`)
- Corpus: 4–6 markdown files in `app/rag/documents/` — underwriting policy + per-state regulations (`policy_CA.md`, `policy_NY.md`, `policy_TX.md`, `underwriting_general.md`) with headers, clause numbers, realistic restructuring rules (max rate reduction, forbearance limits, disclosure requirements).
- **Parent-child chunking** (the concept the spec asks to demonstrate):
  - Parents: split on `##` headers (~1,500 chars) — the units returned to the LLM.
  - Children: 300–400-char windows within each parent, each holding `parent_id`.
  - Retrieval: score **children** (mock scoring = TF-IDF cosine via scikit-learn — no embedding API needed, keeps the demo offline), then return **deduplicated parents** of the top-k children. Small-to-large retrieval: precise matching, full-context generation.
  - Interface: `retrieve_policies(query: str, state_code: str, k: int = 3) -> list[ParentChunk]`.
- README notes the production swap: embeddings + pgvector on the same Neon instance, retriever interface unchanged.

### 5.4 Node B — LLM call (`app/agent/llm.py`)

```python
from anthropic import AnthropicBedrockMantle

client = AnthropicBedrockMantle(aws_region=settings.aws_region)  # SigV4 from env/instance role

def draft_remediation_plan(state: AgentState, policies: list[str]) -> str:
    response = client.messages.create(
        model=settings.bedrock_model_id,          # "anthropic.claude-sonnet-5"
        max_tokens=4096,
        system=REMEDIATION_SYSTEM_PROMPT,          # role, tone, required contract sections
        messages=[{"role": "user", "content": build_prompt(state, policies)}],
    )
```

- Wrap with timeout (client `timeout=60.0`) and retry-once on `APITimeoutError`; on second failure raise `LLMTimeoutError` → case status `ESCALATED`, audit-logged (spec: "catching LLM timeouts").
- No `temperature` (rejected on Sonnet 5). Prompt includes risk features, retrieved policy parents, and — on revision loops — the prior draft + compliance/officer feedback.
- `compliance_check` node: deterministic rule checks against the draft (required sections present, cites a policy clause, no rate below state floor from the policy docs) — cheap, testable, and shows judgment about *not* using an LLM where rules suffice.

---

## 6. Component 4 — enterprise practices & README

- **README.md** contains: the architecture diagram (Section 1), the data-flow narrative (tabular DB → XGBoost → LangGraph decision automation), quickstart (below), model-card summary from `metrics.json`, the demo script (curl sequence: seed → score high-risk customer → watch agent pause → approve → see finalized plan), and the production-upgrade notes (Postgres checkpointer, pgvector, MLflow).
- **Quickstart / setup steps**:
  1. `python -m venv .venv && pip install -e ".[ml,api,agent]"`
  2. `cp .env.example .env` — fill `DATABASE_URL` (Neon), `AWS_REGION` (Bedrock creds via standard AWS chain)
  3. `python ml/generate_dataset.py --rows 25000 --seed 42`
  4. `python ml/train.py` → prints metrics, writes `artifacts/v1/`
  5. `python scripts/seed_db.py` → tables + 50 demo customers (a few engineered to score > 0.70)
  6. `uvicorn app.main:app --reload` → hit `POST /api/v1/risk-assessment/7`, then `POST /api/v1/remediation/{case_id}/approve`
- **Testing**: pytest + `httpx.AsyncClient`; agent test asserts the graph pauses at `human_review` (checkpointer state exists) and reaches END after `Command(resume=...)`. LLM mocked in tests via a fake client injected into `llm.py`.
- **Deployment**: multi-stage Dockerfile (builder installs into a venv; runtime = `python:3.12-slim`, non-root user, `uvicorn --workers 2`). Fargate task behind an ALB; secrets from AWS Secrets Manager → env vars; Bedrock access via task IAM role (no keys in env). CI note: GitHub Actions — lint (ruff), tests, image build/push.

---

## 7. Build order (phases for the implementing agent)

| Phase | Deliverable | Acceptance check |
|---|---|---|
| 1 | Repo scaffold, `pyproject.toml`, config, logging, exceptions | `pip install -e .` clean; `python -c "from app.config import settings"` |
| 2 | `ml/`: dataset gen + training + predict wrapper | `train.py` prints F1/P/R/AUC, writes `roc_curve.png` + artifacts; AUC > 0.80 on synthetic data |
| 3 | DB models, DDL, seed script, engine | `seed_db.py` runs against Neon; tables exist; 50 customers seeded |
| 4 | Risk endpoint with CTE + audit logging | curl returns probability + flag; rows in `risk_assessments` and `audit_logs`; >0.70 creates a case |
| 5 | RAG retriever + policy documents | retriever unit test: query "rate reduction California" returns the CA parent chunk |
| 6 | LangGraph: state, nodes, graph, interrupt, resume endpoints | `test_agent_graph.py` passes end-to-end with mocked LLM; live smoke test against Bedrock |
| 7 | README, Dockerfile, tests polish | container builds & serves; full demo script works from a fresh clone |

Each phase is independently verifiable — do not start a phase until the previous one's check passes.

---

## 8. Environment variables (`.env.example`)

```
DATABASE_URL=postgresql+asyncpg://user:pass@ep-xxx.neon.tech/riskguard?ssl=require
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=anthropic.claude-sonnet-5
MODEL_ARTIFACT_DIR=ml/artifacts/v1
RISK_TRIGGER_THRESHOLD=0.70
CHECKPOINTER_BACKEND=memory
LOG_LEVEL=INFO
# AWS credentials come from the standard AWS chain (profile, env, or IAM role) — never hardcode
```
