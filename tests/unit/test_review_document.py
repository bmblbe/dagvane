"""V1 structured review-document contract: value construction and boundary parsing.

Covers ``dagvane.ports.review`` (immutable values, fingerprint, redaction) and
``dagvane.protocol.review`` (closed-schema boundary parser). Held slice: not
wired to reviewer execution, routing, or persistence.
"""

from __future__ import annotations

import dataclasses
import json
import traceback

import pytest

from dagvane.domain.models import SpecError
from dagvane.ports.review import (
    ReviewDocumentV1,
    ReviewFindingV1,
    ReviewSeverity,
    ReviewValueError,
)
from dagvane.protocol.review import parse_review_document_v1

BASE = "a" * 40
CANDIDATE = "b" * 40
DIFF = "c" * 64


def _doc(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "base_sha": BASE,
        "candidate_sha": CANDIDATE,
        "diff_sha256": DIFF,
        "findings": [],
    }
    payload.update(overrides)
    return payload


def _parse(
    text: str,
    *,
    expected_base_sha: str = BASE,
    expected_candidate_sha: str = CANDIDATE,
    expected_diff_sha256: str = DIFF,
    max_document_bytes: int = 262_144,
    max_findings: int = 100,
) -> ReviewDocumentV1:
    return parse_review_document_v1(
        text,
        expected_base_sha=expected_base_sha,
        expected_candidate_sha=expected_candidate_sha,
        expected_diff_sha256=expected_diff_sha256,
        max_document_bytes=max_document_bytes,
        max_findings=max_findings,
    )


# ---------------------------------------------------------------------------
# ports.review: ReviewSeverity
# ---------------------------------------------------------------------------


def test_severity_members_exact() -> None:
    assert {m.value for m in ReviewSeverity} == {"BLOCKER", "MAJOR", "MINOR", "OPTIONAL"}


def test_severity_case_sensitive() -> None:
    with pytest.raises(ValueError):
        ReviewSeverity("blocker")


# ---------------------------------------------------------------------------
# ports.review: ReviewFindingV1
# ---------------------------------------------------------------------------


def test_finding_valid_minimal() -> None:
    finding = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="issue", file=None)
    assert finding.file is None
    assert finding.description == "issue"


def test_finding_valid_with_file() -> None:
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MINOR, description="issue", file="src/a/b.py"
    )
    assert finding.file == "src/a/b.py"


def test_finding_is_frozen() -> None:
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.description = "y"  # type: ignore[misc]


def test_finding_severity_wrong_type_rejected() -> None:
    with pytest.raises(ReviewValueError, match="ReviewSeverity"):
        ReviewFindingV1(severity="MAJOR", description="x", file=None)  # type: ignore[arg-type]


def test_finding_severity_bool_rejected() -> None:
    with pytest.raises(ReviewValueError, match="ReviewSeverity"):
        ReviewFindingV1(severity=True, description="x", file=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "description",
    [123, None, True, [], {}],
)
def test_finding_description_wrong_type_rejected(description: object) -> None:
    with pytest.raises(ReviewValueError, match="description"):
        ReviewFindingV1(
            severity=ReviewSeverity.MINOR,
            description=description,  # type: ignore[arg-type]
            file=None,
        )


def test_finding_description_empty_rejected() -> None:
    with pytest.raises(ReviewValueError, match="empty"):
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description="", file=None)


def test_finding_description_max_bytes_accepted() -> None:
    text = "a" * 4000
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description=text, file=None)
    assert len(finding.description.encode("utf-8")) == 4000


def test_finding_description_over_max_bytes_rejected() -> None:
    text = "a" * 4001
    with pytest.raises(ReviewValueError, match="4000"):
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description=text, file=None)


def test_finding_description_multibyte_over_limit_rejected() -> None:
    # Each "é" is 2 UTF-8 bytes: 2001 chars -> 4002 bytes, over the 4000 cap.
    text = "é" * 2001
    with pytest.raises(ReviewValueError, match="4000"):
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description=text, file=None)


@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\x1f", "\x7f", "\x9f"])
def test_finding_description_control_chars_rejected(bad_char: str) -> None:
    with pytest.raises(ReviewValueError, match="control"):
        ReviewFindingV1(
            severity=ReviewSeverity.MINOR, description=f"bad{bad_char}text", file=None
        )


def test_finding_description_ordinary_space_accepted() -> None:
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MINOR, description="two words", file=None
    )
    assert finding.description == "two words"


def test_finding_description_not_stripped_or_normalized() -> None:
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MINOR, description="  spaced  ", file=None
    )
    assert finding.description == "  spaced  "


@pytest.mark.parametrize(
    "bad_file",
    [
        "",
        "/abs/path.py",
        "a\\b.py",
        "a/../b.py",
        "./a.py",
        "../a.py",
        "a/./b.py",
        "a//b.py",
        "a/b/",
        "/a",
        "a\x00b.py",
        "a\x01b.py",
        "a b.py",  # ordinary space is a Unicode separator for `file`
        "a⁄b.py",  # fraction slash confusable
        "a․b",  # one dot leader confusable
    ],
)
def test_finding_file_invalid_rejected(bad_file: str) -> None:
    with pytest.raises(ReviewValueError):
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file=bad_file)


def test_finding_file_zero_width_space_outside_stdlib_confusable_scope() -> None:
    # U+200B ZERO WIDTH SPACE is category Cf (format), not Zs/Zl/Zp and not in
    # the explicit confusable blocklist: it passes this V1 contract's
    # stdlib-only confusable check. Documented limitation, not a guarantee.
    tricky = "a​b.py"
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file=tricky)
    assert finding.file == tricky


def test_finding_file_over_max_bytes_rejected() -> None:
    long_file = "d" * 513
    with pytest.raises(ReviewValueError, match="512"):
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file=long_file)


def test_finding_file_max_bytes_accepted() -> None:
    long_file = "d" * 512
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file=long_file)
    assert finding.file == long_file


def test_finding_file_preserves_accepted_spelling() -> None:
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MINOR, description="x", file="src/Weird_Name-1.py"
    )
    assert finding.file == "src/Weird_Name-1.py"


def test_finding_repr_redacts_description() -> None:
    secret = "s3cr3t-do-not-leak-" * 20
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description=secret, file=None)
    text = repr(finding)
    assert secret not in text
    assert "redacted" in text


def test_finding_fingerprint_deterministic() -> None:
    a = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file="f.py")
    b = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file="f.py")
    assert a.fingerprint == b.fingerprint
    assert len(a.fingerprint) == 64
    assert all(c in "0123456789abcdef" for c in a.fingerprint)


def test_finding_fingerprint_distinguishes_fields() -> None:
    base = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file="f.py")
    other_severity = ReviewFindingV1(severity=ReviewSeverity.MINOR, description="x", file="f.py")
    other_description = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="y", file="f.py")
    other_file = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file="g.py")
    no_file = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file=None)
    fingerprints = {
        base.fingerprint,
        other_severity.fingerprint,
        other_description.fingerprint,
        other_file.fingerprint,
        no_file.fingerprint,
    }
    assert len(fingerprints) == 5


def test_finding_fingerprint_not_settable() -> None:
    finding = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file=None)
    # CPython 3.11 may surface TypeError for assignment to a computed property
    # on a frozen, slotted dataclass. Both outcomes prove the value is sealed.
    with pytest.raises((AttributeError, TypeError)):
        finding.fingerprint = "0" * 64  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ports.review: ReviewDocumentV1
# ---------------------------------------------------------------------------


def test_document_valid_empty_findings() -> None:
    doc = ReviewDocumentV1(
        schema_version=1, base_sha=BASE, candidate_sha=CANDIDATE, diff_sha256=DIFF, findings=()
    )
    assert doc.findings == ()


def test_document_findings_frozen_to_tuple() -> None:
    finding = ReviewFindingV1(severity=ReviewSeverity.MAJOR, description="x", file=None)
    doc = ReviewDocumentV1(
        schema_version=1,
        base_sha=BASE,
        candidate_sha=CANDIDATE,
        diff_sha256=DIFF,
        findings=[finding],  # type: ignore[arg-type]
    )
    assert isinstance(doc.findings, tuple)
    assert doc.findings == (finding,)


def test_document_is_frozen() -> None:
    doc = ReviewDocumentV1(
        schema_version=1, base_sha=BASE, candidate_sha=CANDIDATE, diff_sha256=DIFF, findings=()
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.schema_version = 2  # type: ignore[misc]


@pytest.mark.parametrize("bad_version", [0, 2, "1", True, 1.0])
def test_document_bad_schema_version_rejected(bad_version: object) -> None:
    with pytest.raises(ReviewValueError):
        ReviewDocumentV1(
            schema_version=bad_version,  # type: ignore[arg-type]
            base_sha=BASE,
            candidate_sha=CANDIDATE,
            diff_sha256=DIFF,
            findings=(),
        )


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("base_sha", "A" * 40),  # uppercase
        ("base_sha", "a" * 39),
        ("base_sha", "a" * 41),
        ("base_sha", "g" * 40),  # non-hex
        ("candidate_sha", "B" * 40),  # uppercase
        ("candidate_sha", "b" * 39),  # too short
        ("diff_sha256", "c" * 63),
        ("diff_sha256", "c" * 65),
        ("diff_sha256", "C" * 64),
    ],
)
def test_document_bad_sha_rejected(field: str, bad_value: str) -> None:
    kwargs: dict[str, object] = {
        "schema_version": 1,
        "base_sha": BASE,
        "candidate_sha": CANDIDATE,
        "diff_sha256": DIFF,
        "findings": (),
    }
    kwargs[field] = bad_value
    with pytest.raises(ReviewValueError):
        ReviewDocumentV1(**kwargs)  # type: ignore[arg-type]


def test_document_findings_wrong_element_type_rejected() -> None:
    with pytest.raises(ReviewValueError, match="ReviewFindingV1"):
        ReviewDocumentV1(
            schema_version=1,
            base_sha=BASE,
            candidate_sha=CANDIDATE,
            diff_sha256=DIFF,
            findings=("not-a-finding",),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# protocol.review: valid documents
# ---------------------------------------------------------------------------


def test_parse_valid_empty_findings() -> None:
    doc = _parse(json.dumps(_doc()))
    assert doc.findings == ()
    assert doc.schema_version == 1


def test_parse_valid_multiple_findings() -> None:
    payload = _doc(
        findings=[
            {"severity": "BLOCKER", "description": "d1", "file": "a.py"},
            {"severity": "MINOR", "description": "d2", "file": None},
        ]
    )
    doc = _parse(json.dumps(payload))
    assert len(doc.findings) == 2
    assert doc.findings[0].severity == ReviewSeverity.BLOCKER
    assert doc.findings[1].file is None
    assert doc.findings[0].fingerprint != doc.findings[1].fingerprint


def test_parse_preserves_order_and_duplicates() -> None:
    payload = _doc(
        findings=[
            {"severity": "MAJOR", "description": "same", "file": None},
            {"severity": "MAJOR", "description": "same", "file": None},
        ]
    )
    doc = _parse(json.dumps(payload))
    assert len(doc.findings) == 2
    assert doc.findings[0].fingerprint == doc.findings[1].fingerprint


# ---------------------------------------------------------------------------
# protocol.review: schema / type / shape rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("not json at all", "not valid JSON"),
        ("```json\n{}\n```", "not valid JSON"),
        ("prefix {}", "not valid JSON"),
        ("{} suffix", "not valid JSON"),
        ("{}{}", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('"just a string"', "must be a JSON object"),
        ("null", "must be a JSON object"),
        ("true", "must be a JSON object"),
        ("false", "must be a JSON object"),
        ("1", "must be a JSON object"),
        ("NaN", "disallowed JSON constant"),
    ],
)
def test_parse_shape_rejections(text: str, match: str) -> None:
    with pytest.raises(SpecError, match=match):
        _parse(text)


def test_parse_nan_in_field_rejected() -> None:
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "' + CANDIDATE
        + '", "diff_sha256": "' + DIFF + '", "findings": [NaN]}'
    )
    with pytest.raises(SpecError, match="disallowed JSON constant"):
        _parse(text)


def test_parse_infinity_rejected() -> None:
    text = (
        '{"schema_version": Infinity, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF + '", "findings": []}'
    )
    with pytest.raises(SpecError, match="disallowed JSON constant"):
        _parse(text)


def test_parse_duplicate_top_level_key_rejected() -> None:
    text = (
        '{"schema_version": 1, "schema_version": 1, "base_sha": "' + BASE
        + '", "candidate_sha": "' + CANDIDATE + '", "diff_sha256": "' + DIFF
        + '", "findings": []}'
    )
    with pytest.raises(SpecError, match="duplicate key"):
        _parse(text)


def test_parse_duplicate_finding_key_rejected() -> None:
    payload_text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "' + CANDIDATE
        + '", "diff_sha256": "' + DIFF
        + '", "findings": [{"severity": "MAJOR", "severity": "MINOR", '
        + '"description": "d", "file": null}]}'
    )
    with pytest.raises(SpecError, match="duplicate key"):
        _parse(payload_text)


@pytest.mark.parametrize(
    "missing_key", ["schema_version", "base_sha", "candidate_sha", "diff_sha256", "findings"]
)
def test_parse_missing_top_level_key_rejected(missing_key: str) -> None:
    payload = _doc()
    del payload[missing_key]
    with pytest.raises(SpecError, match="do not match"):
        _parse(json.dumps(payload))


def test_parse_extra_top_level_key_rejected() -> None:
    payload = _doc()
    payload["extra"] = True
    with pytest.raises(SpecError, match="do not match"):
        _parse(json.dumps(payload))


@pytest.mark.parametrize("bad_version", [2, "1", True, 1.0, None])
def test_parse_wrong_schema_version_rejected(bad_version: object) -> None:
    payload = _doc(schema_version=bad_version)
    with pytest.raises(SpecError, match="schema_version"):
        _parse(json.dumps(payload))


@pytest.mark.parametrize("field", ["base_sha", "candidate_sha", "diff_sha256"])
@pytest.mark.parametrize("bad_value", [123, None, True, [], {}])
def test_parse_sha_wrong_type_rejected(field: str, bad_value: object) -> None:
    payload = _doc(**{field: bad_value})
    with pytest.raises(SpecError):
        _parse(json.dumps(payload))


def test_parse_base_sha_mismatch_rejected() -> None:
    payload = _doc(base_sha="d" * 40)
    with pytest.raises(SpecError, match="base_sha does not match"):
        _parse(json.dumps(payload))


def test_parse_candidate_sha_mismatch_rejected() -> None:
    payload = _doc(candidate_sha="d" * 40)
    with pytest.raises(SpecError, match="candidate_sha does not match"):
        _parse(json.dumps(payload))


def test_parse_diff_sha256_mismatch_rejected() -> None:
    payload = _doc(diff_sha256="d" * 64)
    with pytest.raises(SpecError, match="diff_sha256 does not match"):
        _parse(json.dumps(payload))


def test_parse_sha_case_mismatch_rejected() -> None:
    payload = _doc(base_sha=BASE.upper())
    with pytest.raises(SpecError):
        _parse(json.dumps(payload))


@pytest.mark.parametrize("field", ["expected_base_sha", "expected_candidate_sha"])
def test_parse_expected_sha_wrong_length_rejected(field: str) -> None:
    with pytest.raises(SpecError):
        _parse(json.dumps(_doc()), **{field: "a" * 39})  # type: ignore[arg-type]


def test_parse_expected_diff_sha256_wrong_length_rejected() -> None:
    with pytest.raises(SpecError):
        _parse(json.dumps(_doc()), expected_diff_sha256="c" * 63)


def test_parse_findings_not_a_list_rejected() -> None:
    payload = _doc(findings={"a": 1})
    with pytest.raises(SpecError, match="must be a JSON array"):
        _parse(json.dumps(payload))


def test_parse_findings_over_max_count_rejected() -> None:
    payload = _doc(
        findings=[{"severity": "MINOR", "description": "d", "file": None} for _ in range(101)]
    )
    with pytest.raises(SpecError, match="max_findings"):
        _parse(json.dumps(payload))


def test_parse_findings_exactly_max_count_accepted() -> None:
    payload = _doc(
        findings=[{"severity": "MINOR", "description": "d", "file": None} for _ in range(100)]
    )
    doc = _parse(json.dumps(payload))
    assert len(doc.findings) == 100


def test_parse_findings_custom_max_count_enforced() -> None:
    payload = _doc(
        findings=[{"severity": "MINOR", "description": "d", "file": None} for _ in range(3)]
    )
    with pytest.raises(SpecError, match="max_findings"):
        _parse(json.dumps(payload), max_findings=2)


@pytest.mark.parametrize(
    "missing_key", ["severity", "description", "file"]
)
def test_parse_finding_missing_key_rejected(missing_key: str) -> None:
    finding: dict[str, object] = {"severity": "MAJOR", "description": "d", "file": None}
    del finding[missing_key]
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="do not match"):
        _parse(json.dumps(payload))


def test_parse_finding_extra_key_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "d", "file": None, "extra": 1}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="do not match"):
        _parse(json.dumps(payload))


def test_parse_finding_not_object_rejected() -> None:
    payload = _doc(findings=["not-an-object"])
    with pytest.raises(SpecError, match="must be a JSON object"):
        _parse(json.dumps(payload))


def test_parse_finding_unknown_severity_rejected() -> None:
    finding = {"severity": "CRITICAL", "description": "d", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="unknown value"):
        _parse(json.dumps(payload))


def test_parse_finding_severity_lowercase_rejected() -> None:
    finding = {"severity": "major", "description": "d", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="unknown value"):
        _parse(json.dumps(payload))


def test_parse_finding_severity_wrong_type_rejected() -> None:
    finding = {"severity": 1, "description": "d", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="severity"):
        _parse(json.dumps(payload))


def test_parse_finding_description_missing_type_rejected() -> None:
    finding = {"severity": "MAJOR", "description": 123, "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="description"):
        _parse(json.dumps(payload))


def test_parse_finding_description_empty_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="empty"):
        _parse(json.dumps(payload))


def test_parse_finding_file_wrong_type_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "d", "file": 5}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="file"):
        _parse(json.dumps(payload))


def test_parse_finding_file_absolute_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "d", "file": "/abs.py"}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError):
        _parse(json.dumps(payload))


def test_parse_finding_file_dotdot_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "d", "file": "a/../b.py"}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError):
        _parse(json.dumps(payload))


def test_parse_no_default_severity_or_file() -> None:
    # A finding missing "file" must fail closed, never default to null.
    finding = {"severity": "MAJOR", "description": "d"}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="do not match"):
        _parse(json.dumps(payload))


def test_parse_document_bytes_over_limit_rejected() -> None:
    payload = _doc(
        findings=[{"severity": "MINOR", "description": "d" * 100, "file": None}]
    )
    text = json.dumps(payload)
    with pytest.raises(SpecError, match="exceeds"):
        _parse(text, max_document_bytes=len(text.encode("utf-8")) - 1)


def test_parse_document_bytes_at_limit_accepted() -> None:
    text = json.dumps(_doc())
    doc = _parse(text, max_document_bytes=len(text.encode("utf-8")))
    assert doc.schema_version == 1


@pytest.mark.parametrize("bad_bound", [0, -1, True, "10", 1.5, None])
def test_parse_max_document_bytes_bound_rejected(bad_bound: object) -> None:
    with pytest.raises(SpecError, match="max_document_bytes"):
        parse_review_document_v1(
            json.dumps(_doc()),
            expected_base_sha=BASE,
            expected_candidate_sha=CANDIDATE,
            expected_diff_sha256=DIFF,
            max_document_bytes=bad_bound,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad_bound", [0, -1, True, "10", 1.5, None])
def test_parse_max_findings_bound_rejected(bad_bound: object) -> None:
    with pytest.raises(SpecError, match="max_findings"):
        parse_review_document_v1(
            json.dumps(_doc()),
            expected_base_sha=BASE,
            expected_candidate_sha=CANDIDATE,
            expected_diff_sha256=DIFF,
            max_findings=bad_bound,  # type: ignore[arg-type]
        )


def test_parse_control_char_in_description_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "bad\x00text", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="control"):
        _parse(json.dumps(payload))


def test_parse_unicode_separator_in_file_rejected() -> None:
    finding = {"severity": "MAJOR", "description": "d", "file": "a b.py"}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError):
        _parse(json.dumps(payload))


def test_parse_error_never_echoes_full_description() -> None:
    secret = "SECRET-PAYLOAD-" * 50
    finding = {"severity": "NOPE", "description": secret, "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError) as excinfo:
        _parse(json.dumps(payload))
    assert secret not in str(excinfo.value)


def test_parse_error_never_echoes_full_document_bytes() -> None:
    text = "x" * 500_000  # not valid JSON, and larger than the default byte cap
    with pytest.raises(SpecError) as excinfo:
        _parse(text)
    assert text not in str(excinfo.value)


# ---------------------------------------------------------------------------
# error-channel hygiene: str(error), cause chain / traceback, and repr must
# never carry provider-controlled bytes; boundary errors normalize to
# SpecError / ReviewValueError with constant per-category messages.
# ---------------------------------------------------------------------------


def _chain_text(exc: BaseException) -> str:
    """The full rendered exception chain, as an operator's log would show it."""
    return "".join(traceback.format_exception(exc))


def test_parse_deeply_nested_array_normalized_to_spec_error() -> None:
    # ~100 KB, under the default byte cap, but far past any recursion limit:
    # must surface as a constant SpecError, never a raw RecursionError.
    text = "[" * 50_000 + "]" * 50_000
    with pytest.raises(SpecError, match="deeply nested"):
        _parse(text)


def test_parse_deeply_nested_findings_value_normalized_to_spec_error() -> None:
    deep = "[" * 50_000 + "]" * 50_000
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF + '", "findings": ' + deep + "}"
    )
    with pytest.raises(SpecError, match="deeply nested"):
        _parse(text)


def test_finding_description_unpaired_surrogate_normalized() -> None:
    bad = "x\ud800y"
    with pytest.raises(ReviewValueError, match="surrogate") as excinfo:
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description=bad, file=None)
    chain = _chain_text(excinfo.value)
    assert "\ud800" not in chain
    assert "ud800" not in chain


def test_finding_file_unpaired_surrogate_normalized() -> None:
    bad = "a\ud800b.py"
    with pytest.raises(ReviewValueError, match="surrogate") as excinfo:
        ReviewFindingV1(severity=ReviewSeverity.MINOR, description="d", file=bad)
    chain = _chain_text(excinfo.value)
    assert "\ud800" not in chain
    assert "ud800" not in chain


def test_parse_escaped_surrogate_in_description_rejected_without_echo() -> None:
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF
        + '", "findings": [{"severity": "MAJOR", "description": "x\\ud800y", '
        + '"file": null}]}'
    )
    with pytest.raises(SpecError, match="surrogate") as excinfo:
        _parse(text)
    chain = _chain_text(excinfo.value)
    assert "\ud800" not in chain
    assert "ud800" not in chain


def test_parse_escaped_surrogate_in_file_rejected_without_echo() -> None:
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF
        + '", "findings": [{"severity": "MAJOR", "description": "d", '
        + '"file": "a\\ud800b.py"}]}'
    )
    with pytest.raises(SpecError, match="surrogate") as excinfo:
        _parse(text)
    chain = _chain_text(excinfo.value)
    assert "\ud800" not in chain
    assert "ud800" not in chain


def test_parse_raw_surrogate_in_text_constant_message() -> None:
    bad = '{"x": "\ud800"}'
    with pytest.raises(SpecError, match="not valid UTF-8") as excinfo:
        _parse(bad)
    chain = _chain_text(excinfo.value)
    assert "\ud800" not in chain
    assert "ud800" not in chain


def test_parse_duplicate_key_error_does_not_echo_key() -> None:
    secret = "SECRET-DUPLICATE-KEY"
    text = '{"' + secret + '": 1, "' + secret + '": 2}'
    with pytest.raises(SpecError, match="duplicate key") as excinfo:
        _parse(text)
    assert secret not in _chain_text(excinfo.value)


def test_parse_extra_top_level_key_error_does_not_echo_key() -> None:
    secret = "SECRET-EXTRA-KEY"
    payload = _doc()
    payload[secret] = 1
    with pytest.raises(SpecError, match="do not match") as excinfo:
        _parse(json.dumps(payload))
    assert secret not in _chain_text(excinfo.value)


def test_parse_extra_finding_key_error_does_not_echo_key() -> None:
    secret = "SECRET-FINDING-KEY"
    finding = {"severity": "MAJOR", "description": "d", "file": None, secret: 1}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="do not match") as excinfo:
        _parse(json.dumps(payload))
    assert secret not in _chain_text(excinfo.value)


def test_parse_unknown_severity_error_does_not_echo_value() -> None:
    secret = "SECRET-SEVERITY-TOKEN"
    finding = {"severity": secret, "description": "d", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError, match="unknown value") as excinfo:
        _parse(json.dumps(payload))
    # Includes the suppressed-cause channel: the ValueError raised by the
    # ReviewSeverity constructor spells out the raw token.
    assert secret not in _chain_text(excinfo.value)


def test_parse_unsupported_schema_version_does_not_echo_value() -> None:
    # An arbitrary-precision int is a data channel: its digits must not be
    # echoed anywhere in the rendered chain.
    marker = int("31337" * 8)
    payload = _doc(schema_version=marker)
    with pytest.raises(SpecError, match="schema_version") as excinfo:
        _parse(json.dumps(payload))
    assert str(marker) not in _chain_text(excinfo.value)


def test_document_schema_version_error_does_not_echo_value() -> None:
    marker = int("31337" * 8)
    with pytest.raises(ReviewValueError, match="schema_version") as excinfo:
        ReviewDocumentV1(
            schema_version=marker,
            base_sha=BASE,
            candidate_sha=CANDIDATE,
            diff_sha256=DIFF,
            findings=(),
        )
    assert str(marker) not in _chain_text(excinfo.value)


def test_parse_invalid_file_error_does_not_echo_value() -> None:
    secret_file = "/SECRET-ABSOLUTE-PATH.py"
    finding = {"severity": "MAJOR", "description": "d", "file": secret_file}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError) as excinfo:
        _parse(json.dumps(payload))
    assert "SECRET-ABSOLUTE-PATH" not in _chain_text(excinfo.value)


def test_parse_invalid_json_error_is_constant() -> None:
    secret = "SECRETRAWCONTENT"
    text = '{"a": ' + secret + "}"
    with pytest.raises(SpecError, match="not valid JSON") as excinfo:
        _parse(text)
    assert secret not in _chain_text(excinfo.value)


def test_finding_repr_redacts_file() -> None:
    secret_file = "src/SECRET-FILE-NAME.py"
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MINOR, description="d", file=secret_file
    )
    text = repr(finding)
    assert "SECRET-FILE-NAME" not in text
    assert "redacted" in text


def test_finding_repr_keeps_none_file_visible() -> None:
    finding = ReviewFindingV1(severity=ReviewSeverity.MINOR, description="d", file=None)
    assert "file=None" in repr(finding)


def test_document_repr_redacts_finding_content() -> None:
    secret_description = "SECRET-DESCRIPTION-TEXT"
    secret_file = "src/SECRET-FILE-NAME.py"
    finding = ReviewFindingV1(
        severity=ReviewSeverity.MAJOR, description=secret_description, file=secret_file
    )
    doc = ReviewDocumentV1(
        schema_version=1,
        base_sha=BASE,
        candidate_sha=CANDIDATE,
        diff_sha256=DIFF,
        findings=(finding,),
    )
    text = repr(doc)
    assert secret_description not in text
    assert "SECRET-FILE-NAME" not in text
    assert "1 findings" in text


# ---------------------------------------------------------------------------
# exception-chain regression: cycle-safe traversal of __cause__ and
# __context__ chains, with inspection of sensitive exception attributes
# (_-_doc, .object, .args) to ensure no raw provider-controlled bytes leak
# ---------------------------------------------------------------------------


def _walk_exception_chain(
    exc: BaseException, *, visited: set[int] | None = None
) -> list[BaseException]:
    """Recursively walk both __cause__ and __context__ chains, cycle-safe.

    Returns all reachable exceptions in the chain. Each exception's identity
    (id()) is tracked to detect and break cycles.
    """
    if visited is None:
        visited = set()
    if id(exc) in visited:
        return []
    visited.add(id(exc))

    result = [exc]
    if exc.__cause__ is not None:
        result.extend(_walk_exception_chain(exc.__cause__, visited=visited))
    if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
        result.extend(_walk_exception_chain(exc.__context__, visited=visited))
    return result


def _check_no_secret_in_exception_chain(
    exc: BaseException, *, secret: str, description: str = ""
) -> None:
    """Assert that a secret is not reachable through exception chain or attributes.

    Checks:
    - Rendered exception chain text
    - Exception .args tuple
    - JSONDecodeError .doc attribute (if present)
    - UnicodeEncodeError .object attribute (if present)
    """
    # Check rendered chain
    chain = _walk_exception_chain(exc)
    rendered = _chain_text(exc)
    assert secret not in rendered, (
        f"Secret found in rendered chain: {description}"
    )

    # Check each exception in the chain for raw attributes
    for e in chain:
        # Check .args tuple
        for arg in e.args:
            if isinstance(arg, str):
                assert secret not in arg, (
                    f"Secret found in {type(e).__name__}.args: {description}"
                )

        # Check .doc attribute (JSONDecodeError)
        if hasattr(e, "doc") and isinstance(e.doc, str):
            assert secret not in e.doc, (
                f"Secret found in {type(e).__name__}.doc: {description}"
            )

        # Check .object attribute (UnicodeEncodeError, ValueError)
        if hasattr(e, "object") and isinstance(e.object, str):
            assert secret not in e.object, (
                f"Secret found in {type(e).__name__}.object: {description}"
            )


def test_parse_invalid_json_with_synthetic_credential_not_leaked() -> None:
    """Regression: invalid JSON containing a raw credential doesn't leak."""
    secret = "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"
    text = '{"a": ' + secret + "}"
    with pytest.raises(SpecError) as excinfo:
        _parse(text)
    _check_no_secret_in_exception_chain(
        excinfo.value, secret=secret, description="invalid JSON with embedded credential"
    )


def test_parse_unknown_severity_token_not_leaked() -> None:
    """Regression: unknown severity token doesn't leak through exception chain."""
    secret = "SECRET-SEVERITY-TOKEN-FROM-PROVIDER"
    finding = {"severity": secret, "description": "d", "file": None}
    payload = _doc(findings=[finding])
    with pytest.raises(SpecError) as excinfo:
        _parse(json.dumps(payload))
    _check_no_secret_in_exception_chain(
        excinfo.value, secret=secret, description="unknown severity token"
    )


def test_parse_escaped_surrogate_in_description_not_leaked_via_chain() -> None:
    """Regression: escaped unpaired surrogate in description doesn't leak."""
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF
        + '", "findings": [{"severity": "MAJOR", "description": "x\\ud800y", '
        + '"file": null}]}'
    )
    with pytest.raises(SpecError) as excinfo:
        _parse(text)
    _check_no_secret_in_exception_chain(
        excinfo.value, secret="ud800", description="escaped surrogate in description"
    )


def test_parse_escaped_surrogate_in_file_not_leaked_via_chain() -> None:
    """Regression: escaped unpaired surrogate in file doesn't leak."""
    text = (
        '{"schema_version": 1, "base_sha": "' + BASE + '", "candidate_sha": "'
        + CANDIDATE + '", "diff_sha256": "' + DIFF
        + '", "findings": [{"severity": "MAJOR", "description": "d", '
        + '"file": "a\\ud800b.py"}]}'
    )
    with pytest.raises(SpecError) as excinfo:
        _parse(text)
    _check_no_secret_in_exception_chain(
        excinfo.value, secret="ud800", description="escaped surrogate in file"
    )


def test_parse_invalid_json_with_credential_and_surrogate_not_leaked() -> None:
    """Regression: combined malformed JSON + secrets/surrogates don't leak."""
    secret = "api_key=super-secret-value"
    # Malformed JSON with embedded secret that won't parse
    text = '{"data": "' + secret + '\\ud800incomplete'
    with pytest.raises(SpecError) as excinfo:
        _parse(text)
    _check_no_secret_in_exception_chain(
        excinfo.value, secret=secret, description="malformed JSON with credential"
    )
    # Also verify the surrogate sequence doesn't appear
    chain = _chain_text(excinfo.value)
    assert "ud800" not in chain
