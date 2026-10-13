"""Alembic environment, async edition.

The database URL comes from application Settings (i.e. env vars), never from
alembic.ini, so migrations and the app can't disagree about which DB they target
and no credentials live in the repo.
"""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import depgraph.db.models  # noqa: F401  (registers models on Base.metadata)
from depgraph.core.config import get_settings
from depgraph.core.logging import configure_logging
from depgraph.db.base import Base

settings = get_settings()
configure_logging(settings)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (`alembic upgrade head --sql`)."""
    context.configure(
        url=settings.sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # NullPool: a one-shot CLI process has no use for a connection pool.
    engine = create_async_engine(settings.sqlalchemy_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
