# One Dockerfile for local dev AND the staging/prod build artifact.
# Docker-first extension, docs/architecture.md Sec5 (parent workspace):
# the image built here (via compose locally) is the SAME image promoted to
# staging/prod (WBS 0.8) - no separate local-only Dockerfile.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir uv

WORKDIR /srv

COPY pyproject.toml uv.lock README.md ./

# ---- dev stage: includes dev dependencies (pytest, ruff, mypy), used by
# docker-compose.yml for local dev + by CI (WBS 0.7) for lint/type/test ----
FROM base AS dev

RUN uv sync --locked --group dev
ENV PATH="/srv/.venv/bin:$PATH"

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- prod stage: runtime dependencies only, the staging/prod artifact ----
FROM base AS prod

RUN uv sync --locked --no-group dev
ENV PATH="/srv/.venv/bin:$PATH"

COPY app ./app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
