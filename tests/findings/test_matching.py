"""Findings: pure matching, then match/rematch against the test database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from depgraph.db.models import AdvisoryRecord, AdvisorySyncRun, Finding, Scan
from depgraph.findings.matching import (
    Entry,
    Installed,
    match,
    match_scan,
    rematch_advisories,
    rematch_pending,
)
from tests.findings.support import EXPECTED, findings_of, load_advisories, make_scan
from tests.helpers import osv_records

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


async def test_match_scan_finds_exactly_the_known_vulnerable_versions(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker)
    async with maker.begin() as s:
        stats = await match_scan(s, scan_id)
    assert await findings_of(maker, scan_id) == EXPECTED
    assert (stats.matched, stats.added, stats.removed) == (4, 4, 0)
    async with maker() as s:
        assert (await s.get(Scan, scan_id)).matched_at is not None


async def test_rematching_is_idempotent_and_keeps_first_detected(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker)
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
    await load_advisories(maker, tmp_path, docs)
    scan_id = await make_scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    assert ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm") not in await findings_of(maker, scan_id)

    # The advisory arrives in a later sync; re-matching just it updates old scans.
    await load_advisories(maker, tmp_path, {"GHSA-35jh-r3h4-6jhm": lodash})
    async with maker.begin() as s:
        stats = await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert stats.added == 1
    assert await findings_of(maker, scan_id) == EXPECTED


async def test_withdrawn_advisory_findings_are_removed(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker)
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
    assert await findings_of(maker, scan_id) == EXPECTED - {
        ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm")
    }


async def test_narrowed_range_removes_only_that_finding(maker, tmp_path):
    docs = osv_records()
    await load_advisories(maker, tmp_path, docs)
    scan_id = await make_scan(maker)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    # Upstream correction: lodash range now ends before 4.17.20.
    fixed = docs["GHSA-35jh-r3h4-6jhm"] | {"modified": "2099-01-01T00:00:00Z"}
    fixed["affected"] = [
        {"package": {"ecosystem": "npm", "name": "lodash"},
         "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.17.0"}]}]}
    ]  # fmt: skip
    await load_advisories(maker, tmp_path, {"GHSA-35jh-r3h4-6jhm": fixed})
    async with maker.begin() as s:
        await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert len(await findings_of(maker, scan_id)) == 3


async def test_rematch_only_touches_completed_scans(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker)
    async with maker.begin() as s:
        await s.execute(update(Scan).where(Scan.id == scan_id).values(status="failed"))
        await rematch_advisories(s, ["GHSA-35jh-r3h4-6jhm"])
    assert await findings_of(maker, scan_id) == set()


async def test_rematch_pending_processes_each_run_once(maker, tmp_path):
    scan_id = await make_scan(maker)  # scan first: no advisories yet
    await load_advisories(maker, tmp_path)
    assert await rematch_pending(maker) == 1
    assert await findings_of(maker, scan_id) == EXPECTED
    assert await rematch_pending(maker) == 0  # rematched_at recorded
    async with maker() as s:
        run = (await s.scalars(select(AdvisorySyncRun))).one()
    assert run.rematched_at is not None


async def test_failed_rematch_is_retried_by_the_next_call(maker, tmp_path, monkeypatch):
    from depgraph.findings import matching

    scan_id = await make_scan(maker)
    await load_advisories(maker, tmp_path)
    real = matching._rematch_run

    async def broken(*a, **k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(matching, "_rematch_run", broken)
    with pytest.raises(RuntimeError):
        await rematch_pending(maker)
    monkeypatch.setattr(matching, "_rematch_run", real)
    assert await rematch_pending(maker) == 1  # still pending, so picked up again
    assert await findings_of(maker, scan_id) == EXPECTED


async def test_full_load_triggers_whole_scan_rematch(maker, tmp_path, monkeypatch):
    from depgraph.findings import matching

    monkeypatch.setattr(matching, "FULL_REMATCH_THRESHOLD", 2)  # 7 changed > 2
    scan_id = await make_scan(maker)
    await load_advisories(maker, tmp_path)
    await rematch_pending(maker)
    assert await findings_of(maker, scan_id) == EXPECTED
    async with maker() as s:
        assert (await s.get(Scan, scan_id)).matched_at > datetime.now(UTC) - timedelta(minutes=1)
