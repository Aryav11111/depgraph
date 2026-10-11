"""Range matcher: hand-picked edge cases, real OSV records, and a property test
against an independent interval oracle."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from depgraph.advisories.matcher import InvalidRangeError, affects_entry, affects_range
from depgraph.advisories.semver import Version
from tests.helpers import FIXTURES


def hit(version: str, *events: dict, versions=()) -> bool:
    return affects_entry(version, [{"type": "SEMVER", "events": list(events)}], versions)


# ------------------------------------------------------------ single interval


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.0.0", True),
        ("4.17.20", True),
        ("4.17.21", False),  # fixed is exclusive
        ("4.17.22", False),
        ("5.0.0", False),
    ],
)
def test_introduced_zero_fixed(version, expected):
    assert hit(version, {"introduced": "0"}, {"fixed": "4.17.21"}) is expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.1.9", False), ("1.2.0", True), ("1.5.0", True), ("2.0.0", False)],
)
def test_introduced_is_inclusive(version, expected):
    assert hit(version, {"introduced": "1.2.0"}, {"fixed": "2.0.0"}) is expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.2.0", True), ("1.2.1", False), ("1.2.0+build", True), ("1.2.1-rc.1", False)],
)
def test_last_affected_is_inclusive(version, expected):
    # 1.2.1-rc.1 > 1.2.0, so it is past last_affected.
    assert hit(version, {"introduced": "0"}, {"last_affected": "1.2.0"}) is expected


def test_introduced_without_upper_bound_affects_everything_after():
    assert hit("999.0.0", {"introduced": "3.0.0"})
    assert not hit("2.9.9", {"introduced": "3.0.0"})


def test_limit_caps_the_range():
    assert hit("1.5.0", {"introduced": "1.0.0"}, {"limit": "2.0.0"})
    assert not hit("2.0.0", {"introduced": "1.0.0"}, {"limit": "2.0.0"})


# ---------------------------------------------------------------- prereleases


def test_prerelease_of_the_fixed_version_is_still_affected():
    # Precedence: 4.17.21-beta < 4.17.21. (npm's satisfies() would say otherwise;
    # see semver.py for why precedence is the right question here.)
    assert hit("4.17.21-beta.1", {"introduced": "0"}, {"fixed": "4.17.21"})


def test_prerelease_bounds_from_real_data():
    events = ({"introduced": "1.2.0-rc.0"}, {"fixed": "1.5.15"})
    assert hit("1.2.0-rc.0", *events)
    assert hit("1.2.0-rc.1", *events)
    assert not hit("1.2.0-beta.9", *events)  # "beta" < "rc" lexically
    assert hit("1.2.0", *events)
    assert hit("1.5.15-rc.0", *events)
    assert not hit("1.5.15", *events)


def test_fixed_in_a_prerelease():
    events = ({"introduced": "0"}, {"fixed": "2.0.0-rc.2"})
    assert hit("2.0.0-rc.1", *events)
    assert not hit("2.0.0-rc.2", *events)
    assert not hit("2.0.0", *events)


# ----------------------------------------------------------- multi-interval


MULTI = ({"introduced": "1.0.0"}, {"fixed": "1.5.0"}, {"introduced": "2.0.0"}, {"fixed": "2.5.0"})


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.9.0", False), ("1.0.0", True), ("1.4.9", True), ("1.5.0", False),
     ("1.9.9", False), ("2.0.0", True), ("2.4.0", True), ("2.5.0", False), ("3.0.0", False)],
)  # fmt: skip
def test_several_intervals_in_one_range(version, expected):
    assert hit(version, *MULTI) is expected


def test_events_need_not_be_sorted():
    shuffled = (MULTI[3], MULTI[0], MULTI[2], MULTI[1])
    for v in ["1.2.0", "1.7.0", "2.2.0", "2.7.0"]:
        assert hit(v, *shuffled) == hit(v, *MULTI)


def test_same_version_introduced_and_fixed_is_empty():
    assert not hit("1.0.0", {"introduced": "1.0.0"}, {"fixed": "1.0.0"})


def test_same_version_introduced_and_last_affected_is_exactly_that_version():
    events = ({"introduced": "1.0.0"}, {"last_affected": "1.0.0"})
    assert hit("1.0.0", *events)
    assert not hit("1.0.1", *events) and not hit("0.9.9", *events)


def test_multiple_ranges_in_one_entry_are_ored():
    ranges = [
        {"type": "SEMVER", "events": [{"introduced": "1.0.0"}, {"fixed": "1.1.0"}]},
        {"type": "SEMVER", "events": [{"introduced": "3.0.0"}, {"fixed": "3.1.0"}]},
    ]
    assert affects_entry("1.0.5", ranges) and affects_entry("3.0.5", ranges)
    assert not affects_entry("2.0.0", ranges)


# --------------------------------------------------- versions list, types


def test_explicit_versions_list():
    assert affects_entry("3.5.3", [], ["3.5.3"])
    assert not affects_entry("3.5.4", [], ["3.5.3"])
    assert affects_entry("3.5.3+build", [], ["3.5.3"])  # build metadata ignored


def test_versions_list_or_ranges():
    ranges = [{"type": "SEMVER", "events": [{"introduced": "2.0.0"}]}]
    assert affects_entry("1.0.0", ranges, ["1.0.0"])
    assert affects_entry("2.1.0", ranges, ["1.0.0"])
    assert not affects_entry("1.5.0", ranges, ["1.0.0"])


def test_ecosystem_ranges_evaluated_git_ranges_ignored():
    events = [{"introduced": "0"}, {"fixed": "2.0.0"}]
    assert affects_entry("1.0.0", [{"type": "ECOSYSTEM", "events": events}])
    assert not affects_entry("1.0.0", [{"type": "GIT", "events": [{"introduced": "0"}]}])


def test_ecosystem_shorthand_bounds_from_real_data():
    # GHSA-3h52-269p-cp9r style: fixed "13.0" means 13.0.0
    ranges = [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "13.0"}]}]
    assert affects_entry("12.9.9", ranges)
    assert not affects_entry("13.0.0", ranges)


def test_unknown_event_types_are_ignored():
    assert hit("1.0.0", {"introduced": "0"}, {"some_future_event": "0.5.0"})


def test_unparseable_bound_raises_instead_of_guessing():
    with pytest.raises(InvalidRangeError):
        hit("1.0.0", {"introduced": "0"}, {"fixed": "not-a-version"})


def test_empty_entry_affects_nothing():
    assert not affects_entry("1.0.0", [], [])
    assert not hit("1.0.0")


# ------------------------------------------------------------ real records


def _entry(adv_id: str, package: str):
    doc = json.loads((FIXTURES / "osv" / f"{adv_id}.json").read_text(encoding="utf-8"))
    return [
        a for a in doc["affected"]
        if a["package"]["ecosystem"] == "npm" and a["package"]["name"] == package
    ]  # fmt: skip


def _affected_by(adv_id: str, package: str, version: str) -> bool:
    return any(
        affects_entry(version, e.get("ranges", []), e.get("versions", []))
        for e in _entry(adv_id, package)
    )


@pytest.mark.parametrize(
    ("adv_id", "package", "version", "expected"),
    [
        # lodash command injection: fixed in 4.17.21
        ("GHSA-35jh-r3h4-6jhm", "lodash", "4.17.20", True),
        ("GHSA-35jh-r3h4-6jhm", "lodash", "4.17.21", False),
        ("GHSA-35jh-r3h4-6jhm", "lodash", "3.10.1", True),
        # minimist prototype pollution
        ("GHSA-xvch-5gv4-984h", "minimist", "1.2.5", True),
        ("GHSA-xvch-5gv4-984h", "minimist", "1.2.6", False),
        ("GHSA-xvch-5gv4-984h", "minimist", "0.2.3", True),
        ("GHSA-xvch-5gv4-984h", "minimist", "0.2.4", False),
        # last_affected 1.2.0
        ("GHSA-2275-rpf5-xv8h", "is-http2", "1.2.0", True),
        ("GHSA-2275-rpf5-xv8h", "is-http2", "1.2.1", False),
        # same package across four versions-only entries: OR across them
        ("GHSA-27f5-xjrr-q9ff", "@opensearch-project/opensearch", "3.7.0", True),
        ("GHSA-27f5-xjrr-q9ff", "@opensearch-project/opensearch", "3.6.0", False),
        # malicious package: the listed versions
        ("MAL-2026-17564", "tostpro", "100.6.0", True),
    ],
)
def test_real_advisories(adv_id, package, version, expected):
    assert _affected_by(adv_id, package, version) is expected


# ------------------------------------------------------------- properties


def _oracle(version: Version, introduced: list[Version], fixed: list[Version]) -> bool:
    """Independent model: build explicit [start, end) intervals, then test membership.

    Starts open an interval if none is open; ends close it. This is the
    interval reading of OSV's spec, written without the event-walk.
    """
    points = sorted([(v, 0) for v in introduced] + [(v, 1) for v in fixed])
    intervals, start = [], None
    for v, is_end in points:
        if not is_end and start is None:
            start = v
        elif is_end and start is not None:
            intervals.append((start, v))
            start = None
    if start is not None:
        intervals.append((start, None))
    return any(lo <= version and (hi is None or version < hi) for lo, hi in intervals)


small_versions = st.builds(
    Version,
    st.integers(0, 3),
    st.integers(0, 3),
    st.integers(0, 3),
    st.one_of(st.just(()), st.tuples(st.sampled_from(["alpha", "beta", "rc"]), st.integers(0, 2))),
)


@given(small_versions, st.lists(small_versions, max_size=3), st.lists(small_versions, max_size=3))
def test_matches_interval_oracle(version, introduced, fixed):
    events = [{"introduced": str(v)} for v in introduced] + [{"fixed": str(v)} for v in fixed]
    assert affects_range(version, events) == _oracle(version, introduced, fixed)


@given(small_versions, small_versions, small_versions)
def test_single_interval_is_half_open(v, a, b):
    lo, hi = sorted([a, b])
    expected = lo <= v < hi
    assert affects_range(v, [{"introduced": str(lo)}, {"fixed": str(hi)}]) == expected
