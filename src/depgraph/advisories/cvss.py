"""CVSS v3.x base score from a vector string (FIRST CVSS v3.1 specification, s7.1).

OSV publishes CVSS as vectors ("CVSS:3.1/AV:N/AC:L/..."), not numbers. We compute
the base score so findings can be ranked and filtered numerically. v3.0 vectors
use the same weights; the only difference is v3.1's floating-point-safe Roundup,
which we apply to both (it differs from v3.0's naive ceil only in the rare cases
where float error crossed a tenth). CVSS v4 is a different, lookup-table model;
we keep v4 vectors but don't score them.
"""

import math

_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.5}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}
_REQUIRED = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")


class InvalidCvssVectorError(ValueError):
    pass


def base_score_v3(vector: str) -> float:
    metrics = _parse(vector)
    try:
        changed = metrics["S"] == "C"
        if metrics["S"] not in ("U", "C"):
            raise KeyError("S")
        iss = 1 - ((1 - _CIA[metrics["C"]]) * (1 - _CIA[metrics["I"]]) * (1 - _CIA[metrics["A"]]))
        pr = (_PR_CHANGED if changed else _PR_UNCHANGED)[metrics["PR"]]
        exploitability = 8.22 * _AV[metrics["AV"]] * _AC[metrics["AC"]] * pr * _UI[metrics["UI"]]
    except KeyError as exc:
        raise InvalidCvssVectorError(f"bad value for {exc} in {vector!r}") from exc

    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    if impact <= 0:
        return 0.0
    if changed:
        return _roundup(min(1.08 * (impact + exploitability), 10))
    return _roundup(min(impact + exploitability, 10))


def severity_from_score(score: float) -> str:
    """CVSS v3 qualitative rating, using GHSA's word for 'Medium'."""
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MODERATE"
    if score > 0:
        return "LOW"
    return "UNKNOWN"


def _parse(vector: str) -> dict[str, str]:
    prefix, _, rest = vector.partition("/")
    if prefix not in ("CVSS:3.0", "CVSS:3.1") or not rest:
        raise InvalidCvssVectorError(f"not a CVSS v3 vector: {vector!r}")
    metrics: dict[str, str] = {}
    for part in rest.split("/"):
        key, sep, value = part.partition(":")
        if not sep or key in metrics:
            raise InvalidCvssVectorError(f"malformed metric {part!r} in {vector!r}")
        metrics[key] = value
    missing = [k for k in _REQUIRED if k not in metrics]
    if missing:
        raise InvalidCvssVectorError(f"missing base metrics {missing} in {vector!r}")
    return metrics  # temporal/environmental metrics are ignored: base score only


def _roundup(value: float) -> float:
    """Smallest one-decimal number >= value, computed on integers (spec Appendix A)."""
    as_int = round(value * 100_000)
    if as_int % 10_000 == 0:
        return as_int / 100_000
    return (math.floor(as_int / 10_000) + 1) / 10
