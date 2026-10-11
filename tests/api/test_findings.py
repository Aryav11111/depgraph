"""Findings API end to end: upload -> match -> list/filter -> detail with paths + fix."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete

from depgraph.api.deps import get_npm
from depgraph.core.config import Settings
from depgraph.db.models import AdvisoryRecord, AdvisorySyncRun, Scan
from depgraph.db.session import create_sessionmaker
from depgraph.registry.npm import NpmRegistryClient
from tests.conftest import make_client
from tests.findings.support import load_advisories
from tests.helpers import lock, pkg

APP = lock(
    {
        "node_modules/lodash": pkg("lodash", "4.17.20"),  # HIGH, direct, ^4 admits fix
        "node_modules/mkdirp": pkg("mkdirp", "0.5.5", dependencies={"minimist": "1.2.5"}),
        "node_modules/minimist": pkg("minimist", "1.2.5"),  # CRITICAL, pinned by mkdirp
        "node_modules/tostpro": pkg("tostpro", "100.6.0"),  # malicious
        "node_modules/left-pad": pkg("left-pad", "1.3.0"),  # clean
    },
    root={"dependencies": {"lodash": "^4.17.0", "mkdirp": "^0.5", "tostpro": "*",
                           "left-pad": "^1"}},
)  # fmt: skip

PACKUMENTS = {
    "lodash": {"4.17.20": {}, "4.17.21": {}},
    "minimist": {"1.2.5": {}, "1.2.6": {}},
    "mkdirp": {"0.5.5": {"minimist": "1.2.5"}, "0.5.6": {"minimist": "^1.2.6"}},
    "tostpro": {"100.6.0": {}},
}


def fake_npm() -> NpmRegistryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.lstrip("/")
        versions = PACKUMENTS.get(name)
        if versions is None:
            return httpx.Response(404)
        return httpx.Response(200, json={
            "name": name, "dist-tags": {},
            "versions": {v: {"version": v, "dist": {}, "dependencies": d}
                         for v, d in versions.items()},
        })  # fmt: skip

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NpmRegistryClient(http, None, registry_url="https://npm.test", max_retries=0)


@pytest.fixture
async def api(settings: Settings, engine, tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    maker = create_sessionmaker(engine)
    await load_advisories(maker, tmp_path)
    created: list[str] = []
    async with make_client(settings) as client:
        client._transport.app.dependency_overrides[get_npm] = fake_npm  # type: ignore[attr-defined]
        resp = await client.post("/scans", json=APP)
        assert resp.status_code == 201, resp.text
        created.append(resp.json()["id"])
        client.scan_id = resp.json()["id"]  # type: ignore[attr-defined]
        client.engine = engine  # type: ignore[attr-defined]
        yield client
    async with maker.begin() as s:
        await s.execute(delete(Scan).where(Scan.id.in_([uuid.UUID(i) for i in created])))
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))


def url(api, suffix: str = "") -> str:
    return f"/scans/{api.scan_id}/findings{suffix}"


# ----------------------------------------------------------------------- list


async def test_upload_is_matched_and_listed_most_severe_first(api):
    body = (await api.get(url(api))).json()
    assert body["matched_at"] is not None
    # Most severe first; within CRITICAL, higher CVSS first and no score last
    # (the malicious report has no CVSS).
    assert [(f["package"], f["advisory"]["id"], f["advisory"]["severity"])
            for f in body["findings"]] == [
        ("minimist", "GHSA-xvch-5gv4-984h", "CRITICAL"),
        ("tostpro", "MAL-2026-17564", "CRITICAL"),
        ("lodash", "GHSA-35jh-r3h4-6jhm", "HIGH"),
    ]  # fmt: skip
    assert body["counts"] == {"CRITICAL": 2, "HIGH": 1, "MODERATE": 0, "LOW": 0, "UNKNOWN": 0}


async def test_summary_fields(api):
    findings = {f["package"]: f for f in (await api.get(url(api))).json()["findings"]}
    lodash = findings["lodash"]
    assert lodash["direct"] is True
    assert [s["name"] for s in lodash["shortest_path"]] == ["app", "lodash"]
    assert "CVE-2021-23337" in lodash["advisory"]["aliases"]
    assert lodash["advisory"]["url"] == "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm"
    minimist = findings["minimist"]
    assert minimist["direct"] is False
    assert [s["name"] for s in minimist["shortest_path"]] == ["app", "mkdirp", "minimist"]
    assert lodash["dev_only"] is False
    assert "left-pad" not in findings


async def test_dev_only_findings_are_flagged(api):
    dev = lock(
        {"node_modules/lodash": pkg("lodash", "4.17.20", dev=True)},
        root={"devDependencies": {"lodash": "^4.17.0"}},
    )
    scan = (await api.post("/scans", json=dev)).json()
    try:
        (f,) = (await api.get(f"/scans/{scan['id']}/findings")).json()["findings"]
        assert f["dev_only"] is True
        assert f["shortest_path"][1]["dep_type"] == "dev"
    finally:
        async with create_sessionmaker(api.engine).begin() as s:
            await s.execute(delete(Scan).where(Scan.id == uuid.UUID(scan["id"])))


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"severity": "HIGH"}, {"lodash"}),
        ({"severity": ["HIGH", "CRITICAL"]}, {"lodash", "minimist", "tostpro"}),
        ({"min_severity": "CRITICAL"}, {"minimist", "tostpro"}),
        ({"min_severity": "HIGH"}, {"lodash", "minimist", "tostpro"}),
        ({"severity": "LOW"}, set()),
        ({"kind": "malicious"}, {"tostpro"}),
        ({"kind": "vulnerability", "min_severity": "CRITICAL"}, {"minimist"}),
    ],
)
async def test_filters(api, params, expected):
    body = (await api.get(url(api), params=params)).json()
    assert {f["package"] for f in body["findings"]} == expected
    assert sum(body["counts"].values()) == 3  # counts are unfiltered


async def test_invalid_filter_is_422(api):
    assert (await api.get(url(api), params={"severity": "SEVERE"})).status_code == 422


async def test_unknown_scan_is_404(api):
    assert (await api.get(f"/scans/{uuid.uuid4()}/findings")).status_code == 404


# --------------------------------------------------------------------- detail


async def _detail(api, package: str, **params) -> dict:
    findings = (await api.get(url(api))).json()["findings"]
    fid = next(f["id"] for f in findings if f["package"] == package)
    resp = await api.get(url(api, f"/{fid}"), params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_detail_direct_dependency_within_range(api):
    d = await _detail(api, "lodash")
    assert d["advisory"]["details"].startswith("`lodash` versions prior to 4.17.21")
    assert any("nvd.nist.gov" in r["url"] for r in d["advisory"]["references"])
    # The root has no stored version (known limitation, NOTES.md stage 1 step 6).
    assert [[(st["name"], st["version"], st["dep_type"]) for st in p] for p in d["paths"]] == [
        [("app", None, None), ("lodash", "4.17.20", "prod")]
    ]
    fix = d["fix"]
    assert (fix["fixed_version"], fix["source"], fix["action"], fix["direct"]) == (
        "4.17.21", "registry", "update_lockfile", True,
    )  # fmt: skip


async def test_detail_transitive_blocked_by_parent(api):
    fix = (await _detail(api, "minimist"))["fix"]
    assert fix["action"] == "upgrade_parent_or_override"
    assert fix["overrides"] == {"minimist": "1.2.6"}
    (mkdirp,) = fix["parents"]
    assert (mkdirp["name"], mkdirp["allows_fix"], mkdirp["upgrade_to"]) == (
        "mkdirp",
        False,
        "0.5.6",
    )
    assert "Upgrade mkdirp to 0.5.6" in fix["message"]


async def test_detail_malicious(api):
    d = await _detail(api, "tostpro")
    assert d["advisory"]["kind"] == "malicious"
    assert d["fix"]["action"] == "remove"


async def test_detail_path_caps(api):
    d = await _detail(api, "minimist", max_paths=1)
    assert len(d["paths"]) == 1


async def test_detail_404s(api):
    assert (await api.get(url(api, "/999999999"))).status_code == 404
    other = f"/scans/{uuid.uuid4()}/findings/1"
    assert (await api.get(other)).status_code == 404


async def test_detail_survives_registry_outage(api):
    def down() -> NpmRegistryClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        return NpmRegistryClient(http, None, registry_url="https://npm.test", max_retries=0)

    api._transport.app.dependency_overrides[get_npm] = down
    fix = (await _detail(api, "lodash"))["fix"]
    assert (fix["fixed_version"], fix["source"]) == ("4.17.21", "advisory")


async def test_openapi_documents_findings(api):
    paths = (await api.get("/openapi.json")).json()["paths"]
    assert {"/scans/{scan_id}/findings", "/scans/{scan_id}/findings/{finding_id}"} <= set(paths)
