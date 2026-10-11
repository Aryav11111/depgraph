"""ASGI entrypoint: `uvicorn --factory depgraph.main:create_app`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from redis.asyncio import Redis

from depgraph import __version__
from depgraph.api import health, scans
from depgraph.api.middleware import RequestContextMiddleware
from depgraph.core.config import Settings, get_settings
from depgraph.core.logging import configure_logging
from depgraph.db.session import create_engine, create_sessionmaker

log = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory: tests build an app with their own settings; prod uses env vars."""
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Pools are created once per process and closed on shutdown, so Cloud Run
        # instance teardown doesn't leave dangling Postgres connections.
        engine = create_engine(settings)
        app.state.sessionmaker = create_sessionmaker(engine)
        # Bytes in/out: the registry cache stores compressed payloads.
        app.state.redis = Redis.from_url(settings.redis_url.get_secret_value())
        log.info("startup", env=settings.app_env, version=__version__)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await engine.dispose()
            log.info("shutdown")

    app = FastAPI(title="depgraph", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health.router)
    app.include_router(scans.router)
    return app
