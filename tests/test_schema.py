"""Schema-level guarantees: the constraints reject the data they exist to reject.

These run against real Postgres because the interesting rules (partial unique
index, composite FKs, CHECKs, cascades) live in the database, not in Python.
"""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.db.models import Package, PackageVersion, Scan, ScanEdge, ScanNode


async def _version(db: AsyncSession, name: str, version: str) -> PackageVersion:
    pkg = Package(name=name)
    db.add(pkg)
    await db.flush()
    pv = PackageVersion(package_id=pkg.id, version=version)
    db.add(pv)
    await db.flush()
    return pv


async def _scan(db: AsyncSession) -> Scan:
    scan = Scan(lockfile_sha256="0" * 64)
    db.add(scan)
    await db.flush()
    return scan


def _root(scan: Scan) -> ScanNode:
    return ScanNode(scan_id=scan.id, source="root", name="my-app", ref=".")


def _registry(scan: Scan, pv: PackageVersion) -> ScanNode:
    return ScanNode(scan_id=scan.id, source="registry", version_id=pv.id)


async def _count(db: AsyncSession, model: type, **filters: object) -> int:
    return await db.scalar(select(func.count()).select_from(model).filter_by(**filters))


async def _assert_rejected(db: AsyncSession, *objs: object) -> None:
    db.add_all(objs)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


# --- migrations -------------------------------------------------------------


def test_migrations_downgrade_and_upgrade_cleanly(migrated, request):
    cfg = Config(str(request.config.rootpath / "alembic.ini"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_models_and_migrations_have_not_drifted(migrated, request):
    # Fails if someone edits a model without generating a migration.
    command.check(Config(str(request.config.rootpath / "alembic.ini")))


# --- happy path -------------------------------------------------------------


async def test_a_small_graph_with_a_cycle_round_trips(db: AsyncSession):
    scan = await _scan(db)
    a, b = await _version(db, "a", "1.0.0"), await _version(db, "b", "2.0.0")
    root, na, nb = _root(scan), _registry(scan, a), _registry(scan, b)
    db.add_all([root, na, nb])
    await db.flush()

    # root -> a -> b -> a : npm permits cycles, so the schema must too.
    for parent, child in [(root, na), (na, nb), (nb, na)]:
        db.add(
            ScanEdge(
                scan_id=scan.id,
                parent_node_id=parent.id,
                child_node_id=child.id,
                dep_type="prod",
                declared_range="^1",
            )
        )
    await db.flush()

    assert await _count(db, ScanEdge, scan_id=scan.id) == 3


async def test_package_versions_are_shared_across_scans(db: AsyncSession):
    pv = await _version(db, "lodash", "4.17.21")
    s1, s2 = await _scan(db), await _scan(db)
    db.add_all([_registry(s1, pv), _registry(s2, pv)])
    await db.flush()


# --- constraints ------------------------------------------------------------


async def test_duplicate_package_version_rejected(db: AsyncSession):
    pv = await _version(db, "left-pad", "1.3.0")
    await _assert_rejected(db, PackageVersion(package_id=pv.package_id, version="1.3.0"))


async def test_same_version_twice_in_one_scan_rejected(db: AsyncSession):
    # name@version is the node identity: duplicates in the lockfile collapse to one.
    scan, pv = await _scan(db), await _version(db, "ms", "2.1.3")
    db.add(_registry(scan, pv))
    await db.flush()
    await _assert_rejected(db, _registry(scan, pv))


async def test_only_one_root_per_scan(db: AsyncSession):
    scan = await _scan(db)
    db.add(_root(scan))
    await db.flush()
    await _assert_rejected(db, ScanNode(scan_id=scan.id, source="root", name="x", ref="other"))


@pytest.mark.parametrize(
    "fields",
    [
        {"source": "registry"},  # registry node without a version
        {"source": "registry", "name": "x", "ref": "."},  # registry node with local fields
        {"source": "workspace", "ref": "packages/a"},  # local node without a name
        {"source": "bogus", "name": "x", "ref": "."},  # unknown source
    ],
)
async def test_node_shape_check(db: AsyncSession, fields):
    scan = await _scan(db)
    await _assert_rejected(db, ScanNode(scan_id=scan.id, **fields))


async def test_registry_node_with_local_fields_and_version_rejected(db: AsyncSession):
    scan, pv = await _scan(db), await _version(db, "x", "1.0.0")
    await _assert_rejected(
        db, ScanNode(scan_id=scan.id, source="registry", version_id=pv.id, name="x", ref=".")
    )


async def test_edge_cannot_cross_scans(db: AsyncSession):
    s1, s2 = await _scan(db), await _scan(db)
    r1, r2 = _root(s1), _root(s2)
    db.add_all([r1, r2])
    await db.flush()
    await _assert_rejected(
        db, ScanEdge(scan_id=s1.id, parent_node_id=r1.id, child_node_id=r2.id, dep_type="prod")
    )


async def test_unknown_dep_type_rejected(db: AsyncSession):
    scan, pv = await _scan(db), await _version(db, "x", "1.0.0")
    root, node = _root(scan), _registry(scan, pv)
    db.add_all([root, node])
    await db.flush()
    await _assert_rejected(
        db,
        ScanEdge(
            scan_id=scan.id, parent_node_id=root.id, child_node_id=node.id, dep_type="bundled"
        ),
    )


async def test_unknown_scan_status_rejected(db: AsyncSession):
    await _assert_rejected(db, Scan(lockfile_sha256="0" * 64, status="done"))


# --- lifecycle --------------------------------------------------------------


async def test_deleting_a_scan_cascades_to_its_graph_but_keeps_the_catalogue(db: AsyncSession):
    scan, pv = await _scan(db), await _version(db, "chalk", "5.3.0")
    root, node = _root(scan), _registry(scan, pv)
    db.add_all([root, node])
    await db.flush()
    db.add(
        ScanEdge(scan_id=scan.id, parent_node_id=root.id, child_node_id=node.id, dep_type="prod")
    )
    await db.flush()

    await db.delete(scan)
    await db.flush()

    assert await _count(db, ScanNode, scan_id=scan.id) == 0
    assert await _count(db, ScanEdge, scan_id=scan.id) == 0
    # A real query, not db.get(), which could be served from the identity map.
    assert await _count(db, PackageVersion, id=pv.id) == 1


async def test_version_in_use_cannot_be_deleted(db: AsyncSession):
    scan, pv = await _scan(db), await _version(db, "debug", "4.3.4")
    db.add(_registry(scan, pv))
    await db.flush()
    await db.delete(pv)
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_scan_ids_are_server_generated_uuids(db: AsyncSession):
    scan_id = await db.scalar(insert(Scan).values(lockfile_sha256="0" * 64).returning(Scan.id))
    assert isinstance(scan_id, uuid.UUID)
