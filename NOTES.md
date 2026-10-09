# Design notes

A running log of design decisions: what was chosen, what it was weighed against,
and why. Newest stage at the bottom.

---

## Stage 1: Scaffold

### Tooling: uv rather than pip-tools or Poetry
One tool manages the Python version, the virtualenv, and a cross-platform lockfile
(`uv.lock`). Installs are fast enough that CI and Docker builds don't need clever
caching to stay quick. `uv sync --frozen` fails if the lockfile is stale, so CI,
Docker and local runs install exactly the same versions.

### Python 3.13 (not 3.14)
3.14 was installed locally, but 3.13 has a longer track record for C-extension wheels
(asyncpg, uvloop) and the `python:3.13-slim` base image. Nothing here needs 3.14
features. Revisit later; it is a one-line change in `.python-version`, `pyproject.toml`
and the Dockerfile.

### SQLAlchemy 2.0 async + asyncpg (not raw asyncpg)
- Alembic autogenerate works from SQLAlchemy metadata, so the ORM pays for itself
  in migrations alone.
- asyncpg is still the driver, so we keep its performance.
- The graph queries (recursive CTEs) will be written as explicit SQL through the
  same engine, so the ORM never hides the queries that matter.
- Trade-off: two abstractions to understand, plus async-ORM pitfalls (no implicit lazy
  loading). Mitigated with `expire_on_commit=False` and explicit eager loads.

### Configuration: pydantic-settings, env vars only
All settings live in one `Settings` class, are validated at startup, and are read
from env vars, with `.env` only as a local convenience. DSNs are `SecretStr`, so they
never appear in reprs or logs (there is a test for this). The Alembic URL comes from
the same settings, never from `alembic.ini`, so the app and migrations can't target
different databases.

### Logging: structlog, JSON in containers
- Stdlib loggers (uvicorn, SQLAlchemy, Alembic) go through the same processor chain,
  so there is one format across the whole process.
- JSON on stdout is what Cloud Logging parses natively; local host runs can use the
  console renderer (`LOG_JSON=false`).
- A pure-ASGI middleware binds `request_id` (honouring incoming `X-Request-ID`) to
  every log line in the request and echoes it on the response. It is pure ASGI
  rather than `BaseHTTPMiddleware` because the latter has known contextvar and
  streaming quirks.
- Gotcha hit during setup: Docker Compose substitutes variables from `.env`, so a
  host-oriented `LOG_JSON=false` leaked into the containers. Containers now hardcode
  JSON so they always log like production.

### App factory + lifespan
`create_app(settings)` builds the app, and the DB engine and Redis client are created
in the lifespan and closed on shutdown. Tests pass their own settings, nothing
connects at import time, and Cloud Run instance shutdown closes pools cleanly.
Uvicorn runs it with `--factory`.

### Health: liveness vs readiness
`/health` never touches dependencies, so a database blip can't get healthy
containers restarted (that would turn an outage into a restart storm). `/health/ready`
checks Postgres and Redis with timeouts and returns 503 with per-dependency detail.

### Compose: one-shot `migrate` service
The API starts only after `alembic upgrade head` exits 0. This mirrors the planned
production shape: migrations run as a separate Cloud Run Job before a deploy, not
inside every app instance's startup, where concurrent instances would race.

### Versions: Postgres 17, Redis 7.4
Matched to what Cloud SQL and Memorystore offer, so local, CI and production run
the same majors.

### Container image
Multi-stage build: dependencies are installed in a layer separate from source (fast
rebuilds), the package is installed non-editable so the runtime stage needs only the
venv, and the image runs as a non-root user. `exec uvicorn` makes it PID 1, so it
receives Cloud Run's SIGTERM directly.

### Testing approach
Tests use `httpx.ASGITransport` against the real app (lifespan included) and real
Postgres/Redis, which are compose locally and service containers in CI. Mocks are
reserved for things we don't own (the npm registry, from stage 4).

### CI
There are three jobs: lint (ruff check + format), test (migrations apply, then pytest,
against Postgres 17 / Redis 7.4 service containers) and docker (the image builds).
Building the image in CI catches Dockerfile breakage before deploy day.
