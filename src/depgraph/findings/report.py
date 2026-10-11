"""Read side for findings: list/get, and dependency paths to each finding.

Paths are derived on read from the stored graph (never stored), so they can't
drift from the graph. Listing attaches each finding's *shortest* path using one
ancestor query for all findings plus an in-memory search per finding, instead
of two queries per finding.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.graph.paths import find_paths
from depgraph.graph.queries import (
    LabelledPaths,
    LabelledStep,
    ancestor_parents,
    labelled_paths,
    nodes_by_id,
    root_node_id,
)

SEVERITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3, "UNKNOWN": 4}


@dataclass(frozen=True, slots=True)
class FindingRow:
    id: int
    scan_id: uuid.UUID
    node_id: int
    package: str
    version: str
    advisory_id: str
    kind: str
    severity: str
    cvss_v3_score: float | None
    summary: str | None
    aliases: list[str]
    published: datetime | None
    first_detected: datetime
    last_confirmed: datetime


_SELECT = """
    SELECT f.id, f.scan_id, f.scan_node_id, p.name, pv.version, a.id, a.kind, a.severity,
           a.cvss_v3_score, a.summary,
           COALESCE((SELECT array_agg(al.alias ORDER BY al.alias) FROM advisory_aliases al
                     WHERE al.advisory_id = a.id), '{}') AS aliases,
           a.published, f.created_at, f.updated_at
    FROM findings f
    JOIN advisories a ON a.id = f.advisory_id
    JOIN scan_nodes n ON n.id = f.scan_node_id
    JOIN package_versions pv ON pv.id = n.version_id
    JOIN packages p ON p.id = pv.package_id
"""
_ORDER = """
    ORDER BY CASE a.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
             WHEN 'MODERATE' THEN 2 WHEN 'LOW' THEN 3 ELSE 4 END,
             a.cvss_v3_score DESC NULLS LAST, p.name, pv.version, a.id
"""


async def list_findings(
    session: AsyncSession,
    scan_id: uuid.UUID,
    *,
    severities: Collection[str] | None = None,
    kinds: Collection[str] | None = None,
) -> list[FindingRow]:
    """Most severe first (then CVSS score, then package name)."""
    where, params = ["f.scan_id = :scan"], {"scan": scan_id}
    if severities:
        where.append("a.severity = ANY(:sev)")
        params["sev"] = sorted(severities)
    if kinds:
        where.append("a.kind = ANY(:kinds)")
        params["kinds"] = sorted(kinds)
    rows = await session.execute(
        text(_SELECT + " WHERE " + " AND ".join(where) + _ORDER), params
    )
    return [FindingRow(*r) for r in rows]


async def get_finding(
    session: AsyncSession, scan_id: uuid.UUID, finding_id: int
) -> FindingRow | None:
    row = (
        await session.execute(
            text(_SELECT + " WHERE f.scan_id = :scan AND f.id = :id"),
            {"scan": scan_id, "id": finding_id},
        )
    ).first()
    return FindingRow(*row) if row else None


async def finding_paths(
    session: AsyncSession,
    finding: FindingRow,
    *,
    max_paths: int,
    max_depth: int,
    max_expansions: int,
) -> LabelledPaths:
    """Every root -> vulnerable-package path, shortest first, capped."""
    return await labelled_paths(
        session,
        finding.scan_id,
        [finding.node_id],
        max_paths=max_paths,
        max_depth=max_depth,
        max_expansions=max_expansions,
    )


async def shortest_paths(
    session: AsyncSession,
    scan_id: uuid.UUID,
    node_ids: Sequence[int],
    *,
    max_depth: int,
    max_expansions: int,
) -> dict[int, list[LabelledStep] | None]:
    """The shortest root -> node path for each node, batched (None: unreachable)."""
    targets = sorted(set(node_ids))
    if not targets:
        return {}
    root = await root_node_id(session, scan_id)
    # One CTE for the union of all targets' ancestors; each search below only
    # ever walks up from its own target, so sharing the map is safe.
    parents = await ancestor_parents(session, scan_id, targets)
    raw = {
        t: find_paths(parents, root, [t], max_paths=1, max_depth=max_depth,
                      max_expansions=max_expansions).paths  # fmt: skip
        for t in targets
    }
    labels = await nodes_by_id(session, sorted({i for ps in raw.values() for p in ps for i, _ in p}))
    return {
        t: [LabelledStep(i, labels[i].name, labels[i].version, d) for i, d in ps[0]] if ps else None
        for t, ps in raw.items()
    }
