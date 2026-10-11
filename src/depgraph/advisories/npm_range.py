"""npm range satisfaction (node-semver's `satisfies`), for fix suggestions.

Different question, different semantics from `matcher.py`:

* matching asks "is this version *ordered* inside the affected interval?" ->
  SemVer precedence, where 1.5.0-beta is below 1.5.0.
* fixing asks "would npm *install* the fixed version given this declared
  range?" -> npm's range rules, including its prerelease policy: a prerelease
  only satisfies a range if some comparator in the same set names a prerelease
  of the same major.minor.patch (so ^1.2.0 does not pull in 1.3.0-beta).

Supported grammar (node-semver): `||` sets; hyphen ranges `A - B`; comparators
`< <= > >= =`; X-ranges (`*`, `x`, `1.x`, `1.2`); tilde `~1.2.3`; caret `^1.2.3`;
`npm:name@range` aliases. Anything else (dist-tags like "latest", git/file/URL
specs) is not a semver range: `satisfies` returns None ("unknown"), and callers
must not treat that as yes or no.
"""

import re
from dataclasses import dataclass

from depgraph.advisories.semver import InvalidVersionError, Version

_OPS = ("<=", ">=", "<", ">", "=")
_PARTIAL = re.compile(
    r"^v?(?P<maj>[xX*]|0|[1-9]\d*)"
    r"(?:\.(?P<min>[xX*]|0|[1-9]\d*)"
    r"(?:\.(?P<pat>[xX*]|0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)?)?$"
)
_NOT_A_RANGE = re.compile(r"^(?:[a-z+]+:|[\w.-]+/[\w.-]+(?:#.*)?$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Comparator:
    op: str  # one of _OPS
    version: Version

    def test(self, v: Version) -> bool:
        if self.op == "<":
            return v < self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == ">":
            return v > self.version
        if self.op == ">=":
            return v >= self.version
        return v == self.version


ANY: tuple[Comparator, ...] = ()  # an empty set matches every release


class InvalidRangeSpecError(ValueError):
    pass


def satisfies(version: Version | str, spec: str) -> bool | None:
    """node-semver `satisfies(version, spec)`; None if `spec` isn't a semver range."""
    try:
        sets = parse_range(spec)
    except InvalidRangeSpecError:
        return None
    v = version if isinstance(version, Version) else Version.parse(version)
    return any(_test_set(cs, v) for cs in sets)


def parse_range(spec: str) -> list[tuple[Comparator, ...]]:
    spec = spec.strip()
    if spec.startswith("npm:"):  # alias: "npm:lodash@^4.17.0" -> "^4.17.0"
        _, _, spec = spec[4:].rpartition("@")
        if not spec:
            return [ANY]
    if _NOT_A_RANGE.match(spec):
        raise InvalidRangeSpecError(f"not a semver range: {spec!r}")
    return [_parse_set(part.strip()) for part in spec.split("||")]


def _test_set(comparators: tuple[Comparator, ...], v: Version) -> bool:
    if not all(c.test(v) for c in comparators):
        return False
    if not v.is_prerelease:
        return True
    # npm's prerelease policy: allowed only when the range itself opts into a
    # prerelease of this exact major.minor.patch.
    return any(
        c.version.is_prerelease
        and (c.version.major, c.version.minor, c.version.patch) == (v.major, v.minor, v.patch)
        for c in comparators
    )


def _parse_set(text: str) -> tuple[Comparator, ...]:
    if not text:
        return ANY
    hyphen = re.fullmatch(r"(\S+)\s+-\s+(\S+)", text)
    if hyphen:
        return _hyphen(hyphen[1], hyphen[2])
    # Allow "> 1.2.3" (operator separated by whitespace), as node-semver does.
    tokens = re.sub(r"(<=|>=|<|>|=|~>?|\^)\s+", r"\1", text).split()
    out: list[Comparator] = []
    for token in tokens:
        out.extend(_parse_comparator(token))
    return tuple(out)


def _partial(text: str) -> tuple[int | None, int | None, int | None, str | None]:
    m = _PARTIAL.match(text)
    if m is None:
        raise InvalidRangeSpecError(f"bad version in range: {text!r}")

    def num(group: str) -> int | None:
        value = m[group]
        return None if value is None or value in "xX*" else int(value)

    major, minor, patch = num("maj"), num("min"), num("pat")
    # Anything after a wildcard is a wildcard too ("1.x.3" == "1.x").
    if major is None:
        minor = patch = None
    elif minor is None:
        patch = None
    return major, minor, patch, m["pre"] if patch is not None else None


def _v(major: int, minor: int = 0, patch: int = 0, pre: str | None = None) -> Version:
    try:
        return Version.parse(f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else ""))
    except InvalidVersionError as exc:
        raise InvalidRangeSpecError(str(exc)) from exc


def _upper_zero(major: int, minor: int = 0, patch: int = 0) -> Comparator:
    # "<X-0": below every prerelease of X too (node-semver's upper bound form).
    return Comparator("<", _v(major, minor, patch, "0"))


def _parse_comparator(token: str) -> list[Comparator]:
    if token.startswith("~"):
        return _tilde(token.lstrip("~>").lstrip("~"))
    if token.startswith("^"):
        return _caret(token[1:])
    op = next((o for o in _OPS if token.startswith(o)), "")
    major, minor, patch, pre = _partial(token[len(op) :])

    if major is None:  # "*", ">=*", "<*"
        return [Comparator("<", _v(0, 0, 0, "0"))] if op in ("<", ">") else list(ANY)
    if op in ("", "="):
        if minor is None:
            return [Comparator(">=", _v(major)), _upper_zero(major + 1)]
        if patch is None:
            return [Comparator(">=", _v(major, minor)), _upper_zero(major, minor + 1)]
        return [Comparator("=", _v(major, minor, patch, pre))]
    if patch is not None:
        return [Comparator(op, _v(major, minor, patch, pre))]
    # Comparators against a partial version, as node-semver desugars them.
    if op == ">":  # >1 -> >=2.0.0 ; >1.2 -> >=1.3.0
        return [Comparator(">=", _v(major + 1) if minor is None else _v(major, minor + 1))]
    if op == ">=":
        return [Comparator(">=", _v(major, minor or 0))]
    if op == "<":  # <1.2 -> <1.2.0-0
        return [Comparator("<", _v(major, minor or 0, 0, "0"))]
    # "<=": <=1 -> <2.0.0-0 ; <=1.2 -> <1.3.0-0
    return [_upper_zero(major + 1) if minor is None else _upper_zero(major, minor + 1)]


def _tilde(text: str) -> list[Comparator]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return list(ANY)
    if minor is None:  # ~1 -> >=1.0.0 <2.0.0-0
        return [Comparator(">=", _v(major)), _upper_zero(major + 1)]
    # ~1.2 / ~1.2.3 / ~1.2.3-beta -> >=1.2.x <1.3.0-0
    return [Comparator(">=", _v(major, minor, patch or 0, pre)), _upper_zero(major, minor + 1)]


def _caret(text: str) -> list[Comparator]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return list(ANY)
    low = Comparator(">=", _v(major, minor or 0, patch or 0, pre))
    # Caret allows changes that don't modify the left-most non-zero component.
    if minor is None:
        return [low, _upper_zero(major + 1)]
    if patch is None:
        return [low, _upper_zero(major + 1) if major else _upper_zero(0, minor + 1)]
    if major:
        return [low, _upper_zero(major + 1)]
    if minor:
        return [low, _upper_zero(0, minor + 1)]
    return [low, _upper_zero(0, 0, patch + 1)]


def _hyphen(low_text: str, high_text: str) -> tuple[Comparator, ...]:
    lo_major, lo_minor, lo_patch, lo_pre = _partial(low_text)
    hi_major, hi_minor, hi_patch, hi_pre = _partial(high_text)
    out: list[Comparator] = []
    if lo_major is not None:
        out.append(Comparator(">=", _v(lo_major, lo_minor or 0, lo_patch or 0, lo_pre)))
    if hi_major is None:
        pass
    elif hi_minor is None:  # "1.2.3 - 2" -> <3.0.0-0
        out.append(_upper_zero(hi_major + 1))
    elif hi_patch is None:  # "1.2.3 - 2.3" -> <2.4.0-0
        out.append(_upper_zero(hi_major, hi_minor + 1))
    else:
        out.append(Comparator("<=", _v(hi_major, hi_minor, hi_patch, hi_pre)))
    return tuple(out)
