"""Scan retention and rate-limit counter pruning."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from depgraph.core.config import Settings
from depgraph.db.models import RateLimitBucket, Scan
from depgraph.db.session import create_sessionmaker
from depgraph.maintenance import prune


async def test_prune_deletes_only_scans_past_retention(engine):
    maker = create_sessionmaker(engine)
    now = datetime.now(UTC)
    async with maker.begin() as s:
        old = Scan(lockfile_sha256="old", created_at=now - timedelta(days=8))
        new = Scan(lockfile_sha256="new", created_at=now - timedelta(days=6))
        stale = RateLimitBucket(key="t:stale", window_start=now - timedelta(days=2), hits=1)
        live = RateLimitBucket(key="t:live", window_start=now, hits=1)
        s.add_all([old, new, stale, live])
    try:
        # Retention off: scans kept, stale counters still cleaned.
        counts = await prune(maker, Settings(app_env="test", scan_retention_days=0))
        assert counts.scans == 0
        assert counts.rate_limit_buckets >= 1

        counts = await prune(maker, Settings(app_env="test", scan_retention_days=7))
        assert counts.scans >= 1
        async with maker() as s:
            left = set(await s.scalars(select(Scan.id).where(Scan.id.in_([old.id, new.id]))))
            keys = set(await s.scalars(select(RateLimitBucket.key).where(
                RateLimitBucket.key.like("t:%"))))  # fmt: skip
        assert left == {new.id}
        assert keys == {"t:live"}
    finally:
        async with maker.begin() as s:
            await s.execute(delete(Scan).where(Scan.id.in_([old.id, new.id])))
            await s.execute(delete(RateLimitBucket).where(RateLimitBucket.key.like("t:%")))
