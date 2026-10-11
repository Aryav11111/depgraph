"""Fix suggestions: pure decision logic, then the DB + registry glue."""

import httpx
import pytest

from depgraph.advisories.semver import Version
from depgraph.findings.fixes import (
    Action,
    Parent,
    ParentRelease,
    fix_for_finding,
    minimum_parent_upgrade,
    suggest_fix,
)
from depgraph.findings.matching import match_scan
from depgraph.findings.report import list_findings
from depgraph.registry.npm import NpmRegistryClient
from tests.findings.support import load_advisories, make_scan
from tests.helpers import lock, pkg


def rng(*events: dict) -> list[dict]:
    return [{"ranges": [{"type": "SEMVER", "events": list(events)}], "versions": []}]


LODASH = rng({"introduced": "0"}, {"fixed": "4.17.21"})
ROOT = dict(name="app", version="1.0.0", is_root=True, dep_type="prod")


def parent(name, version, declared, *, root=False, dep_type="prod"):
    return Parent(name=name, version=version, is_root=root, dep_type=dep_type,
                  declared_range=declared)  # fmt: skip


def published(*versions: str, deprecated=()) -> dict[str, str | None]:
    return {v: ("deprecated" if v in deprecated else None) for v in versions}


def fix(installed="4.17.20", entries=LODASH, versions=None, parents=(), kind="vulnerability",
        package="lodash"):  # fmt: skip
    return suggest_fix(
        package=package, installed=installed, kind=kind, entries=entries,
        published=versions, parents=list(parents),
    )  # fmt: skip


REGISTRY = published("4.17.19", "4.17.20", "4.17.21", "4.17.22-beta.1", "4.17.23", "5.0.0")


# ------------------------------------------------------------ minimum version


def test_minimum_published_unaffected_version():
    f = fix(versions=REGISTRY, parents=[parent("app", "1.0.0", "^4.17.0", root=True)])
    assert (f.fixed_version, f.source, f.breaking) == ("4.17.21", "registry", False)


def test_prereleases_are_skipped_for_stable_installs():
    # Fixed in a beta: 4.17.21 is still affected (it sorts below the beta), the
    # beta itself is skipped for a stable install, so the next stable release wins.
    f = fix(entries=rng({"introduced": "0"}, {"fixed": "4.17.22-beta.1"}), versions=REGISTRY)
    assert f.fixed_version == "4.17.23"
    beta_user = fix(installed="4.17.22-alpha.1",
                    entries=rng({"introduced": "0"}, {"fixed": "4.17.22-beta.1"}),
                    versions=REGISTRY)  # fmt: skip
    assert beta_user.fixed_version == "4.17.22-beta.1"  # already on prereleases: allowed


def test_deprecated_releases_are_avoided_when_possible():
    f = fix(versions=published("4.17.20", "4.17.21", "4.17.23", deprecated={"4.17.21"}))
    assert f.fixed_version == "4.17.23"
    only = fix(versions=published("4.17.20", "4.17.21", deprecated={"4.17.21"}))
    assert only.fixed_version == "4.17.21"  # still better than no fix


def test_fixed_version_that_was_never_published():
    # Advisory says fixed in 1.2.6, but the registry jumps from 1.2.5 to 1.2.8.
    f = fix(installed="1.2.5", package="minimist",
            entries=rng({"introduced": "0"}, {"fixed": "1.2.6"}),
            versions=published("1.2.5", "1.2.8"))  # fmt: skip
    assert f.fixed_version == "1.2.8"


def test_multi_interval_picks_the_first_gap():
    entries = rng({"introduced": "1.0.0"}, {"fixed": "1.5.0"},
                  {"introduced": "2.0.0"}, {"fixed": "2.5.0"})  # fmt: skip
    f = fix(installed="1.2.0", entries=entries, versions=published("1.2.0", "1.5.0", "2.1.0"))
    assert f.fixed_version == "1.5.0"


def test_last_affected_bound():
    entries = rng({"introduced": "0"}, {"last_affected": "1.2.0"})
    f = fix(installed="1.2.0", entries=entries, versions=published("1.2.0", "1.2.1", "1.3.0"))
    assert f.fixed_version == "1.2.1"


def test_unevaluable_range_is_never_recommended_as_safe():
    entries = rng({"introduced": "0"}, {"fixed": "not-a-version"})
    assert fix(entries=entries, versions=REGISTRY).action is Action.NO_FIX


# ------------------------------------------------------------- registry down


def test_registry_unavailable_falls_back_to_advisory_bounds():
    f = fix(versions=None)
    assert (f.fixed_version, f.source) == ("4.17.21", "advisory")


def test_registry_unavailable_and_no_fixed_event():
    f = fix(entries=rng({"introduced": "0"}, {"last_affected": "4.17.20"}), versions=None)
    assert f.action is Action.NO_FIX and f.fixed_version is None


# ------------------------------------------------------------------ actions


def test_direct_dependency_whose_range_already_allows_the_fix():
    f = fix(versions=REGISTRY, parents=[parent("app", "1.0.0", "^4.17.0", root=True)])
    assert f.direct and f.action is Action.UPDATE_LOCKFILE
    assert "npm update lodash" in f.message


def test_direct_dependency_pinned_below_the_fix():
    f = fix(versions=REGISTRY, parents=[parent("app", "1.0.0", "4.17.20", root=True)])
    assert f.action is Action.BUMP_DIRECT
    assert '"^4.17.21"' in f.message


def test_transitive_within_range_is_a_lockfile_update():
    f = fix(installed="1.2.5", package="minimist",
            entries=rng({"introduced": "1.0.0"}, {"fixed": "1.2.6"}),
            versions=published("1.2.5", "1.2.6"),
            parents=[parent("mkdirp", "0.5.5", "^1.2.5")])  # fmt: skip
    assert (f.direct, f.action) == (False, Action.UPDATE_LOCKFILE)
    assert f.parents[0].allows_fix is True


def test_transitive_blocked_by_parent_needs_upgrade_or_override():
    f = fix(installed="1.2.5", package="minimist",
            entries=rng({"introduced": "1.0.0"}, {"fixed": "1.2.6"}),
            versions=published("1.2.5", "1.2.6"),
            parents=[parent("mkdirp", "0.5.5", "1.2.5"),
                     parent("other", "2.0.0", "^1.2.0")])  # fmt: skip
    assert f.action is Action.UPGRADE_PARENT_OR_OVERRIDE
    assert f.overrides == {"minimist": "1.2.6"}
    assert [(p.name, p.allows_fix) for p in f.parents] == [("mkdirp", False), ("other", True)]
    assert "mkdirp@0.5.5 (1.2.5)" in f.message and "other" not in f.message


def test_direct_and_blocked_transitively_still_needs_the_parent():
    # Bumping package.json alone would leave mkdirp's copy behind.
    f = fix(versions=REGISTRY, parents=[parent("app", "1.0.0", "^4.17.0", root=True),
                                        parent("legacy", "1.0.0", "4.17.20")])  # fmt: skip
    assert f.direct and f.action is Action.UPGRADE_PARENT_OR_OVERRIDE


def test_unknown_parent_spec_is_not_claimed_to_allow_the_fix():
    f = fix(versions=REGISTRY, parents=[parent("thing", "1.0.0", "github:foo/lodash#v4")])
    assert f.parents[0].allows_fix is None
    assert f.action is Action.UPGRADE_PARENT_OR_OVERRIDE
    assert "may not" in f.message


@pytest.mark.parametrize(
    ("installed", "fixed_in", "breaking"),
    [("4.17.20", "4.17.21", False), ("1.2.5", "2.0.0", True), ("0.2.3", "0.3.0", True),
     ("0.2.3", "0.2.4", False), ("0.0.3", "0.0.4", True)],
)  # fmt: skip
def test_breaking_follows_caret_semantics(installed, fixed_in, breaking):
    f = fix(installed=installed, entries=rng({"introduced": "0"}, {"fixed": fixed_in}),
            versions=published(installed, fixed_in))  # fmt: skip
    assert f.breaking is breaking


def test_no_published_fix():
    f = fix(entries=rng({"introduced": "0"}), versions=REGISTRY)
    assert (f.action, f.fixed_version, f.source) == (Action.NO_FIX, None, None)


def test_malicious_package_is_removed_not_upgraded():
    f = fix(kind="malicious", entries=[{"ranges": [], "versions": ["100.6.0"]}],
            installed="100.6.0", package="tostpro", versions=published("100.6.0"))  # fmt: skip
    assert f.action is Action.REMOVE
    assert "rotate" in f.message


# ----------------------------------------------------------------- DB glue

APP = lock(
    {
        "node_modules/lodash": pkg("lodash", "4.17.20"),
        "node_modules/mkdirp": pkg("mkdirp", "0.5.5", dependencies={"minimist": "1.2.5"}),
        "node_modules/minimist": pkg("minimist", "1.2.5"),
    },
    root={"dependencies": {"lodash": "^4.17.0", "mkdirp": "^0.5"}},
)


def registry(down: bool = False) -> NpmRegistryClient:
    # name -> {version: dependencies}
    versions = {
        "lodash": {"4.17.20": {}, "4.17.21": {}},
        "minimist": {"1.2.5": {}, "1.2.6": {}, "1.2.8": {}},
        "mkdirp": {"0.5.5": {"minimist": "1.2.5"}, "0.5.6": {"minimist": "^1.2.6"}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if down:
            return httpx.Response(503)
        name = request.url.path.lstrip("/")
        doc = {
            "name": name,
            "dist-tags": {},
            "versions": {
                v: {"version": v, "dist": {}, "dependencies": deps}
                for v, deps in versions[name].items()
            },
        }
        return httpx.Response(200, json=doc)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NpmRegistryClient(http, None, registry_url="https://npm.test", max_retries=0)


@pytest.fixture
async def scanned(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker, APP)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    return scan_id


async def test_suggestions_from_scan_graph_and_registry(maker, scanned):
    async with maker() as s:
        rows = {r.package: r for r in await list_findings(s, scanned)}
        lodash = await fix_for_finding(s, registry(), rows["lodash"])
        minimist = await fix_for_finding(s, registry(), rows["minimist"])
    assert (lodash.direct, lodash.action, lodash.fixed_version) == (
        True, Action.UPDATE_LOCKFILE, "4.17.21",
    )  # fmt: skip
    # mkdirp pins minimist to exactly 1.2.5: the parent blocks the fix
    assert (minimist.direct, minimist.action, minimist.fixed_version) == (
        False, Action.UPGRADE_PARENT_OR_OVERRIDE, "1.2.6",
    )  # fmt: skip
    assert [(p.name, p.version, p.declared_range, p.upgrade_to) for p in minimist.parents] == [
        ("mkdirp", "0.5.5", "1.2.5", "0.5.6")
    ]


async def test_registry_outage_degrades_to_advisory_bounds(maker, scanned):
    async with maker() as s:
        lodash = next(r for r in await list_findings(s, scanned) if r.package == "lodash")
        f = await fix_for_finding(s, registry(down=True), lodash)
    assert (f.fixed_version, f.source) == ("4.17.21", "advisory")


# --------------------------------------------------------- parent upgrades


def releases(**specs: str | None) -> dict[str, ParentRelease]:
    """version (underscored) -> minimist spec; None = dependency dropped."""
    out = {}
    for key, spec in specs.items():
        version, deprecated = key.replace("_", ".").removesuffix(".dep"), key.endswith("_dep")
        out[version] = ParentRelease(deprecated, {} if spec is None else {"minimist": spec})
    return out


FIX = Version.parse("1.2.6")


def test_lowest_parent_release_whose_range_admits_the_fix():
    r = releases(**{"0_5_5": "1.2.5", "0_5_6": "^1.2.6", "0_5_7": "^1.2.6", "1_0_0": "^1.2.6"})
    assert minimum_parent_upgrade("0.5.5", r, "minimist", FIX) == "0.5.6"


def test_parent_release_that_dropped_the_dependency_also_counts():
    r = releases(**{"0_5_6": "1.2.5", "0_6_0": None})
    assert minimum_parent_upgrade("0.5.5", r, "minimist", FIX) == "0.6.0"


def test_deprecated_and_prerelease_parent_releases_are_skipped():
    r = {
        "0.5.6": ParentRelease(True, {"minimist": "^1.2.6"}),
        "0.5.7-beta.1": ParentRelease(False, {"minimist": "^1.2.6"}),
        "0.5.8": ParentRelease(False, {"minimist": "^1.2.6"}),
    }
    assert minimum_parent_upgrade("0.5.5", r, "minimist", FIX) == "0.5.8"


def test_no_parent_release_helps():
    r = releases(**{"0_5_6": "1.2.5", "0_5_7": "<1.2.6"})  # (~1.2.5 would admit 1.2.6)
    assert minimum_parent_upgrade("0.5.5", r, "minimist", FIX) is None
    assert minimum_parent_upgrade(None, r, "minimist", FIX) is None
    assert minimum_parent_upgrade("0.5.5", None, "minimist", FIX) is None


def test_suggestion_names_the_parent_upgrade():
    f = suggest_fix(
        package="minimist", installed="1.2.5", kind="vulnerability",
        entries=rng({"introduced": "1.0.0"}, {"fixed": "1.2.6"}),
        published=published("1.2.5", "1.2.6"),
        parents=[parent("mkdirp", "0.5.5", "1.2.5")],
        parent_releases={"mkdirp": releases(**{"0_5_6": "^1.2.6"})},
    )  # fmt: skip
    assert f.parents[0].upgrade_to == "0.5.6"
    assert "Upgrade mkdirp to 0.5.6" in f.message
