# RiskGuard AI

Enterprise risk analytics & automated remediation platform. Scores loan customers for
default risk with a trained XGBoost pipeline, and for high-risk customers, kicks off a
stateful LangGraph agent that retrieves the applicable policy documents (parent-child RAG),
drafts a remediation plan via Claude Sonnet 5 (Amazon Bedrock), runs it through a
deterministic compliance check, and pauses for human (compliance-officer) sign-off before
finalizing.

See [`PLAN.md`](./PLAN.md) for the full architectural blueprint this was built from.

## Architecture

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

**Skills demonstrated, mapped to the codebase:**

| Area | Where |
|---|---|
| Traditional ML / data science — feature engineering, imputation, scaling, class imbalance (`scale_pos_weight` + optional SMOTE), XGBoost, threshold tuning, ROC-AUC | `ml/` |
| Backend engineering — async FastAPI, SQLAlchemy 2.0 async, a hand-written CTE, Neon Postgres | `app/` |
| GenAI / RAG — parent-child chunking retriever over policy documents, TF-IDF small-to-large retrieval | `app/rag/` |
| Agentic AI — stateful LangGraph with conditional edges, bounded revision loop, checkpointer, human-in-the-loop `interrupt()` | `app/agent/` |

## Repository layout

```
riskguard-ai/
├── PLAN.md                # full architectural blueprint
├── pyproject.toml          # single project; deps grouped [ml] [api] [agent] [dev]
├── .env.example            # every env var, no values
├── Dockerfile               # multi-stage, uvicorn, for Fargate
├── docker-compose.yml       # local Postgres for dev (optional; Neon otherwise)
│
├── ml/                      # dataset generation, training, inference wrapper
├── app/
│   ├── main.py              # FastAPI app factory + lifespan
│   ├── config.py, logging_conf.py, exceptions.py
│   ├── db/                  # models, engine, the risk-assessment CTE
│   ├── api/                 # risk.py, remediation.py
│   ├── rag/                 # policy documents + retriever
│   ├── agent/                # state, nodes, graph, Bedrock LLM wrapper
│   └── schemas.py
├── scripts/                 # seed_db.py, run_migration.sql
└── tests/
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[ml,api,agent]"

cp .env.example .env   # fill DATABASE_URL (Neon or local docker-compose), AWS_REGION

python ml/generate_dataset.py --rows 25000 --seed 42
python ml/train.py                         # prints P/R/F1/ROC-AUC, writes ml/artifacts/v1/

python scripts/seed_db.py                  # tables + 50 demo customers (every 7th is high-risk)

uvicorn app.main:app --reload
```

Local Postgres via `docker-compose up -d` is supported as an alternative to Neon — just
point `DATABASE_URL` at `postgresql+asyncpg://riskguard:riskguard@localhost:5432/riskguard`
(no `ssl=` parameter needed for the local container).

## Demo script

Customer `28` is one of the seeded high-risk profiles (every 7th seeded customer — see
`scripts/seed_db.py`) — low credit score, high DTI, several severely-late payments.

```bash
# 1. Score a high-risk customer — creates a remediation case (probability > 0.70)
curl -s -X POST http://localhost:8000/api/v1/risk-assessment/28 | jq
# => {"customer_id":28,"default_probability":0.995...,"risk_flag":"CRITICAL","remediation_case_id":"<uuid>"}

# 2. The agent runs in the background: retrieves CA policy clauses, drafts a plan via
#    Bedrock, runs the deterministic compliance check, and pauses for human review.
CASE_ID=<uuid from step 1>
curl -s http://localhost:8000/api/v1/remediation/$CASE_ID | jq
# => {"status":"AWAITING_HUMAN_REVIEW","remediation_plan":"RATE_REDUCTION_BPS: ...", ...}

# 3. Simulate compliance-officer sign-off
curl -s -X POST http://localhost:8000/api/v1/remediation/$CASE_ID/approve | jq
# => {"status":"APPROVED","resolved_at":"..."}

# ...or request a revision instead:
curl -s -X POST http://localhost:8000/api/v1/remediation/$CASE_ID/reject \
  -H 'Content-Type: application/json' -d '{"notes": "Shorten the term extension."}' | jq
```

## Model card (from `ml/artifacts/v1/metrics.json`)

| Metric | Value |
|---|---|
| Precision (at tuned threshold) | 0.476 |
| Recall (at tuned threshold) | 0.477 |
| F1 | 0.476 |
| ROC-AUC | **0.899** |
| PR-AUC | 0.501 |
| Tuned classification threshold | 0.80 |
| Training rows | 25,000 (7.84% positive class) |
| `scale_pos_weight` vs. `--use-smote` | `scale_pos_weight` is the default; `--use-smote` (via an `imblearn.pipeline.Pipeline`, so resampling never leaks into inference) is available for comparison |

Note: the tuned classification threshold (0.80, F1-optimal on a held-out validation slice)
is independent of the *business* trigger threshold (`RISK_TRIGGER_THRESHOLD=0.70`) that
decides whether to open a remediation case — the two serve different purposes and are
allowed to diverge.

## Testing

```bash
pip install -e ".[dev]"
pytest
```

- `test_ml_pipeline.py` — trains on a small synthetic sample, asserts metrics/artifacts are well-formed
- `test_retriever.py` — parent-child RAG retrieval, state-scoping, fallback behavior
- `test_agent_graph.py` — the LangGraph workflow end-to-end (happy path, revision loop, human-denial-triggers-revision, max-revisions-exhausted-escalates) against a real database with a **mocked** Anthropic client
- `test_api.py` — the full HTTP surface via `httpx.AsyncClient` against the real app (real lifespan, real DB, mocked LLM)

All DB-backed tests create their own rows (prefixed `TEST-...`) and clean them up —
none of them touch the seeded demo data.

## Verification notes (this build)

Everything above was exercised against a **live Neon Postgres** instance during
development, including the full HTTP flow (risk assessment → remediation case → agent
pause → approve → finalize). Two things were **not** live-verified in this environment and
are worth confirming before treating this as production-ready:

- **Bedrock connectivity** — no AWS credentials were available in the dev environment, so
  `app/agent/llm.py`'s `AnthropicBedrockMantle` call has only been exercised against the
  fake client injected via `set_client()` in tests, never against real Bedrock. Run one
  real request through `draft_remediation_plan` once AWS credentials are configured.
- **The Docker image** — Docker wasn't installed in the dev environment, so the
  `Dockerfile` was written carefully (see its comments on why source is copied directly
  rather than relied on via the pip-installed wheel) but never actually built. Run
  `docker build -t riskguard-ai .` (after `python ml/train.py`) and smoke-test the
  container before deploying it.

## Production upgrade notes

- **Checkpointer**: `CHECKPOINTER_BACKEND=memory` (LangGraph `MemorySaver`) is demo-only —
  paused workflows are lost if the process restarts. Set `CHECKPOINTER_BACKEND=postgres` to
  use `langgraph-checkpoint-postgres` against the same Neon database instead
  (`app/agent/graph.py`'s checkpointer factory already supports both; only the `memory`
  path has been exercised end-to-end in this build).
- **RAG retrieval**: the TF-IDF retriever in `app/rag/retriever.py` is a fully-offline mock.
  The production swap is an embedding model + `pgvector` on the same Neon instance — the
  `retrieve_policies()` interface and `ParentChunk` shape are designed to stay the same.
- **Model registry**: `ml/artifacts/v1/` is a poor-man's model registry (a versioned
  directory). MLflow (or similar) would replace this for real deployment tracking.
- **Deployment**: containerize via the included `Dockerfile` and deploy to AWS Fargate
  behind an ALB — not Vercel (XGBoost/scikit-learn/pandas exceed Vercel's serverless bundle
  limits, and the LangGraph human-in-the-loop workflow needs a process that survives
  between requests). Secrets from AWS Secrets Manager → env vars; Bedrock access via the
  task's IAM role (never static keys). `.github/workflows/ci.yml` lints, tests, and builds
  the image; wire in `docker push` once a container registry is configured.
