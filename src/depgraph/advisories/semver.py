"""SemVer 2.0.0 parsing and precedence (https://semver.org, s11).

This is *precedence*, not npm's range-satisfaction rules. node-semver's
`satisfies()` deliberately excludes prereleases from ranges unless the range
mentions that exact [major, minor, patch], which is an install-time policy
("don't hand me a beta by surprise"). Vulnerability ranges ask a different
question: is this version ordered inside [introduced, fixed)? By precedence,
1.5.15-beta < 1.5.15, so a prerelease of the fixing version is still affected.
OSV specifies SEMVER ranges this way.
"""

import re
from dataclasses import dataclass, field
from functools import total_ordering

# The official regex from semver.org, plus npm's tolerated prefixes ("v1.2.3",
# "=1.2.3") and surrounding whitespace.
_SEMVER = re.compile(
    r"^\s*[v=]?\s*"
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<build>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?\s*$"
)


_NUMERIC_SHORTHAND = re.compile(r"^\s*[v=]?\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")


class InvalidVersionError(ValueError):
    pass


@total_ordering
@dataclass(frozen=True, slots=True, eq=False)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...] = ()
    build: str | None = None
    _key: tuple = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Precedence key. A release sorts after all of its prereleases (flag 1 vs 0).
        # Within a prerelease, numeric identifiers sort before alphanumeric ones
        # ((0, n) < (1, s)), numbers compare numerically, strings in ASCII order, and
        # a shorter list that is a prefix of a longer one sorts first (tuple rules).
        pre = tuple((0, p) if isinstance(p, int) else (1, p) for p in self.prerelease)
        key = (self.major, self.minor, self.patch, 0 if self.prerelease else 1, pre)
        object.__setattr__(self, "_key", key)

    @classmethod
    def parse(cls, text: str) -> "Version":
        m = _SEMVER.match(text) if isinstance(text, str) else None
        if m is None:
            raise InvalidVersionError(f"not a semantic version: {text!r}")
        pre = m["pre"]
        return cls(
            int(m["major"]),
            int(m["minor"]),
            int(m["patch"]),
            tuple(int(p) if p.isdigit() else p for p in pre.split(".")) if pre else (),
            m["build"],
        )

    @classmethod
    def parse_bound(cls, text: str) -> "Version":
        """Parse an advisory range bound, tolerating numeric shorthand.

        Real OSV npm data has a few ECOSYSTEM bounds like "13.0", "1.3" and
        "2026.04.1". npm can't publish such versions, so they are shorthand for
        13.0.0, 1.3.0 and 2026.4.1 (node-semver's `coerce` reads them the same
        way). Only all-numeric shorthand is coerced; anything else still raises.
        Installed versions are always parsed strictly.
        """
        try:
            return cls.parse(text)
        except InvalidVersionError:
            m = _NUMERIC_SHORTHAND.match(text) if isinstance(text, str) else None
            if m is None:
                raise
            return cls(*(int(part) if part else 0 for part in m.groups()))

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    # Build metadata is ignored for precedence *and* equality (spec s10).
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key == other._key

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key < other._key

    def __hash__(self) -> int:
        return hash(self._key)

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            s += "-" + ".".join(str(p) for p in self.prerelease)
        if self.build:
            s += "+" + self.build
        return s
