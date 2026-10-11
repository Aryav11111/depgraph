import pytest

from depgraph.advisories.cvss import InvalidCvssVectorError, base_score_v3, severity_from_score


# Expected scores from the FIRST CVSS v3.1 calculator / published NVD scores.
@pytest.mark.parametrize(
    ("vector", "score"),
    [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),  # classic RCE
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", 10.0),  # scope change caps at 10
        ("CVSS:3.1/AV:N/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:H", 7.2),  # lodash CVE-2021-23337
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),  # typical reflected XSS
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H", 7.5),  # ReDoS / DoS
        ("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:N/A:N", 5.5),
        ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.6),
        # PR:L weighs 0.68 (not 0.62) under S:C. Hand-checked: impact 6.048,
        # exploitability 1.777, x1.08 = 8.451 -> 8.5.
        ("CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:C/C:H/I:H/A:H", 8.5),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),  # no impact
        ("CVSS:3.0/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),  # v3.0 prefix accepted
        # temporal metrics present: base score unchanged
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/E:U/RL:O/RC:C", 9.8),
    ],
)
def test_base_scores_match_reference(vector, score):
    assert base_score_v3(vector) == score


def test_roundup_is_float_safe():
    # 4.0 must not become 4.1 through float error (the reason v3.1 changed Roundup).
    from depgraph.advisories.cvss import _roundup

    assert _roundup(4.000000000000001) == 4.0
    assert _roundup(4.02) == 4.1
    assert _roundup(4.0) == 4.0


@pytest.mark.parametrize(
    "vector",
    [
        "",
        "AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # no prefix
        "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H",  # missing A
        "CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # bad value
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:Q/C:H/I:H/A:H",  # bad scope
        "CVSS:3.1/AV:N/AV:L/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # duplicate metric
        "CVSS:3.1/AVN/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",  # missing colon
    ],
)
def test_invalid_vectors_raise(vector):
    with pytest.raises(InvalidCvssVectorError):
        base_score_v3(vector)


@pytest.mark.parametrize(
    ("score", "level"),
    [(10, "CRITICAL"), (9.0, "CRITICAL"), (8.9, "HIGH"), (7.0, "HIGH"), (6.9, "MODERATE"),
     (4.0, "MODERATE"), (3.9, "LOW"), (0.1, "LOW"), (0, "UNKNOWN")],
)  # fmt: skip
def test_severity_bands(score, level):
    assert severity_from_score(score) == level
