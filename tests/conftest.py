"""Shared fixtures.

Tests exercise the real ASGI app in-process via httpx.ASGITransport (no network
socket, no uvicorn) with the lifespan run explicitly, so the same startup code
path that runs in production builds the DB and Redis clients.

Integration tests need the Postgres/Redis from `make up` locally, or the
service containers in CI; connection details come from DATABASE_URL/REDIS_URL.

Tests never touch the configured database itself: they use `<name>_test` on the
same server (created on demand), so a local dev database holding real data (e.g.
a full advisory load) can't be modified or wiped by test cleanup.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from depgraph.core.config import Settings
from depgraph.db.session import create_engine
from depgraph.main import create_app

# Redirect every Settings() in this process to the test database. Done at import
# time, before any fixture or app reads configuration.
_url = make_url(Settings().database_url.get_secret_value())
if not (_url.database or "").endswith("_test"):
    _url = _url.set(database=f"{_url.database}_test")
os.environ["DATABASE_URL"] = _url.render_as_string(hide_password=False)
# Suites upload many lockfiles from one address; rate-limit tests opt back in.
os.environ["UPLOAD_LIMIT_PER_IP"] = "0"
os.environ["UPLOAD_LIMIT_GLOBAL"] = "0"
TEST_DATABASE_URL = _url


async def _ensure_database(url) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
            )
            if not exists:
                # Identifier comes from our own config, not user input; quoted anyway.
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        await admin.dispose()


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


@pytest.fixture(scope="session", autouse=True)
def migrated(request) -> None:
    """Create the test database if needed and bring it to the latest schema."""
    from alembic import command
    from alembic.config import Config

    from depgraph.core.config import get_settings

    asyncio.run(_ensure_database(TEST_DATABASE_URL))
    get_settings.cache_clear()  # alembic's env.py reads the (now redirected) env
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
