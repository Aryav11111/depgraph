"""Load OSV advisories into Postgres.

Full load = download the ecosystem's bulk export (`<base>/npm/all.zip`, ~220 MB,
~230k records), stream it to a temp file (never into memory), verify its MD5
against the bucket's x-goog-hash header, then upsert records in batches.

Upserts are idempotent and ordered by OSV's `modified` timestamp: a row is only
rewritten when the incoming record is newer, so re-running a load (after a crash
halfway, say) is safe and cheap, and a stale export can never roll data back.
Each batch commits on its own: a 230k-row single transaction would hold locks
and WAL for minutes, and progress survives a crash.
"""

import base64
import hashlib
import json
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from depgraph.advisories.osv import Advisory, InvalidAdvisoryError, parse_osv
from depgraph.core.config import Settings
from depgraph.core.errors import safe_error_text
from depgraph.db.models import AdvisorySyncRun

log = structlog.get_logger(__name__)


class IngestError(Exception):
    pass


@dataclass
class UpsertCounts:
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0  # unparseable or filtered out
    changed_ids: list[str] = field(default_factory=list)

    def add(self, other: "UpsertCounts") -> None:
        self.seen += other.seen
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.skipped += other.skipped
        self.changed_ids.extend(other.changed_ids)


# ------------------------------------------------------------------ download


async def download(
    http: httpx.AsyncClient, url: str, dest: Path, *, chunk_size: int = 1 << 20
) -> str | None:
    """Stream `url` to `dest`; verify GCS's MD5 if advertised. Returns the ETag."""
    md5 = hashlib.md5()  # noqa: S324 (integrity check against the bucket's own MD5, not security)
    async with http.stream("GET", url) as resp:
        resp.raise_for_status()
        expected = _gcs_md5(resp.headers.get_list("x-goog-hash"))
        with dest.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size):
                fh.write(chunk)
                md5.update(chunk)
        etag = resp.headers.get("etag")
    if expected is not None and md5.digest() != expected:
        raise IngestError(f"checksum mismatch downloading {url}")
    return etag


def _gcs_md5(values: list[str]) -> bytes | None:
    # x-goog-hash: crc32c=..., md5=<base64>; may arrive as one or several headers.
    for value in values:
        for part in value.split(","):
            algo, _, digest = part.strip().partition("=")
            if algo == "md5":
                return base64.b64decode(digest)
    return None


# -------------------------------------------------------------------- reading


def iter_zip(path: Path) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.is_dir() and info.filename.endswith(".json"):
                yield info.filename, zf.read(info)


def prepare(raw: bytes, *, include_malicious: bool) -> tuple[Advisory, str] | None:
    """Parse one record. Returns None for records we skip (logged by caller)."""
    doc = json.loads(raw)
    if b"\\u0000" in raw:
        # Postgres JSONB can't store U+0000. 6 of ~230k npm records contain it
        # (inside quoted malware payloads); replace rather than drop the record.
        doc = _replace_nul(doc)
        raw = json.dumps(doc).encode()
    advisory = parse_osv(doc)
    if advisory.kind == "malicious" and not include_malicious:
        return None
    return advisory, raw.decode()


def _replace_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, list):
        return [_replace_nul(v) for v in value]
    if isinstance(value, dict):
        return {_replace_nul(k): _replace_nul(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------- upsert

# Only rewrite when the incoming record is newer. `xmax = 0` distinguishes a
# fresh insert from a conflict-update in RETURNING (a well-known Postgres idiom:
# an inserted row version has no deleting/locking transaction yet).
_UPSERT_ADVISORIES = text(
    """
    INSERT INTO advisories (id, kind, summary, severity, cvss_v3_vector, cvss_v3_score,
                            cvss_v4_vector, published, modified, withdrawn, raw,
                            last_changed_run_id)
    SELECT t.id, t.kind, t.summary, t.severity, t.v3, t.v3_score, t.v4, t.published,
           t.modified, t.withdrawn, t.raw::jsonb, :run_id
    FROM unnest(CAST(:ids AS text[]), CAST(:kinds AS text[]), CAST(:summaries AS text[]),
                CAST(:severities AS text[]), CAST(:v3 AS text[]), CAST(:v3_scores AS numeric[]),
                CAST(:v4 AS text[]), CAST(:published AS timestamptz[]),
                CAST(:modified AS timestamptz[]), CAST(:withdrawn AS timestamptz[]),
                CAST(:raws AS text[]))
         AS t(id, kind, summary, severity, v3, v3_score, v4, published, modified, withdrawn, raw)
    ON CONFLICT (id) DO UPDATE SET
        kind = EXCLUDED.kind, summary = EXCLUDED.summary, severity = EXCLUDED.severity,
        cvss_v3_vector = EXCLUDED.cvss_v3_vector, cvss_v3_score = EXCLUDED.cvss_v3_score,
        cvss_v4_vector = EXCLUDED.cvss_v4_vector, published = EXCLUDED.published,
        modified = EXCLUDED.modified, withdrawn = EXCLUDED.withdrawn, raw = EXCLUDED.raw,
        last_changed_run_id = EXCLUDED.last_changed_run_id, updated_at = now()
    WHERE advisories.modified < EXCLUDED.modified
    RETURNING id, (xmax = 0) AS inserted
    """
)

_INSERT_ALIASES = text(
    """
    INSERT INTO advisory_aliases (advisory_id, alias)
    SELECT * FROM unnest(CAST(:ids AS text[]), CAST(:aliases AS text[]))
    ON CONFLICT DO NOTHING
    """
)

_INSERT_AFFECTED = text(
    """
    INSERT INTO advisory_affected (advisory_id, ecosystem, package, ranges, versions)
    SELECT t.id, t.eco, t.pkg, t.ranges::jsonb,
           ARRAY(SELECT jsonb_array_elements_text(t.versions::jsonb))
    FROM unnest(CAST(:ids AS text[]), CAST(:ecos AS text[]), CAST(:pkgs AS text[]),
                CAST(:ranges AS text[]), CAST(:versions AS text[]))
         AS t(id, eco, pkg, ranges, versions)
    """
)


async def upsert_batch(
    session: AsyncSession, batch: list[tuple[Advisory, str]], run_id: int
) -> UpsertCounts:
    """Upsert advisories; replace aliases/affected for the ones that changed."""
    # Within one batch an id should appear once; if an export ever repeats one,
    # keep the newest (ON CONFLICT can't touch the same row twice per statement).
    newest: dict[str, tuple[Advisory, str]] = {}
    for adv, raw in batch:
        if adv.id not in newest or newest[adv.id][0].modified < adv.modified:
            newest[adv.id] = (adv, raw)
    items = [newest[k] for k in sorted(newest)]  # sorted: consistent lock order
    advs = [a for a, _ in items]

    rows = await session.execute(
        _UPSERT_ADVISORIES,
        {
            "run_id": run_id,
            "ids": [a.id for a in advs],
            "kinds": [a.kind for a in advs],
            "summaries": [a.summary for a in advs],
            "severities": [a.severity for a in advs],
            "v3": [a.cvss_v3_vector for a in advs],
            "v3_scores": [a.cvss_v3_score for a in advs],
            "v4": [a.cvss_v4_vector for a in advs],
            "published": [a.published for a in advs],
            "modified": [a.modified for a in advs],
            "withdrawn": [a.withdrawn for a in advs],
            "raws": [raw for _, raw in items],
        },
    )
    counts = UpsertCounts(seen=len(batch))
    for adv_id, inserted in rows:
        counts.changed_ids.append(adv_id)
        if inserted:
            counts.inserted += 1
        else:
            counts.updated += 1
    counts.unchanged = len(items) - len(counts.changed_ids)
    if not counts.changed_ids:
        return counts

    changed = set(counts.changed_ids)
    await session.execute(
        text("DELETE FROM advisory_aliases WHERE advisory_id = ANY(:ids)"),
        {"ids": sorted(changed)},
    )
    await session.execute(
        text("DELETE FROM advisory_affected WHERE advisory_id = ANY(:ids)"),
        {"ids": sorted(changed)},
    )
    fresh = [a for a in advs if a.id in changed]
    alias_rows = [(a.id, alias) for a in fresh for alias in a.aliases]
    if alias_rows:
        await session.execute(
            _INSERT_ALIASES,
            {"ids": [r[0] for r in alias_rows], "aliases": [r[1] for r in alias_rows]},
        )
    affected_rows = [(a.id, af) for a in fresh for af in a.affected]
    if affected_rows:
        await session.execute(
            _INSERT_AFFECTED,
            {
                "ids": [i for i, _ in affected_rows],
                "ecos": [af.ecosystem for _, af in affected_rows],
                "pkgs": [af.package for _, af in affected_rows],
                "ranges": [
                    json.dumps([{"type": r.type, "events": list(r.events)} for r in af.ranges])
                    for _, af in affected_rows
                ],
                "versions": [json.dumps(list(af.versions)) for _, af in affected_rows],
            },
        )
    return counts


async def apply_records(
    sessionmaker: async_sessionmaker[AsyncSession],
    records: Iterator[tuple[str, bytes]],
    run_id: int,
    *,
    include_malicious: bool,
    batch_size: int,
) -> tuple[UpsertCounts, datetime | None]:
    """Parse and upsert records in committed batches. Returns counts and max modified."""
    total = UpsertCounts()
    cursor: datetime | None = None
    batch: list[tuple[Advisory, str]] = []
    started = time.perf_counter()

    async def flush() -> None:
        async with sessionmaker.begin() as session:
            total.add(await upsert_batch(session, batch, run_id))
        batch.clear()

    for name, raw in records:
        try:
            prepared = prepare(raw, include_malicious=include_malicious)
        except (ValueError, InvalidAdvisoryError) as exc:  # JSONDecodeError is a ValueError
            log.warning("advisory_skipped", record=name, error=str(exc))
            total.skipped += 1
            continue
        if prepared is None:
            total.skipped += 1
            continue
        adv = prepared[0]
        cursor = adv.modified if cursor is None else max(cursor, adv.modified)
        batch.append(prepared)
        if len(batch) >= batch_size:
            await flush()
            if total.seen % (batch_size * 20) == 0:
                log.info("advisory_ingest_progress", seen=total.seen,
                         elapsed_s=round(time.perf_counter() - started))  # fmt: skip
    if batch:
        await flush()
    return total, cursor


# ------------------------------------------------------------------ full load


async def full_load(
    sessionmaker: async_sessionmaker[AsyncSession],
    http: httpx.AsyncClient,
    settings: Settings,
    *,
    archive: Path | None = None,
) -> int:
    """Download (unless `archive` is given) and ingest the whole export. Returns run id."""
    run_id = await start_run(sessionmaker, "full")
    bound = log.bind(run_id=run_id, mode="full")
    try:
        with tempfile.TemporaryDirectory(prefix="osv-") as tmp:
            etag = None
            if archive is None:
                archive = Path(tmp) / "all.zip"
                url = f"{settings.osv_base_url.rstrip('/')}/{settings.osv_ecosystem}/all.zip"
                bound.info("osv_download_started", url=url)
                etag = await download(http, url, archive)
            counts, cursor = await apply_records(
                sessionmaker,
                iter_zip(archive),
                run_id,
                include_malicious=settings.osv_include_malicious,
                batch_size=settings.osv_batch_size,
            )
        await finish_run(sessionmaker, run_id, counts, cursor, etag)
    except Exception as exc:
        bound.exception("advisory_ingest_failed")
        await fail_run(sessionmaker, run_id, exc)
        raise
    bound.info(
        "advisory_ingest_completed",
        seen=counts.seen, inserted=counts.inserted, updated=counts.updated,
        unchanged=counts.unchanged, skipped=counts.skipped,
    )  # fmt: skip
    return run_id


async def start_run(sessionmaker: async_sessionmaker[AsyncSession], mode: str) -> int:
    async with sessionmaker.begin() as session:
        run = AdvisorySyncRun(mode=mode)
        session.add(run)
        await session.flush()
        return run.id


async def finish_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int,
    counts: UpsertCounts,
    cursor: datetime | None,
    etag: str | None,
) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            update(AdvisorySyncRun)
            .where(AdvisorySyncRun.id == run_id)
            .values(
                status="completed",
                finished_at=datetime.now(UTC),
                cursor=cursor,
                source_etag=etag,
                seen=counts.seen,
                inserted=counts.inserted,
                updated=counts.updated,
                unchanged=counts.unchanged,
            )
        )


async def fail_run(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: int, exc: BaseException
) -> None:
    async with sessionmaker.begin() as session:
        await session.execute(
            update(AdvisorySyncRun)
            .where(AdvisorySyncRun.id == run_id)
            .values(
                status="failed",
                finished_at=datetime.now(UTC),
                error=safe_error_text(exc),
            )
        )
