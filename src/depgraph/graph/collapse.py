"""Collapse an install-path graph into one node per package identity.

The parser reports what the lockfile says: one node per install path. The schema
stores one node per *identity* per scan (NOTES.md, step 2). This module is the
single place that policy lives. It is pure, so every merge rule is unit-testable
without a database.

Identity:
* registry -> (registry, name, version): published versions are immutable, so
  copies at different paths are the same code;
* root / workspace / local dir -> (source, name, directory);
* git / remote / file tarball -> (source, name, resolved URL): same URL, same code.
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from depgraph.lockfile.npm import ROOT, DepType, LockfileGraph, LockNode, Source

NodeKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class GraphNode:
    key: NodeKey
    source: Source
    name: str
    version: str | None
    ref: str | None  # None for registry nodes (identity is name@version)
    integrity: str | None
    dev: bool
    optional: bool
    dev_optional: bool
    peer: bool
    install_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True, order=True)
class GraphEdge:
    parent: NodeKey
    child: NodeKey
    dep_type: DepType
    spec: str | None


@dataclass(frozen=True, slots=True)
class CollapsedGraph:
    lockfile_version: int
    root: NodeKey
    nodes: Mapping[NodeKey, GraphNode]
    edges: tuple[GraphEdge, ...]
    warnings: tuple[dict[str, Any], ...] = field(default=())


def collapse(graph: LockfileGraph) -> CollapsedGraph:
    warnings: list[dict[str, Any]] = []
    key_of: dict[str, NodeKey] = {}
    members: dict[NodeKey, list[LockNode]] = defaultdict(list)

    for path, node in graph.nodes.items():
        key = _identity(node, warnings)
        key_of[path] = key
        members[key].append(node)

    nodes = {key: _merge(key, copies, warnings) for key, copies in sorted(members.items())}

    edges: dict[tuple[NodeKey, NodeKey, DepType], GraphEdge] = {}
    for e in graph.edges:
        parent, child = key_of[e.parent], key_of[e.child]
        # Copies of one package declare the same deps (same package.json), so a
        # duplicate here is the same edge seen from another install path.
        edges.setdefault((parent, child, e.dep_type), GraphEdge(parent, child, e.dep_type, e.spec))

    for u in graph.unresolved:
        if u.required:
            warnings.append(
                {
                    "kind": "unresolved_dependency",
                    "parent": u.parent or "(root)",
                    "name": u.name,
                    "spec": u.spec,
                    "dep_type": u.dep_type.value,
                }
            )

    return CollapsedGraph(
        lockfile_version=graph.lockfile_version,
        root=key_of[ROOT],
        nodes=nodes,
        edges=tuple(sorted(edges.values())),
        warnings=tuple(warnings),
    )


def _identity(node: LockNode, warnings: list[dict[str, Any]]) -> NodeKey:
    if node.source is Source.REGISTRY:
        if node.version is not None:
            return (Source.REGISTRY.value, node.name, node.version)
        # Can't be catalogued without a version; keep it, identified by its path.
        warnings.append({"kind": "registry_package_without_version", "path": node.path})
        return (Source.REMOTE.value, node.name, node.path)
    if node.source is Source.ROOT:
        return (Source.ROOT.value, node.name, ".")
    return (node.source.value, node.name, _ref(node))


def _ref(node: LockNode) -> str:
    # Local directories are identified by where they are; fetched code by where it
    # came from (two install paths of one git commit are one node).
    if node.source in (Source.GIT, Source.REMOTE, Source.FILE) and node.resolved:
        return node.resolved
    return node.path


def _merge(key: NodeKey, copies: list[LockNode], warnings: list[dict[str, Any]]) -> GraphNode:
    first = copies[0]
    integrities = sorted({c.integrity for c in copies if c.integrity})
    if len(integrities) > 1:
        warnings.append(
            {
                "kind": "integrity_conflict",
                "package": f"{first.name}@{first.version}",
                "integrities": integrities,
                "paths": sorted(c.path for c in copies),
            }
        )
    source = Source(key[0])
    return GraphNode(
        key=key,
        source=source,
        name=first.name,
        version=first.version,
        ref=None if source is Source.REGISTRY else key[2],
        integrity=integrities[0] if integrities else None,
        # A package is dev-only (or optional, ...) only if *every* copy of it is:
        # one production copy makes it ship.
        dev=all(c.dev for c in copies),
        optional=all(c.optional for c in copies),
        dev_optional=all(c.dev_optional for c in copies),
        peer=all(c.peer for c in copies),
        install_paths=tuple(sorted(c.path for c in copies)),
    )
