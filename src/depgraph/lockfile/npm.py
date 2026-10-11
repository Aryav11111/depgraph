"""package-lock.json (lockfileVersion 2 and 3) -> install-path dependency graph.

Pure: bytes/str/dict in, immutable data out. No I/O, no DB, no logging, so it can
be tested exhaustively and reused anywhere (API, worker, CLI).

The graph is keyed by *install path* ("node_modules/a/node_modules/b"), i.e. it
reports exactly what the lockfile says. Collapsing paths into one node per
name@version is a storage policy, applied by the graph builder, not here.

Format notes (npm docs, "package-lock.json"):
* v2 has the modern ``packages`` map plus a legacy ``dependencies`` tree for npm 6;
  v3 has only ``packages``. We read ``packages`` for both and ignore the legacy tree.
* ``packages[""]`` is the root project. Every other key is a directory relative to
  it, normally under node_modules, or a workspace / local directory.
* Which entry satisfies a dependency is *not* recorded; it is implied by Node's
  module resolution, which we replicate in ``_resolve``.
"""

import fnmatch
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import unquote, urlparse

SUPPORTED_VERSIONS = frozenset({2, 3})
ROOT = ""
_NM = "node_modules"


class LockfileError(ValueError):
    """Base for every error the parser raises."""


class InvalidLockfileError(LockfileError):
    """Not JSON, or not shaped like a package-lock.json."""


class UnsupportedLockfileVersionError(LockfileError):
    def __init__(self, version: object) -> None:
        hint = " (npm <= 6; regenerate with npm >= 7)" if version == 1 else ""
        super().__init__(f"unsupported lockfileVersion {version!r}{hint}; expected 2 or 3")
        self.version = version


class Source(StrEnum):
    ROOT = "root"
    REGISTRY = "registry"
    WORKSPACE = "workspace"
    GIT = "git"
    FILE = "file"
    REMOTE = "remote"


class DepType(StrEnum):
    PROD = "prod"
    DEV = "dev"
    OPTIONAL = "optional"
    PEER = "peer"
    PEER_OPTIONAL = "peer_optional"


# A missing dependency of these types is normal (e.g. an optional platform package
# npm chose not to install, or a peer the consumer opted out of), not a defect.
_MAY_BE_MISSING = frozenset({DepType.OPTIONAL, DepType.PEER_OPTIONAL})


@dataclass(frozen=True, slots=True)
class LockNode:
    path: str
    name: str
    version: str | None
    source: Source
    resolved: str | None = None
    integrity: str | None = None
    dev: bool = False
    optional: bool = False
    dev_optional: bool = False
    peer: bool = False
    in_bundle: bool = False


@dataclass(frozen=True, slots=True, order=True)
class LockEdge:
    parent: str
    child: str
    dep_type: DepType
    spec: str | None  # the declared range, e.g. "^4.17.0"; None for implicit edges


@dataclass(frozen=True, slots=True, order=True)
class Unresolved:
    """A declared dependency that no lockfile entry satisfies."""

    parent: str
    name: str
    dep_type: DepType
    spec: str
    required: bool  # False when absence is legitimate (optional / optional peer)


@dataclass(frozen=True, slots=True)
class LockfileGraph:
    lockfile_version: int
    nodes: Mapping[str, LockNode]  # by install path; ROOT ("") is the project
    edges: tuple[LockEdge, ...]
    unresolved: tuple[Unresolved, ...]

    @property
    def root(self) -> LockNode:
        return self.nodes[ROOT]


# --------------------------------------------------------------------------- API


def parse_package_lock(raw: bytes | str | Mapping[str, Any]) -> LockfileGraph:
    doc = _load(raw)

    version = doc.get("lockfileVersion")
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedLockfileVersionError(version)

    packages = doc.get("packages")
    if not isinstance(packages, Mapping) or not isinstance(packages.get(ROOT), Mapping):
        raise InvalidLockfileError('missing "packages" map or its root ("") entry')
    for key, entry in packages.items():
        if not isinstance(key, str) or not isinstance(entry, Mapping):
            raise InvalidLockfileError(f"malformed packages entry {key!r}")

    workspace_globs = _workspace_globs(packages[ROOT])

    nodes: dict[str, LockNode] = {}
    for path, entry in packages.items():
        if entry.get("link"):
            continue  # a symlink, not a package: resolution follows it to its target
        nodes[path] = _make_node(path, doc, entry, workspace_globs)

    edges: set[LockEdge] = set()
    unresolved: set[Unresolved] = set()

    for path in nodes:
        for name, dep_type, spec in _declared_deps(packages[path], path):
            child = _resolve(path, name, packages)
            if child is not None and child in nodes:
                edges.add(LockEdge(path, child, dep_type, spec))
            else:
                unresolved.add(
                    Unresolved(path, name, dep_type, spec, required=dep_type not in _MAY_BE_MISSING)
                )

    # Workspaces are implicit top-level dependencies of the root project: npm
    # installs them without the root declaring them, so add the edges ourselves
    # unless the root already declares the workspace explicitly.
    linked_from_root = {e.child for e in edges if e.parent == ROOT}
    for path, node in nodes.items():
        if node.source is Source.WORKSPACE and path not in linked_from_root:
            edges.add(LockEdge(ROOT, path, DepType.PROD, None))

    return LockfileGraph(
        lockfile_version=version,
        nodes=dict(sorted(nodes.items())),
        edges=tuple(sorted(edges)),
        unresolved=tuple(sorted(unresolved)),
    )


# ------------------------------------------------------------------- internals


def _load(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidLockfileError(f"not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise InvalidLockfileError("top-level JSON value must be an object")
    return doc


def _workspace_globs(root_entry: Mapping[str, Any]) -> tuple[str, ...]:
    ws = root_entry.get("workspaces")
    if isinstance(ws, Mapping):  # yarn-style {"packages": [...]} is also accepted by npm
        ws = ws.get("packages")
    if not isinstance(ws, list):
        return ()
    return tuple(g.rstrip("/").removeprefix("./") for g in ws if isinstance(g, str))


def _is_installed_path(path: str) -> bool:
    """True for paths npm installed into a node_modules directory."""
    return path == _NM or path.startswith(f"{_NM}/") or f"/{_NM}/" in path


def _name_from_path(path: str) -> str:
    """'node_modules/a/node_modules/@s/b' -> '@s/b'; 'packages/web' -> 'web'."""
    _, sep, tail = path.rpartition(f"{_NM}/")
    return tail if sep else path.rstrip("/").rsplit("/", 1)[-1]


def _make_node(
    path: str, doc: Mapping[str, Any], entry: Mapping[str, Any], workspace_globs: tuple[str, ...]
) -> LockNode:
    resolved = _str_or_none(entry.get("resolved"))
    # "name" is present when it differs from the folder name: npm aliases
    # ("foo": "npm:bar@1") install bar under node_modules/foo with name "bar".
    name = _str_or_none(entry.get("name"))
    version = _str_or_none(entry.get("version"))

    if path == ROOT:
        source = Source.ROOT
        name = name or _str_or_none(doc.get("name")) or "(root)"
    else:
        name = name or _name_from_path(path)
        source = _classify(path, name, version, resolved, entry, workspace_globs)

    return LockNode(
        path=path,
        name=name,
        version=version,
        source=source,
        resolved=resolved,
        integrity=_str_or_none(entry.get("integrity")),
        dev=bool(entry.get("dev")),
        optional=bool(entry.get("optional")),
        dev_optional=bool(entry.get("devOptional")),
        peer=bool(entry.get("peer")),
        in_bundle=bool(entry.get("inBundle")),
    )


def _classify(
    path: str,
    name: str,
    version: str | None,
    resolved: str | None,
    entry: Mapping[str, Any],
    workspace_globs: tuple[str, ...],
) -> Source:
    if not _is_installed_path(path):
        # A local directory (target of a link): a workspace if the root claims it.
        if any(fnmatch.fnmatchcase(path, g) for g in workspace_globs):
            return Source.WORKSPACE
        return Source.FILE

    if resolved is None:
        # Bundled deps ship inside their parent's tarball; and npm can be configured
        # to omit `resolved` for registry packages. Both are registry code.
        return Source.REGISTRY
    if resolved.startswith(("git+", "git:", "github:", "gitlab:", "bitbucket:")):
        return Source.GIT
    if resolved.startswith("file:"):
        return Source.FILE
    if resolved.startswith(("http://", "https://")):
        return Source.REGISTRY if _is_registry_tarball(resolved, name, version) else Source.REMOTE
    return Source.REMOTE


def _is_registry_tarball(url: str, name: str, version: str | None) -> bool:
    """Registry layout: <base>/<name>/-/<unscoped-name>-<version>.tgz.

    Matching on the URL *shape* rather than the host means private registries
    (Artifactory, Verdaccio, GitHub Packages) count as registry sources too.
    """
    if version is None:
        return False
    path = unquote(urlparse(url).path)
    unscoped = name.rsplit("/", 1)[-1]
    return f"/{name}/-/" in path and path.endswith(f"/{unscoped}-{version}.tgz")


def _declared_deps(entry: Mapping[str, Any], path: str) -> Iterator[tuple[str, DepType, str]]:
    """Yield (name, type, spec) for every dependency the entry declares."""
    prod = _str_map(entry.get("dependencies"))
    optional = _str_map(entry.get("optionalDependencies"))
    # npm semantics: optionalDependencies override same-named dependencies.
    for name, spec in prod.items():
        if name not in optional:
            yield name, DepType.PROD, spec
    for name, spec in optional.items():
        yield name, DepType.OPTIONAL, spec

    peer_meta = entry.get("peerDependenciesMeta")
    peer_meta = peer_meta if isinstance(peer_meta, Mapping) else {}
    for name, spec in _str_map(entry.get("peerDependencies")).items():
        meta = peer_meta.get(name)
        is_optional = isinstance(meta, Mapping) and bool(meta.get("optional"))
        yield name, DepType.PEER_OPTIONAL if is_optional else DepType.PEER, spec

    # devDependencies are only installed for the project itself and local
    # directories (workspaces, linked file: deps); an installed package's dev deps
    # are never part of the install.
    if not _is_installed_path(path):
        for name, spec in _str_map(entry.get("devDependencies")).items():
            yield name, DepType.DEV, spec


def _resolve(from_path: str, name: str, packages: Mapping[str, Mapping[str, Any]]) -> str | None:
    """Return the install path Node would load `name` from, starting at `from_path`.

    Node checks <dir>/node_modules/<name> for the requiring package's directory and
    then each ancestor. (Node also skips ancestors that are themselves named
    "node_modules"; the candidate keys that would produce, like
    "node_modules/node_modules/x", can't occur in a lockfile, so no special case.)
    Links (workspaces, file: deps) are followed to their target directory.
    """
    for directory in _ancestors(from_path):
        candidate = f"{directory}/{_NM}/{name}" if directory else f"{_NM}/{name}"
        entry = packages.get(candidate)
        if entry is None:
            continue
        if entry.get("link"):
            target = _str_or_none(entry.get("resolved"))
            return target if target is not None and target in packages else None
        return candidate
    return None


def _ancestors(path: str) -> Iterator[str]:
    """'a/b/c' -> 'a/b/c', 'a/b', 'a', ''."""
    while path:
        yield path
        path = path.rpartition("/")[0]
    yield ROOT


def _str_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
