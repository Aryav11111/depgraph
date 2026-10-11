"""Checked against reality: our findings must equal `npm audit`'s.

Fixtures (regenerate together; see tests/fixtures/README.md):
* tests/fixtures/npm/<project>/: a real npm-generated lockfile plus the
  `npm audit --json` output for it (GitHub's advisory database, via npm);
* tests/fixtures/osv/audit-subset.zip: every OSV record from the full npm export
  that mentions any package in those lockfiles, including the many that do NOT
  affect the installed versions, so over-matching would fail too.

Both snapshots were taken at the same moment, so the comparison is exact: same
(package, advisory) pairs, same severities. npm audit doesn't report OSV's
malicious-package records, so those are compared separately (none expected).
"""

import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete

from depgraph.advisories.ingest import full_load
from depgraph.advisories.npm_range import satisfies
from depgraph.core.config import Settings
from depgraph.db.models import AdvisoryRecord, AdvisorySyncRun, Scan
from depgraph.db.session import create_sessionmaker
from depgraph.findings.fixes import fix_for_finding
from depgraph.findings.report import list_findings
from tests.conftest import make_client
from tests.helpers import FIXTURES

PROJECTS = {
    # project: (lockfile, expected number of (package, advisory) pairs)
    "vulnerable": ("package-lock.json", 89),
    "realworld": ("package-lock.v3.json", 11),
}


def npm_audit(project: str) -> dict[tuple[str, str], dict]:
    """(package, GHSA id) -> the advisory as npm audit reports it."""
    doc = json.loads((FIXTURES / "npm" / project / "npm-audit.json").read_text(encoding="utf-8"))
    out = {}
    for name, vuln in doc["vulnerabilities"].items():
        for via in vuln["via"]:
            if isinstance(via, dict):  # strings are "vulnerable because of a dependency"
                out[(name, via["url"].rsplit("/", 1)[-1])] = via
    return out


# Module-scoped: load the advisory subset and upload both lockfiles once.
@pytest.fixture(scope="module")
async def scans(engine) -> AsyncIterator[dict[str, dict]]:
    """Load the OSV subset, upload each fixture lockfile through the API."""
    maker = create_sessionmaker(engine)
    await full_load(
        maker,
        httpx.AsyncClient(),
        Settings(app_env="test"),
        archive=FIXTURES / "osv" / "audit-subset.zip",
    )
    out: dict[str, dict] = {}
    async with make_client(Settings(app_env="test", log_json=False)) as client:
        for project, (lockfile, _) in PROJECTS.items():
            body = (FIXTURES / "npm" / project / lockfile).read_bytes()
            scan = (await client.post("/scans", content=body)).json()
            findings = (await client.get(f"/scans/{scan['id']}/findings")).json()
            out[project] = {"scan": scan, "findings": findings["findings"]}
        yield out
    async with maker.begin() as s:
        ids = [uuid.UUID(v["scan"]["id"]) for v in out.values()]
        await s.execute(delete(Scan).where(Scan.id.in_(ids)))
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))


@pytest.mark.parametrize("project", PROJECTS)
async def test_findings_equal_npm_audit(scans, project):
    ours = {
        (f["package"], f["advisory"]["id"]): f
        for f in scans[project]["findings"]
        if f["advisory"]["kind"] == "vulnerability"
    }
    theirs = npm_audit(project)
    assert set(ours) == set(theirs), {
        "only ours": sorted(set(ours) - set(theirs)),
        "only npm audit": sorted(set(theirs) - set(ours)),
    }
    assert len(ours) == PROJECTS[project][1]
    severity_mismatches = [
        (k, ours[k]["advisory"]["severity"], theirs[k]["severity"])
        for k in ours
        if ours[k]["advisory"]["severity"] != theirs[k]["severity"].upper()
    ]
    assert severity_mismatches == []


@pytest.mark.parametrize("project", PROJECTS)
async def test_no_malicious_packages_in_fixtures(scans, project):
    assert [f for f in scans[project]["findings"] if f["advisory"]["kind"] == "malicious"] == []


@pytest.mark.parametrize("project", PROJECTS)
async def test_installed_and_fixed_versions_agree_with_npm_audit_ranges(engine, scans, project):
    """Cross-check with npm audit's own vulnerable-range strings (parsed by our
    npm_range): the installed version is inside it, our fixed version outside."""
    theirs = npm_audit(project)
    maker = create_sessionmaker(engine)
    async with maker() as s:
        rows = await list_findings(s, uuid.UUID(scans[project]["scan"]["id"]))
        checked = 0
        for row in rows:
            via = theirs.get((row.package, row.advisory_id))
            if via is None:
                continue
            assert satisfies(row.version, via["range"]) is True, (row.package, row.version, via)
            # Offline (no registry client): fixes come from the advisory's own bounds.
            fix = await fix_for_finding(s, None, row)
            if fix.fixed_version is not None:
                assert satisfies(fix.fixed_version, via["range"]) is False, (
                    row.package, fix.fixed_version, via["range"],
                )  # fmt: skip
            checked += 1
    assert checked == PROJECTS[project][1]


async def test_well_known_advisories_are_found(scans):
    """Spot checks a human can verify on osv.dev / GitHub."""
    found = {(f["package"], f["version"]) for f in scans["vulnerable"]["findings"]}
    aliases = {a for f in scans["vulnerable"]["findings"] for a in f["advisory"]["aliases"]}
    assert ("lodash", "4.17.20") in found  # CVE-2021-23337 command injection
    assert "CVE-2021-23337" in aliases
    assert ("minimist", "1.2.5") in found  # CVE-2021-44906 prototype pollution
    assert "CVE-2021-44906" in aliases
    assert ("axios", "0.21.1") in found  # CVE-2021-3749 ReDoS
    assert ("jsonwebtoken", "8.5.1") in found  # CVE-2022-23529 et al.
    assert ("semver", "5.7.1") in found  # CVE-2022-25883 ReDoS
