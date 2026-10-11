"""Graph builder against real Postgres.

`persist_graph` tests run in the rolled-back `db` session. `run_build` owns its own
transactions (that is the point of it), so those tests commit and clean up.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from depgraph.db.models import Package, PackageVersion, Scan, ScanEdge, ScanNode
from depgraph.db.session import create_sessionmaker
from depgraph.graph import builder
from depgraph.graph.builder import _describe, persist_graph, run_build
from depgraph.graph.collapse import collapse
from depgraph.lockfile.npm import parse_package_lock
from tests.helpers import lock, pkg, realworld_lock

REACHABLE_SQL = text(
    """
    WITH RECURSIVE walk(node_id) AS (
        SELECT id FROM scan_nodes WHERE scan_id = :scan AND source = 'root'
        UNION  -- set semantics: revisiting a node adds nothing, so cycles terminate
        SELECT e.child_node_id
        FROM scan_edges e JOIN walk w ON e.parent_node_id = w.node_id
        WHERE e.scan_id = :scan
    )
    SELECT count(*) FROM walk
    """
)


async def _new_scan(session: AsyncSession) -> uuid.UUID:
    scan = Scan(lockfile_sha256="0" * 64)
    session.add(scan)
    await session.flush()
    return scan.id


async def _count(session: AsyncSession, model, **filters) -> int:
    return await session.scalar(select(func.count()).select_from(model).filter_by(**filters))


@pytest.fixture(scope="module")
def realworld():
    return collapse(parse_package_lock(realworld_lock()))


# ------------------------------------------------------------- persist_graph


async def test_persists_every_node_and_edge(db, realworld):
    scan_id = await _new_scan(db)
    assert await persist_graph(db, scan_id, realworld) == []

    assert await _count(db, ScanNode, scan_id=scan_id) == len(realworld.nodes)
    assert await _count(db, ScanEdge, scan_id=scan_id) == len(realworld.edges)
    assert await _count(db, ScanNode, scan_id=scan_id, source="root") == 1


async def test_stored_graph_is_traversable_from_the_root(db, realworld):
    # Early proof that step 6's recursive-CTE queries work on this layout.
    scan_id = await _new_scan(db)
    await persist_graph(db, scan_id, realworld)
    assert await db.scalar(REACHABLE_SQL, {"scan": scan_id}) == len(realworld.nodes)


async def test_nodes_point_at_the_right_catalogue_versions(db, realworld):
    scan_id = await _new_scan(db)
    await persist_graph(db, scan_id, realworld)
    rows = await db.execute(
        select(Package.name, PackageVersion.version, ScanNode.install_paths)
        .join(PackageVersion, PackageVersion.id == ScanNode.version_id)
        .join(Package, Package.id == PackageVersion.package_id)
        .where(ScanNode.scan_id == scan_id)
    )
    stored = {(name, version): tuple(paths) for name, version, paths in rows}
    expected = {
        (n.name, n.version): n.install_paths
        for n in realworld.nodes.values()
        if n.source.value == "registry"
    }
    assert stored == expected


async def test_second_scan_reuses_catalogue_rows(db, realworld):
    await persist_graph(db, await _new_scan(db), realworld)
    before = await _count(db, PackageVersion)
    await persist_graph(db, await _new_scan(db), realworld)
    assert await _count(db, PackageVersion) == before


async def test_integrity_first_seen_wins_and_mismatch_is_reported(db):
    name = f"t-{uuid.uuid4().hex[:8]}"
    first = collapse(parse_package_lock(lock({f"node_modules/{name}": pkg(name, "1.0.0")})))
    assert await persist_graph(db, await _new_scan(db), first) == []  # no integrity yet

    with_hash = lock({f"node_modules/{name}": pkg(name, "1.0.0", integrity="sha512-AAA")})
    await persist_graph(db, await _new_scan(db), collapse(parse_package_lock(with_hash)))
    integrity = select(PackageVersion.integrity).join(Package).where(Package.name == name)
    assert await db.scalar(integrity) == "sha512-AAA"  # filled in, was NULL

    tampered = lock({f"node_modules/{name}": pkg(name, "1.0.0", integrity="sha512-BBB")})
    warnings = await persist_graph(db, await _new_scan(db), collapse(parse_package_lock(tampered)))
    assert warnings == [
        {
            "kind": "integrity_mismatch",
            "package": f"{name}@1.0.0",
            "lockfile": "sha512-BBB",
            "catalogue": "sha512-AAA",
        }
    ]
    assert await db.scalar(integrity) == "sha512-AAA"  # never overwritten


async def test_graph_without_registry_packages(db):
    g = collapse(
        parse_package_lock(
            lock(
                {
                    "node_modules/w": {"resolved": "packages/w", "link": True},
                    "packages/w": {"name": "w", "version": "1.0.0"},
                },
                root={"workspaces": ["packages/*"]},
            )
        )
    )
    scan_id = await _new_scan(db)
    await persist_graph(db, scan_id, g)
    assert await _count(db, ScanNode, scan_id=scan_id) == 2
    assert await _count(db, ScanEdge, scan_id=scan_id) == 1


# ----------------------------------------------------------------- run_build


@pytest.fixture
async def committed(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A real sessionmaker; scans and t-* catalogue rows it creates are removed after."""
    maker = create_sessionmaker(engine)
    yield maker
    async with maker.begin() as s:
        await s.execute(delete(Scan).where(Scan.lockfile_sha256 == "f" * 64))
        await s.execute(
            delete(PackageVersion).where(
                PackageVersion.package_id.in_(select(Package.id).where(Package.name.like("t-%")))
            )
        )
        await s.execute(delete(Package).where(Package.name.like("t-%")))


async def _committed_scan(maker) -> uuid.UUID:
    async with maker.begin() as s:
        scan = Scan(lockfile_sha256="f" * 64)
        s.add(scan)
        await s.flush()
        return scan.id


async def test_run_build_completes_scan_with_counts(committed, realworld):
    scan_id = await _committed_scan(committed)
    await run_build(committed, scan_id, parse_package_lock(realworld_lock()))
    async with committed() as s:
        scan = await s.get(Scan, scan_id)
    assert scan.status == "completed"
    assert (scan.node_count, scan.edge_count) == (len(realworld.nodes), len(realworld.edges))
    assert (scan.lockfile_version, scan.root_name) == (3, "acme-dashboard")
    assert scan.warnings == [] and scan.error is None
    assert scan.completed_at is not None


async def test_run_build_records_warnings(committed):
    scan_id = await _committed_scan(committed)
    await run_build(
        committed, scan_id, parse_package_lock(lock({}, root={"dependencies": {"x": "1"}}))
    )
    async with committed() as s:
        scan = await s.get(Scan, scan_id)
    assert scan.status == "completed"
    assert [w["kind"] for w in scan.warnings] == ["unresolved_dependency"]


async def test_failed_build_leaves_no_partial_graph(committed, monkeypatch):
    real_persist = builder.persist_graph

    async def persist_then_fail(session, scan_id, graph):
        await real_persist(session, scan_id, graph)  # rows written...
        raise RuntimeError("boom")  # ...then the transaction must roll back

    monkeypatch.setattr(builder, "persist_graph", persist_then_fail)
    scan_id = await _committed_scan(committed)
    with pytest.raises(RuntimeError):
        await run_build(committed, scan_id, parse_package_lock(realworld_lock()))

    async with committed() as s:
        scan = await s.get(Scan, scan_id)
        assert (scan.status, scan.error) == ("failed", "RuntimeError: boom")
        assert await _count(s, ScanNode, scan_id=scan_id) == 0


async def test_concurrent_builds_with_overlapping_new_packages(committed):
    # Both scans insert the same brand-new catalogue rows at the same time: the
    # ON CONFLICT upserts and sorted insert order must make this safe.
    names = [f"t-{uuid.uuid4().hex[:8]}" for _ in range(30)]
    doc = lock(
        {f"node_modules/{n}": pkg(n, "1.0.0") for n in names},
        root={"dependencies": dict.fromkeys(names, "*")},
    )
    scans = [await _committed_scan(committed) for _ in range(4)]
    await asyncio.gather(*(run_build(committed, s, parse_package_lock(doc)) for s in scans))

    async with committed() as s:
        statuses = (await s.scalars(select(Scan.status).where(Scan.id.in_(scans)))).all()
        versions = await s.scalar(
            select(func.count())
            .select_from(PackageVersion)
            .join(Package)
            .where(Package.name.in_(names))
        )
    assert statuses == ["completed"] * 4
    assert versions == len(names)  # no duplicates despite the race


def test_error_text_never_contains_sql():
    exc = DBAPIError("INSERT INTO secrets VALUES ($1)", {"p": "hunter2"}, Exception("x"))
    described = _describe(exc)
    assert "INSERT" not in described and "hunter2" not in described
    assert _describe(ValueError("bad")) == "ValueError: bad"
