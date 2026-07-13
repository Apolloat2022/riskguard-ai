# Build Status

_Last updated: 2026-07-13_

## Summary

All 7 phases of `PLAN.md` are implemented. Full test suite passes (**19/19**), and
everything has now been verified **live**, including the built Docker image against the
real Neon Postgres instance. Only real Bedrock connectivity remains unverified (no AWS
credentials in this environment). This file exists so progress isn't lost if a
session/conversation doesn't carry over (e.g. across a PC restart).

## Phase-by-phase status

| Phase | Status | Notes |
|---|---|---|
| 1. Scaffold, `pyproject.toml`, config, logging, exceptions | ✅ Done | `pip install -e .` clean |
| 2. ML pipeline (`ml/`) | ✅ Done | ROC-AUC **0.899**; artifacts in `ml/artifacts/v1/` |
| 3. DB models, DDL, seed script, engine | ✅ Done | Verified live against Neon (schema, indexes, constraints, generated column) |
| 4. Risk-assessment endpoint (CTE + audit logging) | ✅ Done | Verified live via running uvicorn + curl |
| 5. RAG retriever + policy documents | ✅ Done | 5 unit tests passing, incl. the exact acceptance-check query |
| 6. LangGraph agent workflow | ✅ Done | 4 end-to-end tests + a full HTTP smoke test, all with a **mocked** LLM client (see below) |
| 7. README, Dockerfile, tests, CI | ✅ Done | Docker image built and smoke-tested live (see below) |

## Docker verification (2026-07-13)

- Docker Desktop was stuck on an outdated WSL2 kernel (last updated 4/25/2022,
  kernel 5.10.16) — `docker info` failed with "Docker Desktop is unable to start".
  Fixed by running `wsl --update` (installed the current WSL2 kernel), which
  unblocked the daemon.
- `docker compose up -d` brought up the local Postgres service cleanly
  (`postgres:16-alpine`, healthcheck passed).
- `docker build -t riskguard-ai .` succeeded using the existing
  `ml/artifacts/v1/` (already present from the earlier training run — no retrain
  needed). Final image: `riskguard-ai:latest`, 639MB.
- Container smoke test: ran the image with `--env-file .env` (real Neon
  `DATABASE_URL`), hit `POST /api/v1/risk-assessment/28` — got back
  `risk_flag: CRITICAL`, `default_probability: 0.995`, and a
  `remediation_case_id`, confirming live DB read/write from inside the
  container. The subsequent LangGraph agent step failed with
  `RuntimeError: Could not resolve AWS credentials from session` — expected,
  since no AWS credentials are configured in this environment; the app caught
  and logged the error without crashing.

## What's next

1. Optional: configure real AWS credentials and run one real request through
   `app/agent/llm.py`'s `draft_remediation_plan` to verify actual Bedrock connectivity —
   everything so far has used the fake client injected via `set_client()` in tests.

## Key environment facts (so a fresh session doesn't have to rediscover these)

- **This directory is not yet a git repository** (`git init` was never run). `.gitignore`
  and `.dockerignore` are written and ready for whenever it is.
- `.env` (gitignored) has a **real, working Neon `DATABASE_URL`** — already corrected for
  SQLAlchemy/asyncpg (`postgresql+asyncpg://...?ssl=require`; the pasted Neon console
  string uses `postgresql://...?sslmode=require&channel_binding=require`, which is
  libpq/psycopg syntax, not asyncpg's).
- The Neon DB already has the full schema plus **50 seeded demo customers**
  (`python scripts/seed_db.py` was already run). Every 7th customer (7, 14, 21, 28, 35, 42,
  49) is engineered high-risk. Customer 28 has already been exercised through the full
  risk-assessment → remediation-case → agent-pause → approve flow.
- The local venv (`.venv/`) was built with **Python 3.11** via `py -3.11`, not the system
  `python` (which resolves to 3.14 — too new for reliable XGBoost/scikit-learn wheels).
- Docker was not installed for most of this build; it's mid-install as of this note.
- No AWS credentials are configured in this environment — Bedrock has only been exercised
  through the fake-client test seam, never for real.
