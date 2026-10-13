"""Findings endpoints: what's vulnerable in a scan, how it got there, how to fix it."""

import uuid
from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import text

from depgraph.api.deps import NpmDep, SessionDep
from depgraph.api.scans import _completed_scan
from depgraph.api.schemas import (
    AdvisoryDetail,
    AdvisoryRef,
    FindingDetail,
    FindingsOut,
    FindingSummary,
    FixOut,
    Kind,
    PathStep,
    Severity,
    SeverityCounts,
)
from depgraph.findings.fixes import fix_for_finding
from depgraph.findings.report import (
    SEVERITY_RANK,
    FindingRow,
    finding_paths,
    get_finding,
    list_findings,
    shortest_paths,
)

router = APIRouter(prefix="/scans/{scan_id}/findings", tags=["findings"])


def _advisory_ref(row: FindingRow) -> dict[str, Any]:
    return {
        "id": row.advisory_id,
        "kind": row.kind,
        "severity": row.severity,
        "cvss_v3_score": row.cvss_v3_score,
        "summary": row.summary,
        "aliases": row.aliases,
        "published": row.published,
        "url": f"https://osv.dev/vulnerability/{row.advisory_id}",
    }


@router.get("", response_model=FindingsOut)
async def get_findings(
    request: Request,
    scan_id: uuid.UUID,
    session: SessionDep,
    severity: Annotated[
        list[Severity] | None, Query(description="Only these severities (repeatable)")
    ] = None,
    min_severity: Annotated[Severity | None, Query(description="This severity or worse")] = None,
    kind: Annotated[list[Kind] | None, Query(description="vulnerability and/or malicious")] = None,
) -> FindingsOut:
    """Every finding in the scan, most severe first, each with its shortest path."""
    scan = await _completed_scan(session, scan_id)
    settings = request.app.state.settings

    everything = await list_findings(session, scan_id)
    counts = Counter(r.severity for r in everything)

    wanted = set(severity or SEVERITY_RANK)
    if min_severity is not None:
        wanted &= {s for s, rank in SEVERITY_RANK.items() if rank <= SEVERITY_RANK[min_severity]}
    kinds = set(kind or ("vulnerability", "malicious"))
    rows = [r for r in everything if r.severity in wanted and r.kind in kinds]

    shortest = await shortest_paths(
        session,
        scan_id,
        [r.node_id for r in rows],
        max_depth=settings.paths_max_depth,
        max_expansions=settings.paths_max_expansions,
    )
    findings = []
    for r in rows:
        path = shortest.get(r.node_id)
        findings.append(
            FindingSummary(
                id=r.id,
                package=r.package,
                version=r.version,
                advisory=AdvisoryRef(**_advisory_ref(r)),
                direct=path is not None and len(path) == 2,  # root -> package
                dev_only=r.dev_only,
                shortest_path=[PathStep.model_validate(s) for s in path] if path else None,
                first_detected=r.first_detected,
                last_confirmed=r.last_confirmed,
            )
        )
    return FindingsOut(
        scan_id=scan_id,
        matched_at=scan.matched_at,
        counts=SeverityCounts(**{s: counts.get(s, 0) for s in SEVERITY_RANK}),
        findings=findings,
    )


@router.get("/{finding_id}", response_model=FindingDetail)
async def get_finding_detail(
    request: Request,
    scan_id: uuid.UUID,
    finding_id: int,
    session: SessionDep,
    npm: NpmDep,
    max_paths: Annotated[int | None, Query(ge=1, description="Cap on paths returned")] = None,
    max_depth: Annotated[int | None, Query(ge=1, description="Cap on path length")] = None,
) -> FindingDetail:
    """One finding: the advisory, every dependency path to it, and how to fix it."""
    await _completed_scan(session, scan_id)
    row = await get_finding(session, scan_id, finding_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "finding not found")

    settings = request.app.state.settings
    found = await finding_paths(
        session,
        row,
        max_paths=min(max_paths or settings.paths_default_max, settings.paths_hard_max),
        max_depth=min(max_depth or settings.paths_max_depth, settings.paths_max_depth),
        max_expansions=settings.paths_max_expansions,
    )
    fix = await fix_for_finding(session, npm, row)
    details, references, v3, v4, modified = (
        await session.execute(
            text(
                "SELECT raw->>'details', raw->'references', cvss_v3_vector, cvss_v4_vector,"
                " modified FROM advisories WHERE id = :id"
            ),
            {"id": row.advisory_id},
        )
    ).one()
    return FindingDetail(
        id=row.id,
        scan_id=scan_id,
        package=row.package,
        version=row.version,
        dev_only=row.dev_only,
        advisory=AdvisoryDetail(
            **_advisory_ref(row),
            details=details,
            cvss_v3_vector=v3,
            cvss_v4_vector=v4,
            modified=modified,
            references=[
                {"type": str(r.get("type", "")), "url": str(r.get("url", ""))}
                for r in references or []
                if isinstance(r, dict)
            ],
        ),
        first_detected=row.first_detected,
        last_confirmed=row.last_confirmed,
        paths=[[PathStep.model_validate(s) for s in p] for p in found.paths],
        paths_truncated=found.truncated,
        fix=FixOut.model_validate(fix),
    )
