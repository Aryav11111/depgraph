"""Match installed package versions against advisories; persist findings.

Two halves, like the graph builder:

* `match()` is pure: installed (scan, node, name, version) x affected entries
  -> the set of (scan, node, advisory) matches. An advisory affects a version
  if ANY of its entries for that package does (real data lists one package in
  several entries).
* The DB layer narrows candidates in SQL (`advisory_affected.package =
  ANY(names)` on the (ecosystem, package) index; withdrawn advisories excluded),
  evaluates semver in Python (Postgres can't), and reconciles the findings table.

Two entry points share all of that and differ only in *scope*:

* `match_scan(scan)`: every advisory vs one scan (new upload).
* `rematch_advisories(ids)`: these advisories vs every completed scan (after a
  sync changed them). This is how a newly published advisory reaches scans
  that were uploaded before it existed, and how a withdrawn one leaves them.

Reconciliation is an upsert plus a scoped delete, not delete-all-and-reinsert,
so a finding's `created_at` survives re-matches: it means "first detected".
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.advisories.matcher import InvalidRangeError, affects_entry
from depgraph.advisories.semver import InvalidVersionError, Version
from depgraph.db.models import AdvisorySyncRun, Scan

log = structlog.get_logger(__name__)

ECOSYSTEM = "npm"


@dataclass(frozen=True, slots=True)
class Installed:
    scan_id: uuid.UUID
    node_id: int
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class Entry:
    advisory_id: str
    package: str
    ranges: Sequence[dict[str, Any]]
    versions: Sequence[str]


Match = tuple[uuid.UUID, int, str]  # (scan_id, node_id, advisory_id)


@dataclass
class MatchResult:
    matches: set[Match] = field(default_factory=set)
    # Entries/versions we could not evaluate. Counted and logged, never guessed.
    bad_ranges: set[str] = field(default_factory=set)  # advisory ids
    bad_versions: set[str] = field(default_factory=set)  # "name@version"


def match(installed: Iterable[Installed], entries: Iterable[Entry]) -> MatchResult:
    by_package: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_package[e.package].append(e)

    result = MatchResult()
    for pkg in installed:
        candidates = by_package.get(pkg.name)
        if not candidates:
            continue
        try:
            version = Version.parse(pkg.version)
        except InvalidVersionError:
            result.bad_versions.add(f"{pkg.name}@{pkg.version}")
            continue
        for e in candidates:
            key = (pkg.scan_id, pkg.node_id, e.advisory_id)
            if key in result.matches:
                continue  # already matched via another entry of this advisory
            try:
                if affects_entry(version, e.ranges, e.versions):
                    result.matches.add(key)
            except InvalidRangeError:
                result.bad_ranges.add(e.advisory_id)
    return result


# ------------------------------------------------------------------- queries

_INSTALLED = """
    SELECT n.scan_id, n.id, p.name, pv.version
    FROM scan_nodes n
    JOIN package_versions pv ON pv.id = n.version_id
    JOIN packages p ON p.id = pv.package_id AND p.ecosystem = :eco
"""

_ENTRIES = """
    SELECT aa.advisory_id, aa.package, aa.ranges, aa.versions
    FROM advisory_affected aa
    JOIN advisories a ON a.id = aa.advisory_id
    WHERE aa.ecosystem = :eco AND aa.package = ANY(:names) AND a.withdrawn IS NULL
"""


async def _installed_in_scan(session: AsyncSession, scan_id: uuid.UUID) -> list[Installed]:
    rows = await session.execute(
        text(_INSTALLED + " WHERE n.scan_id = :scan"), {"eco": ECOSYSTEM, "scan": scan_id}
    )
    return [Installed(*r) for r in rows]


async def _installed_across_scans(session: AsyncSession, names: Sequence[str]) -> list[Installed]:
    rows = await session.execute(
        text(
            _INSTALLED
            + " JOIN scans s ON s.id = n.scan_id"
            + " WHERE p.name = ANY(:names) AND s.status = 'completed'"
        ),
        {"eco": ECOSYSTEM, "names": list(names)},
    )
    return [Installed(*r) for r in rows]


async def _entries(
    session: AsyncSession, names: Sequence[str], advisory_ids: Sequence[str] | None = None
) -> list[Entry]:
    sql, params = _ENTRIES, {"eco": ECOSYSTEM, "names": list(names)}
    if advisory_ids is not None:
        sql += " AND aa.advisory_id = ANY(:ids)"
        params["ids"] = list(advisory_ids)
    return [Entry(*r) for r in await session.execute(text(sql), params)]


# ------------------------------------------------------------- reconciliation


@dataclass(frozen=True, slots=True)
class MatchStats:
    matched: int
    added: int
    removed: int
    bad_ranges: int
    bad_versions: int


async def _reconcile(
    session: AsyncSession, scope_sql: str, scope_params: dict[str, Any], result: MatchResult
) -> MatchStats:
    """Make the findings within `scope` equal to `result.matches`."""
    rows = sorted(result.matches, key=lambda m: (str(m[0]), m[1], m[2]))  # stable lock order
    arrays = {
        "scans": [r[0] for r in rows],
        "nodes": [r[1] for r in rows],
        "advs": [r[2] for r in rows],
    }
    removed = await session.execute(
        text(
            f"DELETE FROM findings f WHERE {scope_sql} AND NOT EXISTS ("  # noqa: S608 (fixed fragments)
            " SELECT 1 FROM unnest(CAST(:nodes AS bigint[]), CAST(:advs AS text[])) AS t(n, a)"
            " WHERE t.n = f.scan_node_id AND t.a = f.advisory_id)"
        ),
        {**scope_params, **arrays},
    )
    added = await session.execute(
        text(
            "INSERT INTO findings (scan_id, scan_node_id, advisory_id)"
            " SELECT * FROM unnest(CAST(:scans AS uuid[]), CAST(:nodes AS bigint[]),"
            "                      CAST(:advs AS text[]))"
            " ON CONFLICT (scan_node_id, advisory_id) DO UPDATE SET updated_at = now()"
            " RETURNING (xmax = 0) AS inserted"
        ),
        arrays,
    )
    stats = MatchStats(
        matched=len(rows),
        added=sum(1 for (inserted,) in added if inserted),
        removed=removed.rowcount,
        bad_ranges=len(result.bad_ranges),
        bad_versions=len(result.bad_versions),
    )
    if result.bad_ranges or result.bad_versions:
        log.warning(
            "match_unevaluable",
            advisories=sorted(result.bad_ranges)[:20],
            versions=sorted(result.bad_versions)[:20],
        )
    return stats


async def match_scan(session: AsyncSession, scan_id: uuid.UUID) -> MatchStats:
    """(Re)compute every finding for one scan."""
    installed = await _installed_in_scan(session, scan_id)
    entries = await _entries(session, sorted({i.name for i in installed}))
    stats = await _reconcile(
        session, "f.scan_id = :scope_scan", {"scope_scan": scan_id}, match(installed, entries)
    )
    await session.execute(
        update(Scan).where(Scan.id == scan_id).values(matched_at=datetime.now(UTC))
    )
    log.info("scan_matched", scan_id=str(scan_id), **asdict(stats))
    return stats


async def rematch_advisories(session: AsyncSession, advisory_ids: Sequence[str]) -> MatchStats:
    """Re-evaluate these advisories against every completed scan.

    Covers new advisories (findings appear), changed ranges (findings appear or
    disappear) and withdrawals (the entries query excludes withdrawn advisories,
    so their findings are removed).
    """
    ids = sorted(set(advisory_ids))
    names = sorted(
        {
            r[0]
            for r in await session.execute(
                text("SELECT DISTINCT package FROM advisory_affected"
                     " WHERE ecosystem = :eco AND advisory_id = ANY(:ids)"),
                {"eco": ECOSYSTEM, "ids": ids},
            )
        }
    )  # fmt: skip
    installed = await _installed_across_scans(session, names) if names else []
    entries = await _entries(session, names, ids) if names else []
    return await _reconcile(
        session, "f.advisory_id = ANY(:scope_ids)", {"scope_ids": ids}, match(installed, entries)
    )


# ------------------------------------------------------------ after a sync

# Above this many changed advisories (i.e. a full load), re-matching every scan
# in full is cheaper than evaluating each advisory against every scan.
FULL_REMATCH_THRESHOLD = 20_000
REMATCH_CHUNK = 5_000


async def rematch_pending(sessionmaker: Any) -> int:
    """Re-match every completed sync run whose changes haven't reached scans yet.

    `advisory_sync_runs.rematched_at` makes this self-healing: if re-matching
    fails after a sync, the *next* sync picks those runs up again instead of
    their advisories silently never reaching existing scans. Returns runs done.
    """
    async with sessionmaker() as s:
        run_ids = list(
            await s.scalars(
                select(AdvisorySyncRun.id)
                .where(
                    AdvisorySyncRun.status == "completed", AdvisorySyncRun.rematched_at.is_(None)
                )
                .order_by(AdvisorySyncRun.id)
            )
        )
    for run_id in run_ids:
        await _rematch_run(sessionmaker, run_id)
        async with sessionmaker.begin() as s:
            await s.execute(
                update(AdvisorySyncRun)
                .where(AdvisorySyncRun.id == run_id)
                .values(rematched_at=datetime.now(UTC))
            )
    return len(run_ids)


async def _rematch_run(sessionmaker: Any, run_id: int) -> None:
    async with sessionmaker() as s:
        changed = list(
            await s.scalars(
                text("SELECT id FROM advisories WHERE last_changed_run_id = :r ORDER BY id"),
                {"r": run_id},
            )
        )
        scans = list(
            await s.scalars(select(Scan.id).where(Scan.status == "completed").order_by(Scan.id))
        )
    if not changed or not scans:
        return
    if len(changed) > FULL_REMATCH_THRESHOLD:
        for scan_id in scans:  # one transaction per scan: bounded, and progress sticks
            async with sessionmaker.begin() as s:
                await match_scan(s, scan_id)
        log.info("rematch_full", run_id=run_id, scans=len(scans), advisories=len(changed))
        return
    totals = defaultdict(int)
    for i in range(0, len(changed), REMATCH_CHUNK):
        async with sessionmaker.begin() as s:
            stats = await rematch_advisories(s, changed[i : i + REMATCH_CHUNK])
        for k, v in asdict(stats).items():
            totals[k] += v
    log.info("rematch_advisories", run_id=run_id, advisories=len(changed), **totals)
