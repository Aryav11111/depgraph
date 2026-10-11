"""Findings: pure matching, then match/rematch against the test database."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select, text, update

from depgraph.advisories.ingest import full_load
from depgraph.core.config import Settings
from depgraph.db.models import AdvisoryRecord, AdvisorySyncRun, Finding, Scan
from depgraph.db.session import create_sessionmaker
from depgraph.findings.matching import (
    Entry,
    Installed,
    match,
    match_scan,
    rematch_advisories,
    rematch_pending,
)
from depgraph.graph.builder import run_build
from depgraph.lockfile.npm import parse_package_lock
from tests.helpers import lock, make_zip, osv_records, pkg

S = uuid.uuid4()


def entry(adv: str, package: str, events=None, versions=()) -> Entry:
    ranges = [{"type": "SEMVER", "events": events}] if events else []
    return Entry(adv, package, ranges, list(versions))


# ---------------------------------------------------------------------- pure


def test_match_basic():
    installed = [Installed(S, 1, "lodash", "4.17.20"), Installed(S, 2, "lodash", "4.17.21")]
    r = match(installed, [entry("A", "lodash", [{"introduced": "0"}, {"fixed": "4.17.21"}])])
    assert r.matches == {(S, 1, "A")}


def test_any_entry_of_an_advisory_is_enough():
    entries = [entry("A", "x", versions=["1.0.0"]), entry("A", "x", versions=["2.0.0"])]
    r = match([Installed(S, 1, "x", "2.0.0")], entries)
    assert r.matches == {(S, 1, "A")}


def test_bad_range_in_one_entry_does_not_hide_other_matches():
    entries = [
        entry("A", "x", [{"introduced": "0"}, {"fixed": "garbage"}]),
        entry("B", "x", [{"introduced": "0"}]),
    ]
    r = match([Installed(S, 1, "x", "1.0.0")], entries)
    assert r.matches == {(S, 1, "B")}
    assert r.bad_ranges == {"A"}


def test_unparseable_installed_version_is_reported_not_guessed():
    r = match([Installed(S, 1, "x", "not-semver")], [entry("A", "x", [{"introduced": "0"}])])
    assert r.matches == set() and r.bad_versions == {"x@not-semver"}


def test_packages_without_advisories_are_ignored():
    assert (
        match(
            [Installed(S, 1, "safe", "1.0.0")], [entry("A", "other", [{"introduced": "0"}])]
        ).matches
        == set()
    )


# ------------------------------------------------------------------------ db

# A lockfile pinned to versions with known advisories (fixtures/osv):
VULNERABLE = lock(
    {
        "node_modules/lodash": pkg("lodash", "4.17.20"),  # GHSA-35jh: fixed 4.17.21
        "node_modules/minimist": pkg("minimist", "1.2.6"),  # GHSA-xvch: fixed 1.2.6 (safe)
        "node_modules/mkdirp": pkg("mkdirp", "0.5.5", dependencies={"minimist": "^1.2.5"}),
        "node_modules/mkdirp/node_modules/minimist": pkg("minimist", "1.2.5"),  # vulnerable
        "node_modules/is-http2": pkg("is-http2", "1.2.0"),  # GHSA-2275: last_affected 1.2.0
        "node_modules/tostpro": pkg("tostpro", "100.6.0"),  # MAL-2026-17564
        "node_modules/left-pad": pkg("left-pad", "1.3.0"),  # no advisory
    },
    root={"dependencies": {"lodash": "^4", "minimist": "^1", "mkdirp": "^0.5",
                           "is-http2": "^1", "tostpro": "*", "left-pad": "^1"}},
)  # fmt: skip

EXPECTED = {
    ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm"),
    ("minimist", "1.2.5", "GHSA-xvch-5gv4-984h"),
    ("is-http2", "1.2.0", "GHSA-2275-rpf5-xv8h"),
    ("tostpro", "100.6.0", "MAL-2026-17564"),
}


@pytest.fixture
async def maker(engine):
    m = create_sessionmaker(engine)
    yield m
    async with m.begin() as s:
        await s.execute(delete(Scan).where(Scan.lockfile_sha256 == "m" * 64))
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))


async def _load_advisories(maker, tmp_path, docs=None) -> int:
    archive = make_zip(tmp_path / f"{uuid.uuid4().hex}.zip", docs or osv_records())
    return await full_load(maker, httpx.AsyncClient(), Settings(app_env="test"), archive=archive)


async def _scan(maker, doc=VULNERABLE) -> uuid.UUID:
    async with maker.begin() as s:
        scan = Scan(lockfile_sha256="m" * 64)
        s.add(scan)
        await s.flush()
        scan_id = scan.id
    await run_build(maker, scan_id, parse_package_lock(doc))
    return scan_id


async def _findings(maker, scan_id) -> set[tuple[str, str, str]]:
    async with maker() as s:
        rows = await s.execute(
            text(
                "SELECT p.name, pv.version, f.advisory_id FROM findings f"
                " JOIN scan_nodes n ON n.id = f.scan_node_id"
                " JOIN package_versions pv ON pv.id = n.version_id"
                " JOIN packages p ON p.id = pv.package_id WHERE f.scan_id = :s"
            ),
            {"s": scan_id},
        )
        return {tuple(r) for r in rows}


async def test_match_scan_finds_exactly_the_known_vulnerable_versions(maker, tmp_path):
    await _load_advisories(maker, tmp_path)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        stats = await match_scan(s, scan_id)
    assert await _findings(maker, scan_id) == EXPECTED
    assert (stats.matched, stats.added, stats.removed) == (4, 4, 0)
    async with maker() as s:
        assert (await s.get(Scan, scan_id)).matched_at is not None


async def test_rematching_is_idempotent_and_keeps_first_detected(maker, tmp_path):
    await _load_advisories(maker, tmp_path)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    async with maker() as s:
        first = {
            f.id: f.created_at for f in await s.scalars(select(Finding).filter_by(scan_id=scan_id))
        }
    async with maker.begin() as s:
        stats = await match_scan(s, scan_id)
    assert (stats.added, stats.removed) == (0, 0)
    async with maker() as s:
        again = {
            f.id: f.created_at for f in await s.scalars(select(Finding).filter_by(scan_id=scan_id))
        }
    assert again == first


async def test_advisory_published_after_the_scan_reaches_it(maker, tmp_path):
    # Scan uploaded while the advisory database knows nothing about lodash.
    docs = osv_records()
    lodash = docs.pop("GHSA-35jh-r3h4-6jhm")
    await _load_advisories(maker, tmp_path, docs)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    assert ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm") not in await _findings(maker, scan_id)

    # The advisory arrives in a later sync; re-matching just it updates old scans.
    await _load_advisories(maker, tmp_path, {"GHSA-35jh-r3h4-6jhm": lodash})
    async with maker.begin() as s:
        stats = await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert stats.added == 1
    assert await _findings(maker, scan_id) == EXPECTED


async def test_withdrawn_advisory_findings_are_removed(maker, tmp_path):
    await _load_advisories(maker, tmp_path)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
        await s.execute(
            update(AdvisoryRecord)
            .where(AdvisoryRecord.id == "GHSA-35jh-r3h4-6jhm")
            .values(withdrawn=datetime.now(UTC))
        )
    async with maker.begin() as s:
        stats = await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert stats.removed == 1
    assert await _findings(maker, scan_id) == EXPECTED - {
        ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm")
    }


async def test_narrowed_range_removes_only_that_finding(maker, tmp_path):
    docs = osv_records()
    await _load_advisories(maker, tmp_path, docs)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    # Upstream correction: lodash range now ends before 4.17.20.
    fixed = docs["GHSA-35jh-r3h4-6jhm"] | {"modified": "2099-01-01T00:00:00Z"}
    fixed["affected"] = [
        {"package": {"ecosystem": "npm", "name": "lodash"},
         "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.0"}]}]}
    ]  # fmt: skip
    await _load_advisories(maker, tmp_path, {"GHSA-35jh-r3h4-6jhm": fixed})
    async with maker.begin() as s:
        await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert len(await _findings(maker, scan_id)) == 3


async def test_rematch_only_touches_completed_scans(maker, tmp_path):
    await _load_advisories(maker, tmp_path)
    scan_id = await _scan(maker)
    async with maker.begin() as s:
        await s.execute(update(Scan).where(Scan.id == scan_id).values(status="failed"))
        await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert await _findings(maker, scan_id) == set()


async def test_rematch_pending_processes_each_run_once(maker, tmp_path):
    scan_id = await _scan(maker)  # scan first: no advisories yet
    await _load_advisories(maker, tmp_path)
    assert await rematch_pending(maker) == 1
    assert await _findings(maker, scan_id) == EXPECTED
    assert await rematch_pending(maker) == 0  # rematched_at recorded
    async with maker() as s:
        run = (await s.scalars(select(AdvisorySyncRun))).one()
    assert run.rematched_at is not None


async def test_failed_rematch_is_retried_by_the_next_call(maker, tmp_path, monkeypatch):
    from depgraph.findings import matching

    scan_id = await _scan(maker)
    await _load_advisories(maker, tmp_path)
    real = matching._rematch_run

    async def broken(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(matching, "_rematch_run", broken)
    with pytest.raises(RuntimeError):
        await rematch_pending(maker)
    monkeypatch.setattr(matching, "_rematch_run", real)
    assert await rematch_pending(maker) == 1  # still pending, so picked up again
    assert await _findings(maker, scan_id) == EXPECTED


async def test_full_load_triggers_whole_scan_rematch(maker, tmp_path, monkeypatch):
    from depgraph.findings import matching

    monkeypatch.setattr(matching, "FULL_REMATCH_THRESHOLD", 2)  # 7 changed > 2
    scan_id = await _scan(maker)
    await _load_advisories(maker, tmp_path)
    await rematch_pending(maker)
    assert await _findings(maker, scan_id) == EXPECTED
    async with maker() as s:
        assert (await s.get(Scan, scan_id)).matched_at > datetime.now(UTC) - timedelta(minutes=1)
