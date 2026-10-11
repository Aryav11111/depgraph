"""Scan endpoints: upload a lockfile, read back its graph, query dependency paths."""

import asyncio
import hashlib
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from depgraph.api.deps import SessionDep
from depgraph.api.schemas import EdgeOut, GraphOut, NodeOut, PathsOut, PathStep, ScanOut
from depgraph.db.models import Scan
from depgraph.findings.matching import match_scan
from depgraph.graph import queries
from depgraph.graph.builder import run_build
from depgraph.graph.paths import find_paths
from depgraph.lockfile.npm import LockfileError, parse_package_lock

router = APIRouter(prefix="/scans", tags=["scans"])
log = structlog.get_logger(__name__)

_LOCKFILE_BODY = {
    "requestBody": {
        "required": True,
        "description": "The raw contents of package-lock.json (lockfileVersion 2 or 3).",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
}


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the body, refusing to buffer more than `limit` bytes."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:  # Content-Length can be absent (chunked) or wrong
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(limit: int) -> HTTPException:
    return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"lockfile exceeds {limit} bytes")


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ScanOut,
    openapi_extra=_LOCKFILE_BODY,
    responses={413: {"description": "Lockfile too large"}, 422: {"description": "Bad lockfile"}},
)
async def create_scan(request: Request, response: Response, session: SessionDep) -> Scan:
    """Upload a package-lock.json; it is parsed, resolved and stored synchronously."""
    settings = request.app.state.settings
    body = await _read_body(request, settings.max_lockfile_bytes)
    if not body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty request body")

    # Validate before creating anything: a lockfile we can't parse never becomes
    # a scan row. Parsing is CPU-bound, so keep it off the event loop.
    try:
        graph = await asyncio.to_thread(parse_package_lock, body)
    except LockfileError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    maker = request.app.state.sessionmaker
    async with maker.begin() as s:
        scan = Scan(lockfile_sha256=hashlib.sha256(body).hexdigest())
        s.add(scan)
        await s.flush()
        scan_id = scan.id
    log.info("scan_created", scan_id=str(scan_id), bytes=len(body), packages=len(graph.nodes) - 1)

    try:
        await run_build(maker, scan_id, graph)
    except Exception as exc:
        # run_build already recorded status=failed and a safe error message.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"building scan {scan_id} failed"
        ) from exc

    # Findings are best-effort at upload: if matching fails the graph is still
    # good, matched_at stays NULL, and the next advisory sync re-matches.
    try:
        async with maker.begin() as s:
            await match_scan(s, scan_id)
    except Exception:
        log.exception("scan_match_failed", scan_id=str(scan_id))

    response.headers["Location"] = f"/scans/{scan_id}"
    return await _get_scan(session, scan_id)


@router.get("/{scan_id}", response_model=ScanOut)
async def get_scan(scan_id: uuid.UUID, session: SessionDep) -> Scan:
    return await _get_scan(session, scan_id)


@router.get("/{scan_id}/graph", response_model=GraphOut)
async def get_graph(scan_id: uuid.UUID, session: SessionDep) -> GraphOut:
    """The full resolved graph: every package node and dependency edge."""
    scan = await _completed_scan(session, scan_id)
    nodes = await queries.list_nodes(session, scan_id)
    edges = await queries.list_edges(session, scan_id)
    return GraphOut(
        scan=ScanOut.model_validate(scan),
        root=next(n.id for n in nodes if n.source == "root"),
        nodes=[NodeOut.model_validate(n) for n in nodes],
        edges=[EdgeOut(parent=p, child=c, dep_type=t, declared_range=r) for p, c, t, r in edges],
    )


@router.get("/{scan_id}/paths", response_model=PathsOut)
async def get_paths(
    request: Request,
    scan_id: uuid.UUID,
    session: SessionDep,
    package: Annotated[str, Query(min_length=1, max_length=214, description="Package name")],
    version: Annotated[str | None, Query(max_length=256, description="Exact version")] = None,
    max_paths: Annotated[int | None, Query(ge=1, description="Cap on paths returned")] = None,
    max_depth: Annotated[int | None, Query(ge=1, description="Cap on path length")] = None,
) -> PathsOut:
    """Every dependency path from the project root to `package`, shortest first."""
    settings = request.app.state.settings
    max_paths = min(max_paths or settings.paths_default_max, settings.paths_hard_max)
    max_depth = min(max_depth or settings.paths_max_depth, settings.paths_max_depth)

    await _completed_scan(session, scan_id)
    targets = await queries.find_package_nodes(session, scan_id, package, version)
    result = PathsOut(
        package=package, version=version, targets=[NodeOut.model_validate(t) for t in targets],
        paths=[], truncated=False, max_paths=max_paths, max_depth=max_depth,
    )  # fmt: skip
    if not targets:
        return result

    root = await queries.root_node_id(session, scan_id)
    parents = await queries.ancestor_parents(session, scan_id, [t.id for t in targets])
    search = find_paths(
        parents,
        root,
        [t.id for t in targets],
        max_paths=max_paths,
        max_depth=max_depth,
        max_expansions=settings.paths_max_expansions,
    )
    labels = await queries.nodes_by_id(session, sorted({i for p in search.paths for i, _ in p}))
    result.paths = [
        [
            PathStep(id=i, name=labels[i].name, version=labels[i].version, dep_type=t)
            for i, t in path
        ]
        for path in search.paths
    ]
    result.truncated = search.truncated
    return result


async def _get_scan(session, scan_id: uuid.UUID) -> Scan:
    scan = await session.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scan not found")
    return scan


async def _completed_scan(session, scan_id: uuid.UUID) -> Scan:
    scan = await _get_scan(session, scan_id)
    if scan.status != "completed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"scan is {scan.status}; its graph is not available"
        )
    return scan
