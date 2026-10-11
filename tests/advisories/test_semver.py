"""SemVer 2.0 precedence: the foundation the range matcher stands on."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from depgraph.advisories.semver import InvalidVersionError, Version

V = Version.parse


def test_spec_precedence_chain():
    # The exact example from semver.org s11.4.
    chain = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
        "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
    ]  # fmt: skip
    versions = [V(s) for s in chain]
    for lo, hi in itertools.pairwise(versions):
        assert lo < hi, f"{lo} < {hi}"
    assert sorted(reversed(versions)) == versions


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("1.0.0", "2.0.0"),
        ("2.0.0", "2.1.0"),
        ("2.1.0", "2.1.1"),
        ("1.9.0", "1.10.0"),  # numeric, not lexical
        ("1.0.0-beta.2", "1.0.0-beta.11"),  # numeric identifiers numerically
        ("1.0.0-1", "1.0.0-alpha"),  # numeric identifiers below alphanumeric
        ("1.0.0-Z", "1.0.0-a"),  # ASCII order: uppercase before lowercase
        ("1.0.0-alpha", "1.0.0-alpha.0"),  # shorter prefix sorts first
        ("1.0.0-rc.1", "1.0.0"),  # any prerelease below its release
        ("0.9.9", "1.0.0-alpha"),  # ...but above the previous release
        ("1.0.0-x-y", "1.0.0-x-z"),  # hyphens inside identifiers are text
        ("1.0.0", "18446744073709551616.0.0"),  # beyond 64-bit: Python ints
    ],
)
def test_ordering(lower, higher):
    assert V(lower) < V(higher)
    assert V(higher) > V(lower)
    assert V(lower) != V(higher)


def test_build_metadata_is_ignored_for_precedence_and_equality():
    assert V("1.0.0+build.1") == V("1.0.0+build.2") == V("1.0.0")
    assert hash(V("1.0.0+x")) == hash(V("1.0.0"))
    assert not V("1.0.0+zzz") > V("1.0.0")
    assert V("1.0.0-rc.1+build") < V("1.0.0")


@pytest.mark.parametrize("text", ["v1.2.3", "=1.2.3", " 1.2.3 ", "v 1.2.3"])
def test_npm_tolerated_prefixes(text):
    assert V(text) == V("1.2.3")


@pytest.mark.parametrize(
    "text",
    [
        "", "1", "1.2", "1.2.3.4", "01.2.3", "1.02.3", "1.2.03", "1.2.3-",
        "1.2.3-01", "1.2.3-alpha..1", "1.2.3+", "1.2.3+a..b", "a.b.c", "-1.2.3",
        "1.2.3 4", "latest", "^1.2.3", ">=1.0.0",
    ],
)  # fmt: skip
def test_strict_parse_rejects_invalid(text):
    with pytest.raises(InvalidVersionError):
        V(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("13.0", "13.0.0"), ("1.3", "1.3.0"), ("7", "7.0.0"), ("2026.04.1", "2026.4.1")],
)
def test_bound_parsing_coerces_numeric_shorthand_seen_in_real_data(text, expected):
    assert Version.parse_bound(text) == V(expected)


@pytest.mark.parametrize("text", ["13.x", "1.3-beta", "latest", ""])
def test_bound_parsing_still_rejects_non_numeric_garbage(text):
    with pytest.raises(InvalidVersionError):
        Version.parse_bound(text)


def test_str_round_trips():
    for s in ["1.2.3", "1.2.3-alpha.1", "1.2.3+build.5", "1.0.0-rc.1+sha.abc"]:
        assert str(V(s)) == s


def test_comparison_with_non_version_is_not_supported():
    with pytest.raises(TypeError):
        _ = V("1.0.0") < "1.0.1"  # type: ignore[operator]


# ------------------------------------------------------------ properties

# Identifiers chosen to hit every comparison rule: numeric vs numeric, numeric vs
# text, ASCII case order, hyphens, and prefixes ("a" vs "a-b").
ident = st.one_of(
    st.integers(min_value=0, max_value=50),
    st.sampled_from(["a", "a-b", "alpha", "beta", "rc", "Z", "x1", "-"]),
)
versions = st.builds(
    Version,
    st.integers(0, 5),
    st.integers(0, 5),
    st.integers(0, 5),
    st.lists(ident, max_size=3).map(tuple),
)


@given(versions)
def test_round_trip_through_text(v):
    assert V(str(v)) == v


@given(versions, versions)
def test_total_order_is_consistent(a, b):
    assert sum([a < b, a == b, a > b]) == 1  # exactly one holds
    assert (a <= b) == (not a > b)


@given(versions, versions, versions)
def test_transitivity(a, b, c):
    if a <= b and b <= c:
        assert a <= c


@given(versions)
def test_prerelease_sorts_below_its_release(v):
    release = Version(v.major, v.minor, v.patch)
    if v.is_prerelease:
        assert v < release
    else:
        assert v == release
