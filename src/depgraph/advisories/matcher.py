"""Decide whether an installed version is affected by an OSV `affected` entry.

Pure. Implements the OSV schema's evaluation algorithm
(https://ossf.github.io/osv-schema/#evaluation) for SEMVER (and npm ECOSYSTEM)
ranges:

    sort the range's events by version ("0" is below everything);
    affected = False
    for each event, in order:
        introduced X:     if v >= X (or X == "0")  -> affected = True
        fixed X:          if v >= X                -> affected = False
        last_affected X:  if v >  X                -> affected = False
        limit X:          if v >= X                -> affected = False

A single range can encode several intervals (introduced/fixed/introduced/fixed);
the walk handles that. A version is affected by an entry if it appears in the
entry's explicit `versions` list OR any SEMVER/ECOSYSTEM range says so; it is
affected by an advisory if any of the advisory's entries for its package says so.
GIT ranges (commit hashes) can't apply to registry versions and are ignored.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from depgraph.advisories.semver import InvalidVersionError, Version

# OSV's npm records use SEMVER; the few ECOSYSTEM ranges for npm also mean npm
# semver, so both are evaluated the same way.
EVALUATED_RANGE_TYPES = frozenset({"SEMVER", "ECOSYSTEM"})

# Tie-break for events at the same version. introduced first: then
# introduced=X, fixed=X is the empty interval, and introduced=X,
# last_affected=X is exactly {X}, which are the only sensible readings.
_ORDER = {"introduced": 0, "last_affected": 1, "fixed": 2, "limit": 3}
_ZERO = object()  # introduced: "0", below every version


class InvalidRangeError(ValueError):
    """An event's version doesn't parse: the range can't be evaluated safely.

    Raised rather than guessed. Dropping a bad `fixed` would widen the range
    (false positives); dropping a bad `introduced` would hide it (false negatives).
    """


def affects_range(version: Version, events: Sequence[Mapping[str, str]]) -> bool:
    parsed: list[tuple[object, int, str]] = []
    for event in events:
        for kind, raw in event.items():
            if kind not in _ORDER:
                continue  # unknown event types: ignore, per OSV forward-compat
            if kind == "introduced" and raw == "0":
                parsed.append((_ZERO, _ORDER[kind], kind))
                continue
            try:
                parsed.append((Version.parse_bound(raw), _ORDER[kind], kind))
            except InvalidVersionError as exc:
                raise InvalidRangeError(f"unparseable {kind} version {raw!r}") from exc

    parsed.sort(key=_sort_key)
    affected = False
    for bound, _, kind in parsed:
        if kind == "introduced":
            if bound is _ZERO or version >= bound:
                affected = True
        elif kind == "fixed" or kind == "limit":
            if version >= bound:
                affected = False
        elif kind == "last_affected" and version > bound:
            affected = False
    return affected


def _sort_key(item: tuple[object, int, str]) -> tuple:
    bound, order, _ = item
    return (0,) if bound is _ZERO else (1, bound, order)


def affects_entry(
    version: str | Version, ranges: Iterable[Mapping[str, Any]], versions: Iterable[str] = ()
) -> bool:
    """One OSV `affected` entry: explicit versions list OR any evaluable range."""
    v = version if isinstance(version, Version) else Version.parse(version)
    for listed in versions:
        try:
            if Version.parse_bound(listed) == v:  # equality ignores build metadata
                return True
        except InvalidVersionError:
            if listed == str(version):
                return True
    for r in ranges:
        if r.get("type") in EVALUATED_RANGE_TYPES and affects_range(v, r.get("events") or ()):
            return True
    return False
