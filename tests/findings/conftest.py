from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from depgraph.db.models import AdvisoryRecord, AdvisorySyncRun, Scan
from depgraph.db.session import create_sessionmaker
from tests.findings.support import SCAN_MARKER


@pytest.fixture
async def maker(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Committing sessionmaker; scans and advisories it creates are removed after."""
    m = create_sessionmaker(engine)
    yield m
    async with m.begin() as s:  # test database only (see tests/conftest.py)
        await s.execute(delete(Scan).where(Scan.lockfile_sha256 == SCAN_MARKER))
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))
