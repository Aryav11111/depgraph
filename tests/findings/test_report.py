"""Findings read side: ordering, filters, and paths to each vulnerable package."""

import pytest

from depgraph.findings.matching import match_scan
from depgraph.findings.report import (
    finding_paths,
    get_finding,
    list_findings,
    shortest_paths,
)
from tests.findings.support import load_advisories, make_scan
from tests.helpers import lock, pkg

CAPS = {"max_depth": 64, "max_expansions": 200_000}

# minimist@1.2.5 is reachable two ways: root -> mkdirp -> minimist, and
# root -> optimist -> mkdirp -> minimist (a longer path through the same node).
DIAMOND = lock(
    {
        "node_modules/mkdirp": pkg("mkdirp", "0.5.5", dependencies={"minimist": "1.2.5"}),
        "node_modules/optimist": pkg("optimist", "0.6.1", dependencies={"mkdirp": "^0.5"}),
        "node_modules/minimist": pkg("minimist", "1.2.5"),
        "node_modules/lodash": pkg("lodash", "4.17.20", dev=True),
    },
    root={"dependencies": {"mkdirp": "^0.5", "optimist": "^0.6"},
          "devDependencies": {"lodash": "^4"}},
)  # fmt: skip


@pytest.fixture
async def scanned(maker, tmp_path):
    await load_advisories(maker, tmp_path)
    scan_id = await make_scan(maker, DIAMOND)
    async with maker.begin() as s:
        await match_scan(s, scan_id)
    return scan_id


async def test_list_is_most_severe_first_with_details(maker, scanned):
    async with maker() as s:
        rows = await list_findings(s, scanned)
    assert [(r.package, r.version, r.advisory_id, r.severity) for r in rows] == [
        ("minimist", "1.2.5", "GHSA-xvch-5gv4-984h", "CRITICAL"),
        ("lodash", "4.17.20", "GHSA-35jh-r3h4-6jhm", "HIGH"),
    ]
    lodash = rows[1]
    assert "CVE-2021-23337" in lodash.aliases
    assert lodash.kind == "vulnerability" and lodash.first_detected is not None


async def test_filters(maker, scanned):
    async with maker() as s:
        assert [r.package for r in await list_findings(s, scanned, severities={"HIGH"})] == [
            "lodash"
        ]
        assert await list_findings(s, scanned, severities={"LOW"}) == []
        assert await list_findings(s, scanned, kinds={"malicious"}) == []


async def test_all_paths_to_a_finding_shortest_first(maker, scanned):
    async with maker() as s:
        minimist = next(r for r in await list_findings(s, scanned) if r.package == "minimist")
        found = await finding_paths(s, minimist, max_paths=10, **CAPS)
    named = [[(st.name, st.dep_type) for st in p] for p in found.paths]
    assert named == [
        [("app", None), ("mkdirp", "prod"), ("minimist", "prod")],
        [("app", None), ("optimist", "prod"), ("mkdirp", "prod"), ("minimist", "prod")],
    ]
    assert not found.truncated


async def test_path_cap(maker, scanned):
    async with maker() as s:
        minimist = next(r for r in await list_findings(s, scanned) if r.package == "minimist")
        found = await finding_paths(s, minimist, max_paths=1, **CAPS)
    assert len(found.paths) == 1 and found.truncated
    assert [st.name for st in found.paths[0]] == ["app", "mkdirp", "minimist"]


async def test_dev_only_finding_shows_the_dev_edge(maker, scanned):
    async with maker() as s:
        lodash = next(r for r in await list_findings(s, scanned) if r.package == "lodash")
        found = await finding_paths(s, lodash, max_paths=10, **CAPS)
    assert [[(st.name, st.dep_type) for st in p] for p in found.paths] == [
        [("app", None), ("lodash", "dev")]
    ]


async def test_batched_shortest_paths_match_individual_search(maker, scanned):
    async with maker() as s:
        rows = await list_findings(s, scanned)
        batched = await shortest_paths(s, scanned, [r.node_id for r in rows], **CAPS)
        for r in rows:
            single = await finding_paths(s, r, max_paths=1, **CAPS)
            assert batched[r.node_id] == single.paths[0]


async def test_get_finding_is_scoped_to_its_scan(maker, scanned):
    import uuid

    async with maker() as s:
        row = (await list_findings(s, scanned))[0]
        assert (await get_finding(s, scanned, row.id)) == row
        assert await get_finding(s, uuid.uuid4(), row.id) is None
        assert await get_finding(s, scanned, -1) is None
