"""Normalize OSV records (https://ossf.github.io/osv-schema/) into our shape.

Pure: dict in, frozen dataclass out. Deliberately conservative: anything we
don't understand is kept verbatim (`raw`, range `events`) rather than dropped,
because range semantics are applied later by the matcher, not here.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from depgraph.advisories.cvss import InvalidCvssVectorError, base_score_v3, severity_from_score

ECOSYSTEM = "npm"
SEVERITIES = ("CRITICAL", "HIGH", "MODERATE", "LOW", "UNKNOWN")
KINDS = ("vulnerability", "malicious")


class InvalidAdvisoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AffectedRange:
    type: str  # SEMVER | ECOSYSTEM | GIT
    events: tuple[Mapping[str, str], ...]  # [{"introduced": "0"}, {"fixed": "1.2.3"}, ...]


@dataclass(frozen=True, slots=True)
class Affected:
    ecosystem: str
    package: str
    ranges: tuple[AffectedRange, ...]
    versions: tuple[str, ...]  # explicitly enumerated affected versions


@dataclass(frozen=True, slots=True)
class Advisory:
    id: str
    kind: str
    summary: str | None
    details: str | None
    severity: str
    cvss_v3_vector: str | None
    cvss_v3_score: float | None
    cvss_v4_vector: str | None
    aliases: tuple[str, ...]
    published: datetime | None
    modified: datetime
    withdrawn: datetime | None
    affected: tuple[Affected, ...]
    raw: Mapping[str, Any]


def parse_osv(doc: Mapping[str, Any], *, ecosystem: str = ECOSYSTEM) -> Advisory:
    adv_id = doc.get("id")
    if not isinstance(adv_id, str) or not adv_id:
        raise InvalidAdvisoryError("record has no id")
    modified = parse_timestamp(doc.get("modified"))
    if modified is None:
        raise InvalidAdvisoryError(f"{adv_id}: missing or invalid 'modified'")

    db_specific = doc.get("database_specific") or {}
    malicious = adv_id.startswith("MAL-") or "malicious-packages-origins" in db_specific

    v3_vector = v4_vector = None
    for entry in doc.get("severity") or []:
        kind, score = entry.get("type"), entry.get("score")
        if kind == "CVSS_V3" and isinstance(score, str):
            v3_vector = score
        elif kind == "CVSS_V4" and isinstance(score, str):
            v4_vector = score
    v3_score = None
    if v3_vector:
        try:
            v3_score = base_score_v3(v3_vector)
        except InvalidCvssVectorError:
            v3_score = None  # keep the vector for humans; don't invent a number

    return Advisory(
        id=adv_id,
        kind="malicious" if malicious else "vulnerability",
        summary=_str(doc.get("summary")),
        details=_str(doc.get("details")),
        severity=_severity(malicious, db_specific, v3_score),
        cvss_v3_vector=v3_vector,
        cvss_v3_score=v3_score,
        cvss_v4_vector=v4_vector,
        aliases=tuple(
            sorted({a for a in doc.get("aliases") or [] if isinstance(a, str) and a != adv_id})
        ),
        published=parse_timestamp(doc.get("published")),
        modified=modified,
        withdrawn=parse_timestamp(doc.get("withdrawn")),
        affected=tuple(_affected(doc.get("affected") or [], ecosystem)),
        raw=doc,
    )


def _severity(malicious: bool, db_specific: Mapping[str, Any], v3_score: float | None) -> str:
    # A known-malicious package is the worst case regardless of any score:
    # installing it runs attacker code. Surfacing it as CRITICAL is a policy
    # choice (NOTES.md); `kind` still distinguishes it from vulnerabilities.
    if malicious:
        return "CRITICAL"
    # GHSA's own rating when present (it is curated, and covers records with
    # only a v4 vector); otherwise derived from the CVSS v3 base score.
    label = str(db_specific.get("severity") or "").upper()
    label = {"MEDIUM": "MODERATE"}.get(label, label)
    if label in SEVERITIES:
        return label
    return severity_from_score(v3_score) if v3_score is not None else "UNKNOWN"


def _affected(entries: list[Any], ecosystem: str) -> list[Affected]:
    out = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        package = entry.get("package") or {}
        if package.get("ecosystem") != ecosystem or not isinstance(package.get("name"), str):
            continue  # multi-ecosystem advisories: keep only what we can match
        ranges = []
        for r in entry.get("ranges") or []:
            events = tuple(
                {k: v for k, v in e.items() if isinstance(v, str)}
                for e in r.get("events") or []
                if isinstance(e, Mapping)
            )
            if isinstance(r.get("type"), str) and events:
                ranges.append(AffectedRange(type=r["type"], events=events))
        versions = tuple(v for v in entry.get("versions") or [] if isinstance(v, str))
        out.append(
            Affected(
                ecosystem=ecosystem,
                package=package["name"],
                ranges=tuple(ranges),
                versions=versions,
            )
        )
    return out


# RFC 3339 with up to nanosecond precision ("...48.040851775Z"); Python's
# datetime holds microseconds, so extra fraction digits are truncated.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None  # naive timestamps are ambiguous: reject


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
