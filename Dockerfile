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

# Dependencies come from the lockfile, not from resolving the pyproject extras
# at build time. `pip install ".[...]"` re-resolves against PyPI on every build,
# which is how a fresh image silently picked up mcp 2.0.0 and crashed on import
# while the repo and the local venv were both unchanged.
#
# Regenerate with (--universal is load-bearing: without it the resolution is
# tied to whichever platform ran it and uvloop is emitted unmarked, making the
# lock uninstallable on Windows):
#   docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
#     "pip install -q uv && uv pip compile --universal --extra ml --extra api \
#      --extra agent --extra mcp pyproject.toml -o requirements.lock"
#
# Copied before the source so the 92-package layer is cached across source-only
# changes.
COPY requirements.lock ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md ./
COPY app ./app
COPY ml ./ml

# --no-deps: every dependency is already installed at its locked version above,
# and without this flag pip would re-resolve the extras and defeat the lock.
RUN pip install --no-cache-dir --no-deps .


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
