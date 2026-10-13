"""Housekeeping run after each advisory sync: scan retention and stale counters.

A public demo accepts uploads from anyone, so without retention its database
grows without bound. Deleting a scan cascades to its nodes, edges and findings
(ON DELETE CASCADE). Deletes go in batches so no single statement holds locks
on a large share of the graph tables. The global package catalogue is kept: it
is shared, deduplicated and small.
"""

from dataclasses import dataclass

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from depgraph.core.config import Settings

log = structlog.get_logger(__name__)

BATCH = 200

_DELETE_OLD_SCANS = text("""
    DELETE FROM scans WHERE id IN (
        SELECT id FROM scans
        WHERE created_at < now() - make_interval(days => :days)
        ORDER BY created_at
        LIMIT :batch
    )
""")
# Longer than any rate-limit window, so live counters are never touched.
_DELETE_OLD_BUCKETS = text(
    "DELETE FROM rate_limit_buckets WHERE window_start < now() - interval '1 day'"
)


@dataclass(frozen=True, slots=True)
class PruneCounts:
    scans: int
    rate_limit_buckets: int


async def prune(maker: async_sessionmaker[AsyncSession], settings: Settings) -> PruneCounts:
    scans = 0
    if settings.scan_retention_days > 0:
        while True:
            async with maker.begin() as s:
                deleted = (
                    await s.execute(
                        _DELETE_OLD_SCANS,
                        {"days": settings.scan_retention_days, "batch": BATCH},
                    )
                ).rowcount
            scans += deleted
            if deleted < BATCH:
                break
    async with maker.begin() as s:
        buckets = (await s.execute(_DELETE_OLD_BUCKETS)).rowcount
    counts = PruneCounts(scans=scans, rate_limit_buckets=buckets)
    log.info(
        "prune_done",
        retention_days=settings.scan_retention_days,
        scans=scans,
        rate_limit_buckets=buckets,
    )
    return counts
