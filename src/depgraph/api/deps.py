"""FastAPI dependencies: hand request handlers the shared clients from app.state."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.db.session import session_scope
from depgraph.registry.npm import NpmRegistryClient


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in session_scope(request.app.state.sessionmaker):
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def get_npm(request: Request) -> NpmRegistryClient:
    return request.app.state.npm


NpmDep = Annotated[NpmRegistryClient, Depends(get_npm)]
