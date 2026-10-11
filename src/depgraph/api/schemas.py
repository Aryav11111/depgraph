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
