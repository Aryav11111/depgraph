"""Fix suggestions: the minimum safe version, and how hard it is to get there.

`suggest_fix` is pure. Its inputs are gathered by `fix_for_finding` from the
scan graph (who depends on the vulnerable node, with which declared ranges), the
advisory (its affected entries for the package) and the npm registry (which
versions actually exist).

Minimum fixed version
    The lowest *published* version above the installed one that no entry of the
    advisory affects. Evaluated against real registry versions rather than read
    off `fixed` events: ranges can have several intervals, `last_affected`
    bounds and explicit version lists, and a `fixed` version might never have
    been published. Prereleases are skipped (unless the installed version is
    one) and so are deprecated releases (unless nothing else fixes it).
    If the registry is unreachable we fall back to the advisory's own `fixed`
    bounds and say so (`source="advisory"`).

Effort (`action`)
    direct dependency:
        fix inside the declared range -> UPDATE_LOCKFILE  (npm update <pkg>)
        otherwise                     -> BUMP_DIRECT      (edit package.json)
    transitive dependency:
        fix inside every parent's declared range -> UPDATE_LOCKFILE
        some parent's range excludes it          -> UPGRADE_PARENT_OR_OVERRIDE
    no published version fixes it -> NO_FIX
    known-malicious package       -> REMOVE

"Inside the range" uses npm's own `satisfies` rules (npm_range.py), because
the question is what npm would install, not how versions are ordered.
"""

import asyncio
import enum
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.advisories.matcher import InvalidRangeError, affects_entry
from depgraph.advisories.npm_range import satisfies
from depgraph.advisories.semver import InvalidVersionError, Version
from depgraph.findings.report import FindingRow
from depgraph.graph.queries import nodes_by_id
from depgraph.registry.npm import NpmRegistryClient, RegistryError

log = structlog.get_logger(__name__)


class Action(enum.StrEnum):
    UPDATE_LOCKFILE = "update_lockfile"
    BUMP_DIRECT = "bump_direct"
    UPGRADE_PARENT_OR_OVERRIDE = "upgrade_parent_or_override"
    NO_FIX = "no_fix"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class Parent:
    """A package that depends on the vulnerable one, as resolved in this scan."""

    name: str
    version: str | None
    is_root: bool
    dep_type: str
    declared_range: str | None
    allows_fix: bool | None = None  # None: range unknown / not semver
    # Lowest published release of this parent that would accept the fix (only
    # computed for blocking, non-root parents when registry data is available).
    upgrade_to: str | None = None


@dataclass(frozen=True, slots=True)
class ParentRelease:
    deprecated: bool
    dependencies: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class FixSuggestion:
    package: str
    installed: str
    fixed_version: str | None
    source: str | None  # "registry" | "advisory" | None
    breaking: bool | None  # fix is outside ^installed (a semver-major change)
    direct: bool
    action: Action
    parents: tuple[Parent, ...]
    message: str
    overrides: Mapping[str, str] = field(default_factory=dict)


def suggest_fix(
    *,
    package: str,
    installed: str,
    kind: str,
    entries: Sequence[Mapping[str, Any]],
    published: Mapping[str, str | None] | None,
    parents: Sequence[Parent],
    parent_releases: Mapping[str, Mapping[str, ParentRelease]] | None = None,
) -> FixSuggestion:
    """`entries`: the advisory's affected entries for `package` ({ranges, versions}).
    `published`: registry versions -> deprecation message (None if not deprecated);
    None when the registry couldn't be reached.
    `parent_releases`: parent name -> its published releases, used to compute the
    minimum parent upgrade; parents missing here simply get no `upgrade_to`."""
    current = Version.parse(installed)
    fixed, source = _minimum_fix(current, entries, published)
    direct = any(p.is_root for p in parents)
    judged = tuple(replace(p, allows_fix=_allows(fixed, p.declared_range)) for p in parents)
    if fixed is not None and parent_releases:
        judged = tuple(
            replace(p, upgrade_to=minimum_parent_upgrade(
                p.version, parent_releases.get(p.name), package, fixed))
            if p.allows_fix is not True and not p.is_root else p
            for p in judged
        )  # fmt: skip
    # Who stands between us and the fix: parents whose declared range excludes it,
    # or that we can't evaluate (git/tag specs).
    blockers = [p for p in judged if p.allows_fix is not True]
    breaking = None if fixed is None else satisfies(fixed, f"^{current}") is False

    if kind == "malicious":
        action = Action.REMOVE
        message = (
            f"{package}@{installed} is a known-malicious release. Remove it, and rotate any "
            "credentials available to the install environment (install scripts run on install)."
        )
    elif fixed is None:
        action = Action.NO_FIX
        message = (
            f"No published version of {package} fixes this. Consider removing or replacing "
            f"{package}, or mitigating the vulnerable code path."
        )
    elif not blockers:
        action = Action.UPDATE_LOCKFILE
        message = (
            f"{package}@{fixed} already satisfies every declared range for {package}: "
            f"`npm update {package}` (or regenerating the lockfile) resolves it, "
            "no package.json change needed."
        )
    elif all(p.is_root for p in blockers):
        action = Action.BUMP_DIRECT
        message = (
            f"{package} is a direct dependency: change its range in package.json to "
            f'"^{fixed}"'
            + (" (a semver-major upgrade: check for breaking changes)." if breaking else ".")
        )
    else:
        action = Action.UPGRADE_PARENT_OR_OVERRIDE
        named = ", ".join(
            "package.json" if p.is_root else f"{p.name}@{p.version} ({p.declared_range})"
            for p in blockers
        )
        upgrades = [f"{p.name} to {p.upgrade_to}" for p in blockers if p.upgrade_to]
        how = f"Upgrade {', '.join(upgrades)}" if upgrades else "Upgrade the blocking parent"
        message = (
            f"{named} does not (or may not) allow {package}@{fixed}. {how}, or force it "
            f'with an npm override: "overrides": {{"{package}": "{fixed}"}}.'
        )

    return FixSuggestion(
        package=package,
        installed=installed,
        fixed_version=str(fixed) if fixed else None,
        source=source if fixed else None,
        breaking=breaking,
        direct=direct,
        action=action,
        parents=judged,
        message=message,
        overrides={package: str(fixed)}
        if action is Action.UPGRADE_PARENT_OR_OVERRIDE and fixed
        else {},
    )


def minimum_parent_upgrade(
    current: str | None,
    releases: Mapping[str, ParentRelease] | None,
    package: str,
    fixed: Version,
) -> str | None:
    """Lowest stable, non-deprecated release of a parent, newer than `current`,
    whose declared range for `package` accepts `fixed`, or that dropped the
    dependency entirely.

    One level deep, deliberately: it answers "which parent release stops pinning
    the vulnerable version?". Whether that release brings other changes is for
    normal upgrade testing; reproducing npm's full resolver is out of scope.
    """
    if not current or not releases:
        return None
    try:
        now = Version.parse(current)
    except InvalidVersionError:
        return None
    candidates = []
    for text_version, release in releases.items():
        try:
            v = Version.parse(text_version)
        except InvalidVersionError:
            continue
        if v > now and not v.is_prerelease and not release.deprecated:
            candidates.append((v, release))
    for v, release in sorted(candidates, key=lambda c: c[0]):
        spec = release.dependencies.get(package)
        if spec is None or satisfies(fixed, spec) is True:
            return str(v)
    return None


def _allows(fixed: Version | None, declared: str | None) -> bool | None:
    if fixed is None or not declared:
        return None
    return satisfies(fixed, declared)


def _affected(v: Version, entries: Sequence[Mapping[str, Any]]) -> bool:
    for e in entries:
        try:
            if affects_entry(v, e.get("ranges") or [], e.get("versions") or []):
                return True
        except InvalidRangeError:
            return True  # can't prove it's safe: never recommend it
    return False


def _minimum_fix(
    current: Version,
    entries: Sequence[Mapping[str, Any]],
    published: Mapping[str, str | None] | None,
) -> tuple[Version | None, str | None]:
    if published is not None:
        candidates: list[tuple[Version, bool]] = []
        for text_version, deprecated in published.items():
            try:
                v = Version.parse(text_version)
            except InvalidVersionError:
                continue
            if v <= current or (v.is_prerelease and not current.is_prerelease):
                continue
            candidates.append((v, deprecated is not None))
        candidates.sort(key=lambda c: c[0])
        safe = [(v, dep) for v, dep in candidates if not _affected(v, entries)]
        preferred = next((v for v, dep in safe if not dep), None)
        fallback = safe[0][0] if safe else None
        return (preferred or fallback), "registry"

    # Registry unavailable: the advisory's own `fixed` bounds, verified unaffected.
    bounds = set()
    for e in entries:
        for r in e.get("ranges") or []:
            for event in r.get("events") or []:
                if "fixed" in event:
                    try:
                        bounds.add(Version.parse_bound(event["fixed"]))
                    except InvalidVersionError:
                        continue
    for v in sorted(bounds):
        if v > current and not _affected(v, entries):
            return v, "advisory"
    return None, None


# ------------------------------------------------------------------ DB glue


async def fix_for_finding(
    session: AsyncSession, npm: NpmRegistryClient | None, finding: FindingRow
) -> FixSuggestion:
    entries = [
        {"ranges": ranges, "versions": versions}
        for ranges, versions in await session.execute(
            text(
                "SELECT ranges, versions FROM advisory_affected"
                " WHERE advisory_id = :a AND ecosystem = 'npm' AND package = :p"
            ),
            {"a": finding.advisory_id, "p": finding.package},
        )
    ]
    parents = await _parents(session, finding.scan_id, finding.node_id)
    published = None
    parent_releases: dict[str, dict[str, ParentRelease]] = {}
    if npm is not None:
        try:
            packument = await npm.get_packument(finding.package)
            published = {v: info.deprecated for v, info in packument.versions.items()}
        except RegistryError as exc:
            log.warning("fix_registry_unavailable", package=finding.package, error=str(exc))
        # Parents' release histories, for "upgrade X to Y". Fetched concurrently;
        # the client's single-flight and Redis cache make repeats cheap.
        names = sorted({p.name for p in parents if not p.is_root})
        results = await asyncio.gather(
            *(npm.get_packument(n) for n in names), return_exceptions=True
        )
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("fix_parent_registry_unavailable", package=name, error=str(result))
                continue
            parent_releases[name] = {
                v: ParentRelease(info.deprecated is not None, info.dependencies)
                for v, info in result.versions.items()
            }

    return suggest_fix(
        package=finding.package,
        installed=finding.version,
        kind=finding.kind,
        entries=entries,
        published=published,
        parents=parents,
        parent_releases=parent_releases,
    )


async def _parents(session: AsyncSession, scan_id: uuid.UUID, node_id: int) -> list[Parent]:
    rows = list(
        await session.execute(
            text(
                "SELECT e.parent_node_id, e.dep_type, e.declared_range FROM scan_edges e"
                " WHERE e.scan_id = :s AND e.child_node_id = :n"
                " ORDER BY e.parent_node_id, e.dep_type"
            ),
            {"s": scan_id, "n": node_id},
        )
    )
    labels = await nodes_by_id(session, sorted({r[0] for r in rows}))
    return [
        Parent(
            name=labels[pid].name,
            version=labels[pid].version,
            is_root=labels[pid].source == "root",
            dep_type=dep_type,
            declared_range=declared,
        )
        for pid, dep_type, declared in rows
    ]
