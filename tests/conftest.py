"""Shared fixtures.

Tests exercise the real ASGI app in-process via httpx.ASGITransport (no network
socket, no uvicorn) with the lifespan run explicitly, so the same startup code
path that runs in production builds the DB and Redis clients.

Integration tests need the Postgres/Redis from `make up` locally, or the
service containers in CI; connection details come from DATABASE_URL/REDIS_URL.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from depgraph.core.config import Settings
from depgraph.main import create_app


@asynccontextmanager
async def make_client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def settings() -> Settings:
    return Settings(app_env="test", log_json=False)


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    async with make_client(settings) as c:
        yield c
