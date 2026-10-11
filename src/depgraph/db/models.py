"""ORM models.

Two halves, deliberately:

* Global, deduplicated catalogue: ``packages`` and ``package_versions``. On npm a
  published ``name@version`` is immutable, so one row serves every scan that uses it.
* Per-scan graph: ``scan_nodes`` and ``scan_edges``. *Which* version a dependency
  resolves to is a property of a specific lockfile, so edges can't be global.

See NOTES.md, "Stage 1 / step 2", for the alternatives considered.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from depgraph.db.base import Base

# Text + CHECK rather than native Postgres ENUMs: adding a value to a CHECK is a
# plain constraint swap, whereas ALTER TYPE ... ADD VALUE has transactional caveats.
SCAN_STATUSES = ("pending", "processing", "completed", "failed")
NODE_SOURCES = ("root", "registry", "workspace", "git", "file", "remote")
DEP_TYPES = ("prod", "dev", "optional", "peer", "peer_optional")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Package(Base):
    __tablename__ = "packages"
    __table_args__ = (UniqueConstraint("ecosystem", "name"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Only 'npm' today; the column exists so a second ecosystem is not a migration
    # that rewrites every unique key.
    ecosystem: Mapped[str] = mapped_column(Text, server_default="npm")
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PackageVersion(Base):
    __tablename__ = "package_versions"
    __table_args__ = (UniqueConstraint("package_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("packages.id", ondelete="RESTRICT"))
    # Raw version string. Ordering needs semver parsing, done in Python when needed.
    version: Mapped[str] = mapped_column(Text)
    # Subresource-integrity hash from the lockfile (first seen). Immutable on npm,
    # so a later scan reporting a different hash would be a tampering signal.
    integrity: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (CheckConstraint(_in("status", SCAN_STATUSES), name="status"),)

    # UUID because it is exposed in URLs: not enumerable, no row-count leak.
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    lockfile_sha256: Mapped[str] = mapped_column(Text)
    lockfile_version: Mapped[int | None] = mapped_column(SmallInteger)
    root_name: Mapped[str | None] = mapped_column(Text)
    node_count: Mapped[int | None] = mapped_column(Integer)
    edge_count: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScanNode(Base):
    """One package in one scan's resolved graph.

    Identity is ``name@version`` per scan: a version installed at several
    ``node_modules`` paths is a single node, with every path kept in
    ``install_paths``. Non-registry nodes (the root project, workspaces, git/file
    deps) have no immutable ``name@version``, so they carry ``name``/``ref`` here
    instead of pointing at the global catalogue.
    """

    __tablename__ = "scan_nodes"
    __table_args__ = (
        CheckConstraint(_in("source", NODE_SOURCES), name="source"),
        CheckConstraint(
            "(source = 'registry' AND version_id IS NOT NULL AND name IS NULL AND ref IS NULL)"
            " OR (source <> 'registry' AND version_id IS NULL"
            " AND name IS NOT NULL AND ref IS NOT NULL)",
            name="registry_xor_local",
        ),
        UniqueConstraint("scan_id", "version_id", name="uq_scan_nodes_scan_id_version_id"),
        UniqueConstraint("scan_id", "source", "ref", name="uq_scan_nodes_scan_id_source_ref"),
        # Target of the composite FKs from scan_edges (see there).
        UniqueConstraint("scan_id", "id", name="uq_scan_nodes_scan_id_id"),
        Index(
            "uq_scan_nodes_one_root_per_scan",
            "scan_id",
            unique=True,
            postgresql_where=text("source = 'root'"),
        ),
        # "Which scans contain this package version?" without touching edges.
        Index("ix_scan_nodes_version_id", "version_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(Text)
    version_id: Mapped[int | None] = mapped_column(
        ForeignKey("package_versions.id", ondelete="RESTRICT")
    )
    name: Mapped[str | None] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(Text)  # '.', workspace path, git/file URL
    # Lockfile flags, mirrored as-is.
    is_dev: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_optional: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_dev_optional: Mapped[bool] = mapped_column(Boolean, server_default="false")
    is_peer: Mapped[bool] = mapped_column(Boolean, server_default="false")
    install_paths: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")


class ScanEdge(Base):
    """parent --depends on--> child, as resolved in one scan."""

    __tablename__ = "scan_edges"
    __table_args__ = (
        CheckConstraint(_in("dep_type", DEP_TYPES), name="dep_type"),
        # Composite FKs: an edge can only join two nodes of *its own* scan.
        ForeignKeyConstraint(
            ["scan_id", "parent_node_id"],
            ["scan_nodes.scan_id", "scan_nodes.id"],
            ondelete="CASCADE",
            name="fk_scan_edges_parent_scan_nodes",
        ),
        ForeignKeyConstraint(
            ["scan_id", "child_node_id"],
            ["scan_nodes.scan_id", "scan_nodes.id"],
            ondelete="CASCADE",
            name="fk_scan_edges_child_scan_nodes",
        ),
        # PK prefix (scan_id, parent_node_id) serves forward traversal; this serves
        # the reverse walk used by "paths to package X".
        Index("ix_scan_edges_scan_id_child_node_id", "scan_id", "child_node_id"),
    )

    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), primary_key=True
    )
    parent_node_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    child_node_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dep_type: Mapped[str] = mapped_column(Text, primary_key=True)
    # The range the parent declared (e.g. "^4.17.0"); null if the lockfile omits it.
    declared_range: Mapped[str | None] = mapped_column(Text)
