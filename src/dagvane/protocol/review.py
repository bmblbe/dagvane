"""Boundary parser for the V1 structured review document.

Held slice for R1-F0: this module only turns untrusted external text into a
validated ``dagvane.ports.review.ReviewDocumentV1``. It performs no reviewer
execution, no Git checkout checks, no routing/escalation, and no persistence,
and it is not wired into any run/backend/CLI integration.
"""

from __future__ import annotations

import json

from dagvane.domain.models import SpecError
from dagvane.ports.review import (
    REVIEW_SCHEMA_VERSION,
    ReviewDocumentV1,
    ReviewFindingV1,
    ReviewSeverity,
    ReviewValueError,
    validate_git_sha,
    validate_sha256,
)

__all__ = ["parse_review_document_v1"]

_DEFAULT_MAX_DOCUMENT_BYTES = 262_144
_DEFAULT_MAX_FINDINGS = 100

_DOCUMENT_KEYS = frozenset(
    {"schema_version", "base_sha", "candidate_sha", "diff_sha256", "findings"}
)
_FINDING_KEYS = frozenset({"severity", "description", "file"})


def _short_repr(value: object, *, limit: int = 80) -> str:
    """Bound how much of a value ever lands in an exception message.

    Used only for trusted caller-supplied configuration (the bound
    parameters); provider-controlled document content -- keys, severity
    tokens, description text, raw document bytes -- is never passed through
    this helper or any other error message.
    """
    text = repr(value)
    if len(text) > limit:
        return f"{text[:limit]}...(truncated, {len(text)} chars)"
    return text


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            # Constant message: the key name itself is provider-controlled.
            raise SpecError("review document has a duplicate key")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> float:
    raise SpecError(f"review document contains a disallowed JSON constant: {name}")


def _positive_int_bound(value: object, *, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecError(f"{name} must be a positive integer, got {_short_repr(value)}")


def parse_review_document_v1(
    text: str,
    *,
    expected_base_sha: str,
    expected_candidate_sha: str,
    expected_diff_sha256: str,
    max_document_bytes: int = _DEFAULT_MAX_DOCUMENT_BYTES,
    max_findings: int = _DEFAULT_MAX_FINDINGS,
) -> ReviewDocumentV1:
    """Strictly parse one UTF-8 JSON review document against a closed V1 schema.

    Any malformed input -- wrong shape, unknown/missing/extra keys, wrong
    types, coercible-but-wrong values, or a value mismatched against the
    three explicit expected identities -- raises ``SpecError``. Disposition
    (whether findings are acceptable) is left entirely to the caller/judge.
    """
    _positive_int_bound(max_document_bytes, name="max_document_bytes")
    _positive_int_bound(max_findings, name="max_findings")

    # Classify inside each except, then raise only after it exits: a raise
    # executed *inside* an except block still sets __context__ to the caught
    # exception even with `from None`/no chain, and several of the raw
    # exceptions here carry untrusted bytes on an attribute (JSONDecodeError
    # .doc, UnicodeEncodeError .object) that a structured exception collector
    # could read even though the rendered message/traceback stays constant.
    expected_sha_error: str | None = None
    try:
        validate_git_sha(expected_base_sha, ctx="expected_base_sha")
        validate_git_sha(expected_candidate_sha, ctx="expected_candidate_sha")
        validate_sha256(expected_diff_sha256, ctx="expected_diff_sha256")
    except ReviewValueError as exc:
        expected_sha_error = str(exc)
    if expected_sha_error is not None:
        raise SpecError(expected_sha_error)

    if not isinstance(text, str):
        raise SpecError("review document: must be a string")
    document_bytes: bytes | None = None
    try:
        document_bytes = text.encode("utf-8")
    except UnicodeEncodeError:
        pass
    if document_bytes is None:
        raise SpecError("review document is not valid UTF-8")
    if len(document_bytes) > max_document_bytes:
        raise SpecError(
            f"review document exceeds {max_document_bytes} UTF-8 bytes "
            f"({len(document_bytes)} bytes)"
        )

    json_parse_error: str | None = None
    obj: dict[str, object] | None = None
    try:
        obj = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except RecursionError:
        # Small documents can still nest arbitrarily deep; normalize instead
        # of leaking a RecursionError past this boundary.
        json_parse_error = "deep"
    except ValueError:
        # Constant message; a JSONDecodeError cause retains the full document
        # text on its ``doc`` attribute. SpecError does not derive from
        # ValueError, so hook-raised SpecErrors propagate unwrapped.
        json_parse_error = "invalid"
    if json_parse_error is not None:
        if json_parse_error == "deep":
            raise SpecError("review document is too deeply nested")
        else:
            raise SpecError("review document is not valid JSON")
    if not isinstance(obj, dict):
        raise SpecError("review document must be a JSON object")
    if set(obj.keys()) != _DOCUMENT_KEYS:
        # Never echo the provider-controlled key names, only the required set.
        raise SpecError(
            f"review document keys do not match {sorted(_DOCUMENT_KEYS)!r}"
        )

    schema_version = obj["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise SpecError("review document: schema_version must be an integer")
    if schema_version != REVIEW_SCHEMA_VERSION:
        # Constant message: an arbitrary-precision int can smuggle raw data.
        raise SpecError("review document: unsupported schema_version")

    base_sha = obj["base_sha"]
    candidate_sha = obj["candidate_sha"]
    diff_sha256 = obj["diff_sha256"]
    sha_validation_error: str | None = None
    try:
        validate_git_sha(base_sha, ctx="base_sha")
        validate_git_sha(candidate_sha, ctx="candidate_sha")
        validate_sha256(diff_sha256, ctx="diff_sha256")
    except ReviewValueError as exc:
        sha_validation_error = str(exc)
    if sha_validation_error is not None:
        raise SpecError(sha_validation_error)

    if base_sha != expected_base_sha:
        raise SpecError("review document: base_sha does not match the expected base SHA")
    if candidate_sha != expected_candidate_sha:
        raise SpecError(
            "review document: candidate_sha does not match the expected candidate SHA"
        )
    if diff_sha256 != expected_diff_sha256:
        raise SpecError(
            "review document: diff_sha256 does not match the expected diff SHA-256"
        )

    raw_findings = obj["findings"]
    if not isinstance(raw_findings, list):
        raise SpecError("review document: findings must be a JSON array")
    if len(raw_findings) > max_findings:
        raise SpecError(f"review document: findings exceeds max_findings ({max_findings})")

    findings: list[ReviewFindingV1] = []
    for index, raw_finding in enumerate(raw_findings):
        ctx = f"findings[{index}]"
        if not isinstance(raw_finding, dict):
            raise SpecError(f"review document: {ctx} must be a JSON object")
        if set(raw_finding.keys()) != _FINDING_KEYS:
            # Never echo the provider-controlled key names, only the required set.
            raise SpecError(
                f"review document: {ctx} keys do not match {sorted(_FINDING_KEYS)!r}"
            )

        raw_severity = raw_finding["severity"]
        if not isinstance(raw_severity, str) or isinstance(raw_severity, bool):
            raise SpecError(f"review document: {ctx}.severity must be a string")
        severity: ReviewSeverity | None = None
        try:
            severity = ReviewSeverity(raw_severity)
        except ValueError:
            # Constant message; the suppressed cause's args would echo the
            # provider-controlled token into any formatted traceback.
            pass
        if severity is None:
            raise SpecError(
                f"review document: {ctx}.severity has an unknown value"
            )

        description = raw_finding["description"]
        file = raw_finding["file"]
        if file is not None and not isinstance(file, str):
            raise SpecError(f"review document: {ctx}.file must be a string or null")

        finding: ReviewFindingV1 | None = None
        finding_error: str | None = None
        try:
            finding = ReviewFindingV1(severity=severity, description=description, file=file)
        except ReviewValueError as exc:
            finding_error = str(exc)
        if finding_error is not None or finding is None:
            detail = finding_error or "invalid finding"
            raise SpecError(f"review document: {ctx} is invalid: {detail}")
        findings.append(finding)

    document_error: str | None = None
    result: ReviewDocumentV1 | None = None
    try:
        result = ReviewDocumentV1(
            schema_version=schema_version,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            diff_sha256=diff_sha256,
            findings=tuple(findings),
        )
    except ReviewValueError as exc:
        document_error = str(exc)
    if document_error is not None or result is None:
        detail = document_error or "invalid document"
        raise SpecError(f"review document is invalid: {detail}")
    return result
