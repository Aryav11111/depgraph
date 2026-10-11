"""Incremental advisory sync, and the long-running worker loop around it.

OSV publishes `<base>/<ecosystem>/modified_id.csv`: every record id with its
`modified` timestamp, newest first. An incremental sync:

1. GETs the CSV with If-None-Match (unchanged export -> 304, nothing to do);
2. streams it only until it reaches records older than our cursor (minus an
   overlap window), so cost scales with what changed, not with the database;
3. fetches just those records (`<base>/<ecosystem>/<id>.json`) concurrently and
   upserts them through the same idempotent, modified-guarded path as a full load.

The overlap window re-checks records modified shortly *before* the cursor. A
record can land in the export with a `modified` older than ones we've already
seen (pipeline delays); re-processing is free because upserts are idempotent.

No cursor yet, or a backlog bigger than `osv_incremental_max` -> full load
instead (one 220 MB download beats tens of thousands of small requests).

Only one sync runs at a time, across every worker/API container: a Postgres
advisory lock, held on a dedicated connection for the sync's duration.
"""

import asyncio
import signal
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta

import httpx
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from depgraph.advisories.ingest import (
    UpsertCounts,
    apply_records,
    fail_run,
    finish_run,
    full_load,
    start_run,
)
from depgraph.advisories.osv import parse_timestamp
from depgraph.core.config import Settings
from depgraph.db.models import AdvisorySyncRun
from depgraph.db.session import create_sessionmaker

log = structlog.get_logger(__name__)

# Arbitrary but fixed 64-bit key for pg_try_advisory_lock ("depgraph" in ASCII).
SYNC_LOCK_KEY = 0x6465706772617068


@asynccontextmanager
async def advisory_lock(engine: AsyncEngine, key: int = SYNC_LOCK_KEY) -> AsyncIterator[bool]:
    """Yield True if we got the lock (non-blocking), False if someone else holds it.

    Session-level advisory locks belong to a connection, so the lock is taken and
    released on one dedicated connection that stays checked out meanwhile. If the
    process dies, Postgres drops the connection and the lock with it, so a
    crashed worker can never wedge syncing.
    """
    async with engine.connect() as conn:
        got = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}))
        try:
            yield got
        finally:
            if got:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                await conn.commit()


def ids_modified_after(lines: Iterable[str], since: datetime) -> list[tuple[datetime, str]]:
    """Parse modified_id.csv lines (newest first) up to the first one <= `since`."""
    out = []
    for line in lines:
        stamp, sep, adv_id = line.strip().partition(",")
        if not sep or not adv_id:
            continue  # blank / malformed line
        modified = parse_timestamp(stamp)
        if modified is None:
            continue
        if modified <= since:
            break  # sorted newest first: everything after this is older
        out.append((modified, adv_id))
    return out


async def sync(engine: AsyncEngine, http: httpx.AsyncClient, settings: Settings) -> int | None:
    """Run one sync. Returns its run id, or None if another sync holds the lock."""
    async with advisory_lock(engine) as got:
        if not got:
            log.info("advisory_sync_skipped", reason="another sync is running")
            return None
        return await _sync_locked(engine, http, settings)


async def _sync_locked(engine: AsyncEngine, http: httpx.AsyncClient, settings: Settings) -> int:
    maker = create_sessionmaker(engine)
    async with maker() as s:
        cursor = await s.scalar(
            select(AdvisorySyncRun.cursor)
            .where(AdvisorySyncRun.status == "completed", AdvisorySyncRun.cursor.is_not(None))
            .order_by(AdvisorySyncRun.cursor.desc())
            .limit(1)
        )
        csv_etag = await s.scalar(
            select(AdvisorySyncRun.source_etag)
            .where(AdvisorySyncRun.status == "completed", AdvisorySyncRun.mode == "incremental")
            .order_by(AdvisorySyncRun.id.desc())
            .limit(1)
        )
    if cursor is None:
        log.info("advisory_sync_full", reason="no cursor yet")
        return await full_load(maker, http, settings)

    base = f"{settings.osv_base_url.rstrip('/')}/{settings.osv_ecosystem}"
    run_id = await start_run(maker, "incremental")
    bound = log.bind(run_id=run_id, mode="incremental")
    try:
        headers = {"If-None-Match": csv_etag} if csv_etag else {}
        since = cursor - timedelta(seconds=settings.osv_sync_overlap_s)
        async with http.stream("GET", f"{base}/modified_id.csv", headers=headers) as resp:
            if resp.status_code == 304:
                bound.info("advisory_sync_unchanged")
                await finish_run(maker, run_id, UpsertCounts(), cursor, csv_etag)
                return run_id
            resp.raise_for_status()
            etag = resp.headers.get("etag")
            lines: list[str] = []
            async for line in resp.aiter_lines():
                lines.append(line)
                stamp = parse_timestamp(line.partition(",")[0])
                if stamp is not None and stamp <= since:
                    break  # stop downloading: the rest is older
        changed = ids_modified_after(lines, since)

        if len(changed) > settings.osv_incremental_max:
            bound.info("advisory_sync_full", reason="backlog too large", changed=len(changed))
            await finish_run(maker, run_id, UpsertCounts(), cursor, csv_etag)
            return await full_load(maker, http, settings)

        records = await _fetch_records(http, base, [i for _, i in changed], settings)
        counts, newest = await apply_records(
            maker,
            iter(records),
            run_id,
            include_malicious=settings.osv_include_malicious,
            batch_size=settings.osv_batch_size,
        )
        new_cursor = max(cursor, newest) if newest else cursor
        await finish_run(maker, run_id, counts, new_cursor, etag)
    except Exception as exc:
        bound.exception("advisory_sync_failed")
        await fail_run(maker, run_id, exc)
        raise
    bound.info(
        "advisory_sync_completed",
        listed=len(changed), fetched=len(records), inserted=counts.inserted,
        updated=counts.updated, unchanged=counts.unchanged, skipped=counts.skipped,
    )  # fmt: skip
    return run_id


async def _fetch_records(
    http: httpx.AsyncClient, base: str, ids: list[str], settings: Settings
) -> list[tuple[str, bytes]]:
    sem = asyncio.Semaphore(settings.osv_fetch_concurrency)

    async def one(adv_id: str) -> tuple[str, bytes] | None:
        async with sem:
            for attempt in range(settings.osv_fetch_retries + 1):
                try:
                    resp = await http.get(f"{base}/{adv_id}.json")
                except httpx.TransportError as exc:
                    error = repr(exc)
                else:
                    if resp.status_code == 200:
                        return f"{adv_id}.json", resp.content
                    if resp.status_code == 404:
                        log.warning("advisory_record_missing", advisory=adv_id)
                        return None
                    if resp.status_code < 500 and resp.status_code != 429:
                        resp.raise_for_status()
                    error = f"HTTP {resp.status_code}"
                if attempt < settings.osv_fetch_retries:
                    await asyncio.sleep(min(30.0, 0.5 * 2**attempt))
            raise RuntimeError(f"fetching {adv_id} failed: {error}")

    results = await asyncio.gather(*(one(i) for i in ids))
    return [r for r in results if r is not None]


# ------------------------------------------------------------------ worker


async def run_worker(engine: AsyncEngine, http: httpx.AsyncClient, settings: Settings,
                     stop: asyncio.Event) -> None:  # fmt: skip
    """Sync, then sleep `osv_sync_interval_s`, until `stop` is set.

    A failed sync is logged and retried next interval; it never kills the
    worker. Stopping takes effect between syncs (or immediately while sleeping),
    so SIGTERM never leaves a half-applied batch.
    """
    log.info("advisory_worker_started", interval_s=settings.osv_sync_interval_s)
    while not stop.is_set():
        try:
            await sync(engine, http, settings)
        except Exception:
            log.exception("advisory_worker_sync_error")
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.osv_sync_interval_s)
    log.info("advisory_worker_stopped")


def install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows event loops don't support signal handlers; Ctrl+C still works.
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
