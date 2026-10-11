"""Read queries over a stored scan graph."""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.graph.paths import Parents


@dataclass(frozen=True, slots=True)
class NodeRow:
    id: int
    name: str
    version: str | None
    source: str
    ref: str | None
    dev: bool
    optional: bool
    dev_optional: bool
    peer: bool
    install_paths: list[str]


# Registry nodes take name/version from the catalogue; local/git/... nodes carry
# their own name (and have no catalogued version).
_NODE_COLUMNS = """
    n.id, COALESCE(p.name, n.name) AS name, pv.version, n.source, n.ref,
    n.is_dev, n.is_optional, n.is_dev_optional, n.is_peer, n.install_paths
"""
_NODE_FROM = """
    FROM scan_nodes n
    LEFT JOIN package_versions pv ON pv.id = n.version_id
    LEFT JOIN packages p ON p.id = pv.package_id
"""


async def list_nodes(session: AsyncSession, scan_id: uuid.UUID) -> list[NodeRow]:
    rows = await session.execute(
        text(f"SELECT {_NODE_COLUMNS} {_NODE_FROM} WHERE n.scan_id = :scan ORDER BY n.id"),
        {"scan": scan_id},
    )
    return [NodeRow(*r) for r in rows]


async def nodes_by_id(session: AsyncSession, ids: Sequence[int]) -> dict[int, NodeRow]:
    rows = await session.execute(
        text(f"SELECT {_NODE_COLUMNS} {_NODE_FROM} WHERE n.id = ANY(:ids)"),
        {"ids": list(ids)},
    )
    return {r.id: NodeRow(*r) for r in rows}


async def find_package_nodes(
    session: AsyncSession, scan_id: uuid.UUID, name: str, version: str | None
) -> list[NodeRow]:
    rows = await session.execute(
        text(
            f"SELECT {_NODE_COLUMNS} {_NODE_FROM}"
            " WHERE n.scan_id = :scan AND COALESCE(p.name, n.name) = :name"
            " AND (CAST(:version AS text) IS NULL OR pv.version = :version)"
            " ORDER BY n.id"
        ),
        {"scan": scan_id, "name": name, "version": version},
    )
    return [NodeRow(*r) for r in rows]


async def list_edges(
    session: AsyncSession, scan_id: uuid.UUID
) -> list[tuple[int, int, str, str | None]]:
    rows = await session.execute(
        text(
            "SELECT parent_node_id, child_node_id, dep_type, declared_range FROM scan_edges"
            " WHERE scan_id = :scan ORDER BY parent_node_id, child_node_id, dep_type"
        ),
        {"scan": scan_id},
    )
    return [tuple(r) for r in rows]


async def root_node_id(session: AsyncSession, scan_id: uuid.UUID) -> int | None:
    return await session.scalar(
        text("SELECT id FROM scan_nodes WHERE scan_id = :scan AND source = 'root'"),
        {"scan": scan_id},
    )


async def ancestor_parents(
    session: AsyncSession, scan_id: uuid.UUID, targets: Sequence[int]
) -> Parents:
    """Parent links restricted to the targets' ancestors.

    The recursive CTE computes the ancestor *set*: UNION (not UNION ALL) discards
    already-seen nodes, so cycles terminate. Only edges into that set are loaded,
    so the in-memory path search never sees the parts of the graph that can't
    reach a target. Each step uses the (scan_id, child_node_id) index.
    """
    rows = await session.execute(
        text(
            """
            WITH RECURSIVE ancestors(node_id) AS (
                SELECT unnest(CAST(:targets AS bigint[]))
                UNION
                SELECT e.parent_node_id
                FROM scan_edges e JOIN ancestors a ON e.child_node_id = a.node_id
                WHERE e.scan_id = :scan
            )
            SELECT e.child_node_id, e.parent_node_id, e.dep_type
            FROM scan_edges e
            WHERE e.scan_id = :scan AND e.child_node_id IN (SELECT node_id FROM ancestors)
            """
        ),
        {"scan": scan_id, "targets": list(targets)},
    )
    parents: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for child, parent, dep_type in rows:
        parents[child].append((parent, dep_type))
    return parents
