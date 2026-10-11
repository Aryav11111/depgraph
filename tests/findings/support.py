"""Shared data and helpers for findings tests (fixtures live in conftest.py)."""

import uuid

import httpx
from sqlalchemy import text

from depgraph.advisories.ingest import full_load
from depgraph.core.config import Settings
from depgraph.db.models import Scan
from depgraph.graph.builder import run_build
from depgraph.lockfile.npm import parse_package_lock
from tests.helpers import lock, make_zip, osv_records, pkg

SCAN_MARKER = "m" * 64  # lockfile_sha256 of scans created here, for cleanup

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


async def load_advisories(maker, tmp_path, docs=None) -> int:
    archive = make_zip(tmp_path / f"{uuid.uuid4().hex}.zip", docs or osv_records())
    return await full_load(maker, httpx.AsyncClient(), Settings(app_env="test"), archive=archive)


async def make_scan(maker, doc=VULNERABLE) -> uuid.UUID:
    async with maker.begin() as s:
        scan = Scan(lockfile_sha256=SCAN_MARKER)
        s.add(scan)
        await s.flush()
        scan_id = scan.id
    await run_build(maker, scan_id, parse_package_lock(doc))
    return scan_id


async def findings_of(maker, scan_id) -> set[tuple[str, str, str]]:
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
