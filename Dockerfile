# syntax=docker/dockerfile:1.7

# ---- builder: resolve and install dependencies into a self-contained venv ----
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.20 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Dependencies first, project second: editing source code doesn't bust the
# (slow) dependency layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY README.md ./
COPY src ./src
# --no-editable installs the package into site-packages, so the runtime image
# doesn't need the src/ tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- runtime: no compiler, no uv, non-root ----
FROM python:3.13-slim AS runtime

RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER app
EXPOSE 8080

# Cloud Run injects $PORT; exec so uvicorn is PID 1 and receives SIGTERM directly.
CMD ["sh", "-c", "exec uvicorn --factory depgraph.main:create_app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
