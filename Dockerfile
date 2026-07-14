# syntax=docker/dockerfile:1

# Prerequisite: ml/artifacts/v1/ (model.joblib, metrics.json, threshold.json,
# feature_names.json) must already exist in the build context — run
# `python ml/train.py` before building. Training does not happen inside this
# image; it packages a pre-trained artifact, same as a real model-registry
# deploy would.

FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY app ./app
COPY ml ./ml

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[ml,api,agent,mcp]"


FROM python:3.12-slim AS runtime

RUN useradd --create-home --shell /bin/bash riskguard
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv

# Full source copied directly (not relied on via the builder's pip-installed
# copy) — hatchling's default wheel build does not guarantee non-.py data
# files (joblib/json/png/md) are bundled, and this app resolves several
# paths relative to CWD or `__file__` rather than via package resources.
# `python -m uvicorn` (below) puts CWD first on sys.path, so this copy is
# what actually gets imported at runtime.
COPY --chown=riskguard:riskguard app ./app
COPY --chown=riskguard:riskguard ml ./ml

USER riskguard

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
