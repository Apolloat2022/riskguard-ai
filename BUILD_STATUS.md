# Build Status

_Last updated: 2026-07-13_

## Summary

All 7 phases of `PLAN.md` are implemented. Full test suite passes (**19/19**), and
everything has now been verified **live** end-to-end: Docker image, real Neon Postgres,
and real AWS Bedrock (Claude) — nothing left running only against mocks. Now mid-deployment
to AWS (ECS Fargate) for a public portfolio demo; see `AWS_DEPLOYMENT.md`. This file exists
so progress isn't lost if a session/conversation doesn't carry over.

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

## Real Bedrock verification (2026-07-13)

- Created IAM user `riskguard-deployer` (access-key based, `AdministratorAccess` for now —
  tighten before this is anything but a personal portfolio account), configured locally via
  `aws configure`.
- **Two separate Bedrock access gates, easy to conflate**:
  1. Raw model/IAM access — `anthropic.claude-sonnet-5` (the bare model ID) is gated behind
     AWS Sales approval on this account; `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
     (an inference-profile ID — required for on-demand invocation of newer models,
     `aws bedrock list-inference-profiles` to find these) worked immediately.
  2. Anthropic's own one-time "use case details" form — separate from IAM/model access
     entirely. Manual-approval "Model access" console page is retired, but the form still
     exists: classic Bedrock console (drop `-mantle` from the URL) → Model catalog → pick
     a Claude model → banner has a **Submit use case details** button. Takes a few minutes
     to propagate after submitting.
- **Found and fixed a real bug while verifying**: `app/agent/llm.py` constructed
  `AnthropicBedrockMantle` (the newer bedrock-mantle-endpoint client). Mantle access turned
  out to be gated separately from classic Bedrock runtime access and returned 403 for every
  model on this account regardless of the use-case form. Switched to the classic
  `AnthropicBedrock` client (same inference-profile model ID) — confirmed working via a
  live container run: `POST .../invoke "HTTP/1.1 200 OK"` for customer 28's remediation plan.
  `BEDROCK_MODEL_ID` in `.env`/`.env.example` reflects the working inference-profile ID.

## Key environment facts (so a fresh session doesn't have to rediscover these)

- Now a git repository, pushed to https://github.com/Apolloat2022/riskguard-ai.
  `Bedrock screenshot.jpg`, `Docer installation.jpg`, `gettingstarted*.jpg`,
  `models.jpg`, `submittoanthropic.jpg` and similar working screenshots are gitignored
  (`*.jpg`) — they contain the AWS account ID/ARNs and shouldn't be pushed.
- `.env` (gitignored) has a **real, working Neon `DATABASE_URL`** — already corrected for
  SQLAlchemy/asyncpg (`postgresql+asyncpg://...?ssl=require`; the pasted Neon console
  string uses `postgresql://...?sslmode=require&channel_binding=require`, which is
  libpq/psycopg syntax, not asyncpg's).
- The Neon DB already has the full schema plus **50 seeded demo customers**
  (`python scripts/seed_db.py` was already run). Every 7th customer (7, 14, 21, 28, 35, 42,
  49) is engineered high-risk. Customer 28 has already been exercised through the full
  risk-assessment → remediation-case → agent-pause → approve flow, including with a real
  Bedrock-drafted remediation plan.
- The local venv (`.venv/`) was built with **Python 3.11** via `py -3.11`, not the system
  `python` (which resolves to 3.14 — too new for reliable XGBoost/scikit-learn wheels).
- AWS CLI v2 is installed at `C:\Program Files\Amazon\AWSCLIV2\` — needed admin rights to
  install (the installer's default machine-wide install fails silently with error 1925
  otherwise); PATH may need refreshing per-shell (`$env:Path` in PowerShell, `export
  PATH=".../AWSCLIV2:$PATH"` in Git Bash) since it was installed mid-session.
- Currently mid-deployment to AWS (ECS Fargate) for a public portfolio demo — see
  `AWS_DEPLOYMENT.md` for the full plan and progress.
