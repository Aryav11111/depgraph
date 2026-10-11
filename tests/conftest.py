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
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from depgraph.core.config import Settings
from depgraph.db.session import create_engine
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


@pytest.fixture(scope="session")
def migrated(request) -> None:
    """Bring the test database to the latest schema once per session."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(request.config.rootpath / "alembic.ini")), "head")


@pytest.fixture(scope="session")
async def engine(migrated) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(Settings(app_env="test"))
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose work is always rolled back: tests never see each other's rows.

    The outer transaction is never committed; `create_savepoint` turns the code
    under test's commit()/rollback() into SAVEPOINT operations inside it.
    """
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
