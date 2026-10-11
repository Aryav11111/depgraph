"""Persist a collapsed graph and drive a scan's status lifecycle.

`persist_graph` writes inside the caller's transaction (so tests can roll it back);
`run_build` owns the transactions and the status machine:

    pending -> processing -> completed        (graph + counts in one transaction)
                          \\-> failed          (graph rolled back, error recorded)

Every bulk write is a single `INSERT ... SELECT FROM unnest(<one array per
column>)`: one round trip per table regardless of graph size. (executemany sends
a statement per row; it was measured several times slower on the 385-node fixture,
and the gap grows linearly.) Arrays are built in sorted key order, so two scans
inserting overlapping catalogue rows lock them in the same order and can't deadlock.
"""

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from depgraph.core.errors import safe_error_text
from depgraph.db.models import Scan
from depgraph.graph.collapse import CollapsedGraph, NodeKey, collapse
from depgraph.lockfile.npm import LockfileGraph, Source

log = structlog.get_logger(__name__)

ECOSYSTEM = "npm"

_INSERT_PACKAGES = text(
    """
    INSERT INTO packages (ecosystem, name)
    SELECT :eco, t.name FROM unnest(CAST(:names AS text[])) AS t(name)
    ON CONFLICT (ecosystem, name) DO NOTHING
    """
)

# First-seen integrity wins; it is only filled in if we never had one. The WHERE
# makes the common case (already known) a no-op instead of a row update.
_UPSERT_VERSIONS = text(
    """
    INSERT INTO package_versions (package_id, version, integrity)
    SELECT p.id, t.version, t.integrity
    FROM unnest(CAST(:names AS text[]), CAST(:versions AS text[]),
                CAST(:integrities AS text[])) WITH ORDINALITY AS t(name, version, integrity, i)
    JOIN packages p ON p.ecosystem = :eco AND p.name = t.name
    ORDER BY t.i
    ON CONFLICT (package_id, version) DO UPDATE SET integrity = EXCLUDED.integrity
    WHERE package_versions.integrity IS NULL AND EXCLUDED.integrity IS NOT NULL
    """
)

_SELECT_VERSIONS = text(
    """
    SELECT pv.id, p.name, pv.version, pv.integrity
    FROM unnest(CAST(:names AS text[]), CAST(:versions AS text[])) AS t(name, version)
    JOIN packages p ON p.ecosystem = :eco AND p.name = t.name
    JOIN package_versions pv ON pv.package_id = p.id AND pv.version = t.version
    """
)

# install_paths is itself an array, and Postgres multi-dimensional arrays must be
# rectangular, so each node's paths travel as a JSON string, unpacked server-side.
_INSERT_NODES = text(
    """
    INSERT INTO scan_nodes (scan_id, source, version_id, name, ref, is_dev, is_optional,
                            is_dev_optional, is_peer, install_paths)
    SELECT CAST(:scan_id AS uuid), t.source, t.version_id, t.name, t.ref, t.dev, t.optional,
           t.dev_optional, t.peer, ARRAY(SELECT jsonb_array_elements_text(t.paths::jsonb))
    FROM unnest(CAST(:sources AS text[]), CAST(:version_ids AS bigint[]),
                CAST(:names AS text[]), CAST(:refs AS text[]), CAST(:dev AS boolean[]),
                CAST(:optional AS boolean[]), CAST(:dev_optional AS boolean[]),
                CAST(:peer AS boolean[]), CAST(:paths AS text[]))
         AS t(source, version_id, name, ref, dev, optional, dev_optional, peer, paths)
    RETURNING id, source, version_id, ref
    """
)

_INSERT_EDGES = text(
    """
    INSERT INTO scan_edges (scan_id, parent_node_id, child_node_id, dep_type, declared_range)
    SELECT CAST(:scan_id AS uuid), t.parent, t.child, t.dep_type, t.spec
    FROM unnest(CAST(:parents AS bigint[]), CAST(:children AS bigint[]),
                CAST(:dep_types AS text[]), CAST(:specs AS text[]))
         AS t(parent, child, dep_type, spec)
    """
)


async def persist_graph(
    session: AsyncSession, scan_id: uuid.UUID, graph: CollapsedGraph
) -> list[dict[str, Any]]:
    """Insert catalogue rows, nodes and edges. Returns extra warnings found on the way."""
    warnings: list[dict[str, Any]] = []
    version_ids = await _upsert_catalogue(session, graph, warnings)

    nodes = list(graph.nodes.values())  # already sorted by key
    registry = [n.source is Source.REGISTRY for n in nodes]
    vids = [
        version_ids[(n.name, n.version)] if r else None
        for n, r in zip(nodes, registry, strict=True)
    ]
    result = await session.execute(
        _INSERT_NODES,
        {
            "scan_id": scan_id,
            "sources": [n.source.value for n in nodes],
            "version_ids": vids,
            "names": [None if r else n.name for n, r in zip(nodes, registry, strict=True)],
            "refs": [n.ref for n in nodes],
            "dev": [n.dev for n in nodes],
            "optional": [n.optional for n in nodes],
            "dev_optional": [n.dev_optional for n in nodes],
            "peer": [n.peer for n in nodes],
            "paths": [json.dumps(n.install_paths) for n in nodes],
        },
    )
    # RETURNING order isn't guaranteed, so map rows back by their per-scan natural
    # key: version_id for registry nodes, (source, ref) otherwise. These are exactly
    # the keys the unique constraints enforce, so the mapping is unambiguous.
    by_natural = {
        (source, version_id if source == Source.REGISTRY else ref): node_id
        for node_id, source, version_id, ref in result
    }
    node_id: dict[NodeKey, int] = {
        n.key: by_natural[(n.source.value, vid if r else n.ref)]
        for n, vid, r in zip(nodes, vids, registry, strict=True)
    }

    if graph.edges:
        await session.execute(
            _INSERT_EDGES,
            {
                "scan_id": scan_id,
                "parents": [node_id[e.parent] for e in graph.edges],
                "children": [node_id[e.child] for e in graph.edges],
                "dep_types": [e.dep_type.value for e in graph.edges],
                "specs": [e.spec for e in graph.edges],
            },
        )
    return warnings


async def _upsert_catalogue(
    session: AsyncSession, graph: CollapsedGraph, warnings: list[dict[str, Any]]
) -> dict[tuple[str, str], int]:
    """Get-or-create packages and versions; return (name, version) -> version id."""
    registry = [n for n in graph.nodes.values() if n.source is Source.REGISTRY]
    if not registry:
        return {}

    names = sorted({n.name for n in registry})
    await session.execute(_INSERT_PACKAGES, {"eco": ECOSYSTEM, "names": names})

    wanted = sorted({(n.name, n.version, n.integrity) for n in registry}, key=lambda r: r[:2])
    columns = {
        "eco": ECOSYSTEM,
        "names": [w[0] for w in wanted],
        "versions": [w[1] for w in wanted],
    }
    await session.execute(_UPSERT_VERSIONS, {**columns, "integrities": [w[2] for w in wanted]})
    rows = await session.execute(_SELECT_VERSIONS, columns)

    ids: dict[tuple[str, str], int] = {}
    ours = {(n, v): i for n, v, i in wanted}
    for vid, name, version, known in rows:
        ids[(name, version)] = vid
        lockfile = ours[(name, version)]
        # npm versions are immutable: the same name@version with a different hash
        # than we've recorded before is a tampering / registry-mixup signal.
        if known and lockfile and known != lockfile:
            warnings.append(
                {
                    "kind": "integrity_mismatch",
                    "package": f"{name}@{version}",
                    "lockfile": lockfile,
                    "catalogue": known,
                }
            )
    return ids


async def run_build(
    sessionmaker: async_sessionmaker[AsyncSession], scan_id: uuid.UUID, graph: LockfileGraph
) -> None:
    """Build and persist a scan's graph, recording the outcome on the scan row.

    Re-raises after marking the scan failed, so callers (API/worker) can decide
    how to surface it.
    """
    bound = log.bind(scan_id=str(scan_id))
    started = time.perf_counter()
    async with sessionmaker.begin() as session:
        await session.execute(update(Scan).where(Scan.id == scan_id).values(status="processing"))

    try:
        collapsed = collapse(graph)
        async with sessionmaker.begin() as session:
            extra = await persist_graph(session, scan_id, collapsed)
            warnings = [*collapsed.warnings, *extra]
            await session.execute(
                update(Scan)
                .where(Scan.id == scan_id)
                .values(
                    status="completed",
                    lockfile_version=collapsed.lockfile_version,
                    root_name=collapsed.nodes[collapsed.root].name,
                    node_count=len(collapsed.nodes),
                    edge_count=len(collapsed.edges),
                    warnings=warnings,
                    completed_at=datetime.now(UTC),
                )
            )
    except Exception as exc:
        bound.exception("scan_build_failed")
        async with sessionmaker.begin() as session:
            await session.execute(
                update(Scan)
                .where(Scan.id == scan_id)
                .values(status="failed", error=_describe(exc), completed_at=datetime.now(UTC))
            )
        raise

    bound.info(
        "scan_build_completed",
        nodes=len(collapsed.nodes),
        edges=len(collapsed.edges),
        warnings=len(warnings),
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def _describe(exc: BaseException) -> str:
    return safe_error_text(exc, db_message="database error while storing the graph")
