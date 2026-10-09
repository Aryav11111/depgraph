"""Liveness and readiness probes.

/health       -> the process is up (liveness). Never touches dependencies, so a
                 database blip doesn't get a healthy container killed and restarted.
/health/ready -> the process can serve traffic (readiness): Postgres and Redis respond.
"""

import asyncio

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from depgraph.api.deps import RedisDep, SessionDep

router = APIRouter(tags=["health"])
log = structlog.get_logger(__name__)

CHECK_TIMEOUT_S = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(session: SessionDep, redis: RedisDep, response: Response) -> dict[str, object]:
    checks: dict[str, str] = {}

    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), CHECK_TIMEOUT_S)
        checks["postgres"] = "ok"
    except Exception as exc:
        log.warning("readiness_check_failed", dependency="postgres", error=repr(exc))
        checks["postgres"] = "error"

    try:
        await asyncio.wait_for(redis.ping(), CHECK_TIMEOUT_S)
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("readiness_check_failed", dependency="redis", error=repr(exc))
        checks["redis"] = "error"

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "checks": checks}
