"""V1 structured review-document value contract.

Held slice for R1-F0: pure, immutable value types and their construction-time
validation for a versioned reviewer-output document. This module performs no
I/O, does not parse JSON (see ``dagvane.protocol.review`` for boundary
parsing), and is not wired into any run/backend/CLI integration.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "REVIEW_SCHEMA_VERSION",
    "ReviewValueError",
    "ReviewSeverity",
    "ReviewFindingV1",
    "ReviewDocumentV1",
    "validate_git_sha",
    "validate_sha256",
]

REVIEW_SCHEMA_VERSION = 1


class ReviewValueError(ValueError):
    """A V1 review value failed construction-time validation."""


class ReviewSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    OPTIONAL = "OPTIONAL"


# C0 controls (incl. NUL) plus DEL and C1 controls. Ordinary space (0x20) is
# outside both ranges, so it is never rejected by this check alone.
_CONTROL_RANGES = ((0x00, 0x1F), (0x7F, 0x9F))


def _has_control_char(text: str) -> bool:
    return any(
        lo <= ord(ch) <= hi for ch in text for lo, hi in _CONTROL_RANGES
    )


_GIT_SHA_HEX_LEN = 40
_SHA256_HEX_LEN = 64
_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_lower_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in _LOWER_HEX_DIGITS for c in value)


def validate_git_sha(value: object, *, ctx: str) -> str:
    """Validate a lowercase 40-hex Git commit SHA. Returns it unchanged."""
    if not isinstance(value, str):
        raise ReviewValueError(f"{ctx}: must be a string")
    if not _is_lower_hex(value, _GIT_SHA_HEX_LEN):
        raise ReviewValueError(f"{ctx}: must be a lowercase 40-hex Git SHA")
    return value


def validate_sha256(value: object, *, ctx: str) -> str:
    """Validate a lowercase 64-hex SHA-256 digest. Returns it unchanged."""
    if not isinstance(value, str):
        raise ReviewValueError(f"{ctx}: must be a string")
    if not _is_lower_hex(value, _SHA256_HEX_LEN):
        raise ReviewValueError(f"{ctx}: must be a lowercase 64-hex SHA-256")
    return value


_MAX_DESCRIPTION_BYTES = 4000
_MAX_FILE_BYTES = 512

# Unicode General Category groups treated as separators for the `file` field:
# Zs (space separator, includes ordinary U+0020), Zl (line), Zp (paragraph).
_SEPARATOR_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})

# Pragmatic, stdlib-only blocklist of characters commonly used as visual
# stand-ins for the ASCII path separators '/' '\' '.' in lookalike /
# path-traversal attempts. Not a full UTS #39 confusables table (unavailable
# in the standard library).
_CONFUSABLE_CHARS = frozenset(
    "⁄∕／⧸╱＼∖"  # slash / backslash lookalikes
    "．․。﹒"  # full-stop lookalikes
)


def _validate_description(value: object) -> None:
    if not isinstance(value, str):
        raise ReviewValueError(f"description: must be a string, got {type(value).__name__}")
    if value == "":
        raise ReviewValueError("description: must not be empty")
    # Classify inside the except, then raise after it exits: `from None`
    # alone only clears __cause__, but a raise still executed *inside* an
    # except block leaves __context__ pointing at the raw UnicodeEncodeError
    # (its .object attribute holds the full untrusted string).
    encoded_length: int | None = None
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        pass
    if encoded_length is None:
        raise ReviewValueError("description: contains an unpaired surrogate")
    if encoded_length > _MAX_DESCRIPTION_BYTES:
        raise ReviewValueError(f"description: exceeds {_MAX_DESCRIPTION_BYTES} UTF-8 bytes")
    if _has_control_char(value):
        raise ReviewValueError("description: contains a NUL or C0/C1 control character")


def _validate_file(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ReviewValueError(f"file: must be a string or null, got {type(value).__name__}")
    if value == "":
        raise ReviewValueError("file: must not be empty")
    encoded_length: int | None = None
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        pass
    if encoded_length is None:
        raise ReviewValueError("file: contains an unpaired surrogate")
    if encoded_length > _MAX_FILE_BYTES:
        raise ReviewValueError(f"file: exceeds {_MAX_FILE_BYTES} UTF-8 bytes")
    if _has_control_char(value):
        raise ReviewValueError("file: contains a NUL or control character")
    if value.startswith("/"):
        raise ReviewValueError("file: must not be an absolute path")
    if "\\" in value:
        raise ReviewValueError("file: must not contain a backslash")
    for ch in value:
        if unicodedata.category(ch) in _SEPARATOR_CATEGORIES:
            raise ReviewValueError("file: must not contain a Unicode separator character")
        if ch in _CONFUSABLE_CHARS:
            raise ReviewValueError("file: must not contain a confusable separator character")
    segments = value.split("/")
    if any(segment == "" for segment in segments):
        raise ReviewValueError("file: must not contain an empty path segment")
    if any(segment in (".", "..") for segment in segments):
        raise ReviewValueError("file: must not contain a '.' or '..' path segment")


def _finding_canonical_bytes(severity: ReviewSeverity, description: str, file: str | None) -> bytes:
    canonical: dict[str, object] = {
        "severity": severity.value,
        "description": description,
        "file": file,
    }
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return text.encode("utf-8")


@dataclass(frozen=True, slots=True)
class ReviewFindingV1:
    """One immutable V1 review finding.

    ``fingerprint`` is a computed property, not a stored field: it is always
    system-derived from the validated fields and can never be supplied or
    overridden by the caller.
    """

    severity: ReviewSeverity
    description: str
    file: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.severity, ReviewSeverity):
            raise ReviewValueError(
                f"severity: must be a ReviewSeverity member, got {type(self.severity).__name__}"
            )
        _validate_description(self.description)
        _validate_file(self.file)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            _finding_canonical_bytes(self.severity, self.description, self.file)
        ).hexdigest()

    def __repr__(self) -> str:
        # Never echo the untrusted description or file: only their byte lengths.
        description_bytes = len(self.description.encode("utf-8"))
        if self.file is None:
            file_repr = "None"
        else:
            file_repr = f"<redacted, {len(self.file.encode('utf-8'))} bytes>"
        return (
            f"ReviewFindingV1(severity={self.severity.value}, "
            f"description=<redacted, {description_bytes} bytes>, file={file_repr})"
        )


@dataclass(frozen=True, slots=True)
class ReviewDocumentV1:
    """One immutable V1 review document. ``findings`` is always frozen to a tuple."""

    schema_version: int
    base_sha: str
    candidate_sha: str
    diff_sha256: str
    findings: tuple[ReviewFindingV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise ReviewValueError("schema_version: must be an integer")
        if self.schema_version != REVIEW_SCHEMA_VERSION:
            # Constant message: an arbitrary-precision int can smuggle raw data.
            raise ReviewValueError(f"schema_version: must be {REVIEW_SCHEMA_VERSION}")
        validate_git_sha(self.base_sha, ctx="base_sha")
        validate_git_sha(self.candidate_sha, ctx="candidate_sha")
        validate_sha256(self.diff_sha256, ctx="diff_sha256")
        findings: tuple[ReviewFindingV1, ...] = ()
        findings_not_iterable = False
        try:
            findings = tuple(self.findings)
        except TypeError:
            findings_not_iterable = True
        if findings_not_iterable:
            raise ReviewValueError("findings: must be an iterable of ReviewFindingV1")
        if not all(isinstance(item, ReviewFindingV1) for item in findings):
            raise ReviewValueError("findings: every element must be a ReviewFindingV1")
        object.__setattr__(self, "findings", findings)

    def __repr__(self) -> str:
        # Only validated identity fields plus a finding count: never any
        # finding content, even indirectly through element reprs.
        return (
            f"ReviewDocumentV1(schema_version={self.schema_version}, "
            f"base_sha={self.base_sha!r}, candidate_sha={self.candidate_sha!r}, "
            f"diff_sha256={self.diff_sha256!r}, "
            f"findings=<{len(self.findings)} findings>)"
        )
