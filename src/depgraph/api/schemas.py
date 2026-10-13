"""Response models. Explicit, so the OpenAPI schema documents the contract and
internal columns never leak by accident."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: Literal["pending", "processing", "completed", "failed"]
    lockfile_sha256: str
    lockfile_version: int | None
    root_name: str | None
    node_count: int | None
    edge_count: int | None
    warnings: list[dict[str, Any]]
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: str | None
    source: str
    ref: str | None = Field(description="Path or URL for non-registry packages")
    dev: bool
    optional: bool
    dev_optional: bool
    peer: bool
    install_paths: list[str]


class EdgeOut(BaseModel):
    parent: int
    child: int
    dep_type: str
    declared_range: str | None


class GraphOut(BaseModel):
    scan: ScanOut
    root: int
    nodes: list[NodeOut]
    edges: list[EdgeOut]


class PathStep(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    version: str | None
    dep_type: str | None = Field(description="Type of the edge into this node; null for root")


class PathsOut(BaseModel):
    package: str
    version: str | None
    targets: list[NodeOut] = Field(description="Nodes in the scan matching the query")
    paths: list[list[PathStep]] = Field(description="Root-to-target paths, shortest first")
    truncated: bool = Field(description="A cap was hit; more paths may exist")
    max_paths: int
    max_depth: int


# ------------------------------------------------------------------ findings

Severity = Literal["CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN"]
Kind = Literal["vulnerability", "malicious"]


class AdvisoryRef(BaseModel):
    id: str
    kind: Kind
    severity: Severity
    cvss_v3_score: float | None
    summary: str | None
    aliases: list[str] = Field(description="CVE / other GHSA ids")
    published: datetime | None
    url: str = Field(description="Human-readable advisory page on osv.dev")


class FindingSummary(BaseModel):
    id: int
    package: str
    version: str
    advisory: AdvisoryRef
    direct: bool = Field(description="The project depends on this package directly")
    dev_only: bool = Field(description="Only reachable via devDependencies: not in prod installs")
    shortest_path: list[PathStep] | None = Field(
        description="Shortest root-to-package path; null if unreachable within caps"
    )
    first_detected: datetime
    last_confirmed: datetime


class SeverityCounts(BaseModel):
    """Every severity is always present (0 if none), so clients needn't guess keys.

    An explicit model rather than dict[Severity, int]: a dict renders in OpenAPI
    as an open string->int map, which typed clients can't rely on.
    """

    CRITICAL: int = 0
    HIGH: int = 0
    MODERATE: int = 0
    LOW: int = 0
    UNKNOWN: int = 0


class FindingsOut(BaseModel):
    scan_id: uuid.UUID
    matched_at: datetime | None = Field(
        description="When findings were last computed for this scan; null: not yet"
    )
    counts: "SeverityCounts" = Field(description="Findings per severity, before filtering")
    findings: list[FindingSummary]


class ParentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    version: str | None
    is_root: bool
    dep_type: str
    declared_range: str | None
    allows_fix: bool | None = Field(description="Declared range admits the fix; null: unknown")
    upgrade_to: str | None = Field(description="Lowest release of this parent that admits it")


class FixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fixed_version: str | None
    source: Literal["registry", "advisory"] | None
    breaking: bool | None = Field(description="Fix is a semver-major change from installed")
    direct: bool
    action: Literal[
        "update_lockfile", "bump_direct", "upgrade_parent_or_override", "no_fix", "remove"
    ]
    parents: list[ParentOut]
    message: str
    overrides: dict[str, str] = Field(description="Snippet for the package.json overrides field")


class AdvisoryDetail(AdvisoryRef):
    details: str | None
    cvss_v3_vector: str | None
    cvss_v4_vector: str | None
    modified: datetime
    references: list[dict[str, str]]


class FindingDetail(BaseModel):
    id: int
    scan_id: uuid.UUID
    package: str
    version: str
    dev_only: bool
    advisory: AdvisoryDetail
    first_detected: datetime
    last_confirmed: datetime
    paths: list[list[PathStep]] = Field(description="Root-to-package paths, shortest first")
    paths_truncated: bool
    fix: FixOut
