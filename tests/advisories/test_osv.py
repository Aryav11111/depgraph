"""OSV normalization against real records (tests/fixtures/osv, verbatim from OSV)."""

import json
from datetime import UTC, datetime

import pytest

from depgraph.advisories.osv import InvalidAdvisoryError, parse_osv
from tests.helpers import FIXTURES

OSV = FIXTURES / "osv"


def load(adv_id: str) -> dict:
    return json.loads((OSV / f"{adv_id}.json").read_text(encoding="utf-8"))


def test_github_advisory():
    a = parse_osv(load("GHSA-35jh-r3h4-6jhm"))  # lodash command injection
    assert (a.id, a.kind, a.severity) == ("GHSA-35jh-r3h4-6jhm", "vulnerability", "HIGH")
    assert "CVE-2021-23337" in a.aliases
    assert a.id not in a.aliases
    assert list(a.aliases) == sorted(a.aliases)
    assert a.summary == "Command Injection in lodash"
    (lodash,) = [af for af in a.affected if af.package == "lodash"]
    assert lodash.ranges[0].type == "SEMVER"
    assert {"fixed": "4.17.21"} in lodash.ranges[0].events
    assert a.published == datetime(2021, 5, 6, 16, 5, 51, tzinfo=UTC)
    assert a.withdrawn is None
    assert a.raw["id"] == a.id  # full record kept verbatim


def test_nanosecond_timestamps_are_truncated_not_rejected():
    a = parse_osv(load("GHSA-35jh-r3h4-6jhm"))  # modified: ...T03:49:04.067984836Z
    assert a.modified.tzinfo is not None
    assert a.modified.microsecond == 67984


def test_malicious_package_record():
    a = parse_osv(load("MAL-2026-17564"))
    assert (a.kind, a.severity) == ("malicious", "CRITICAL")
    assert a.affected[0].package == "tostpro"


def test_withdrawn_advisory_keeps_its_withdrawal_time():
    assert parse_osv(load("GHSA-236c-vhj4-gfxg")).withdrawn is not None


def test_last_affected_event_preserved():
    (af,) = parse_osv(load("GHSA-2275-rpf5-xv8h")).affected
    assert af.ranges[0].events == ({"introduced": "0"}, {"last_affected": "1.2.0"})


def test_versions_only_entries_for_one_package_stay_separate():
    # Real data: one package listed in four affected entries, one version each.
    # The matcher must OR across entries, not assume one entry per package.
    affected = parse_osv(load("GHSA-27f5-xjrr-q9ff")).affected
    assert {af.package for af in affected} == {"@opensearch-project/opensearch"}
    assert [af.versions for af in affected] == [("3.5.3",), ("3.6.2",), ("3.7.0",), ("3.8.0",)]
    assert all(af.ranges == () for af in affected)


def test_multi_ecosystem_record_keeps_only_npm_with_all_ranges():
    # Real data: the same advisory covers Hex and npm; four prerelease-bounded ranges.
    (af,) = parse_osv(load("EEF-CVE-2026-56812")).affected
    assert (af.ecosystem, af.package) == ("npm", "phoenix")
    assert [r.events for r in af.ranges] == [
        ({"introduced": "1.2.0-rc.0"}, {"fixed": "1.5.15"}),
        ({"introduced": "1.6.0-rc.0"}, {"fixed": "1.6.17"}),
        ({"introduced": "1.7.0-rc.0"}, {"fixed": "1.7.24"}),
        ({"introduced": "1.8.0-rc.0"}, {"fixed": "1.8.9"}),
    ]


# ---------------------------------------------------------------- synthetic


def record(**overrides) -> dict:
    base = {
        "id": "GHSA-test-test-test",
        "modified": "2026-01-01T00:00:00Z",
        "affected": [
            {
                "package": {"ecosystem": "npm", "name": "pkg"},
                "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "1.0.0"}]}],
            }
        ],
    }
    return base | overrides


@pytest.mark.parametrize(
    ("db_specific", "severity", "expected"),
    [
        ({"severity": "MEDIUM"}, [], "MODERATE"),  # GHSA alias for the same band
        ({"severity": "low"}, [], "LOW"),
        # no curated label: derived from the CVSS v3 score (9.8 -> CRITICAL)
        ({}, [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
         "CRITICAL"),
        ({}, [{"type": "CVSS_V3", "score": "garbage"}], "UNKNOWN"),
        ({}, [], "UNKNOWN"),
        # curated label wins over the computed score
        ({"severity": "LOW"},
         [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}], "LOW"),
    ],
)  # fmt: skip
def test_severity_resolution(db_specific, severity, expected):
    a = parse_osv(record(database_specific=db_specific, severity=severity))
    assert a.severity == expected


def test_cvss_vectors_and_score():
    v3 = "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    v4 = "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
    a = parse_osv(
        record(severity=[{"type": "CVSS_V3", "score": v3}, {"type": "CVSS_V4", "score": v4}])
    )
    assert (a.cvss_v3_vector, a.cvss_v3_score, a.cvss_v4_vector) == (v3, 9.8, v4)


def test_invalid_cvss_vector_keeps_vector_but_no_score():
    a = parse_osv(record(severity=[{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N"}]))
    assert a.cvss_v3_vector == "CVSS:3.1/AV:N" and a.cvss_v3_score is None


def test_other_ecosystems_are_dropped():
    a = parse_osv(
        record(
            affected=[
                {"package": {"ecosystem": "PyPI", "name": "django"}, "versions": ["1.0"]},
                {"package": {"ecosystem": "npm", "name": "pkg"}, "versions": ["1.0.0"]},
            ]
        )
    )
    assert [af.package for af in a.affected] == ["pkg"]


def test_malformed_affected_parts_are_skipped_not_fatal():
    a = parse_osv(
        record(
            affected=[
                "nonsense",
                {"package": {"ecosystem": "npm"}},  # no name
                {"package": {"ecosystem": "npm", "name": "ok"},
                 "ranges": [{"type": "SEMVER", "events": []}, {"events": [{"fixed": "1"}]}]},
            ]
        )
    )  # fmt: skip
    (af,) = a.affected
    assert af.package == "ok" and af.ranges == ()  # empty/typeless ranges dropped


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"id": "", "modified": "2026-01-01T00:00:00Z"},
        {"id": "X"},  # no modified
        {"id": "X", "modified": "not a date"},
        {"id": "X", "modified": "2026-01-01T00:00:00"},  # naive: ambiguous
    ],
)
def test_invalid_records_raise(doc):
    with pytest.raises(InvalidAdvisoryError):
        parse_osv(doc)
