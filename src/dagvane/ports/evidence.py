"""Evidence Execution Port V1: exact-SHA verification semantics as a contract.

An evidence command is a bounded, non-interactive, argv-only subprocess
invocation (never a shell string) run against one disposable, isolated view
of an exact source SHA. This module defines the closed value types and the
executor Protocol shape only — no shell, Git, or sandbox implementation, and
no orchestration/state-machine integration. It exists so that "the command
saw exactly this commit and touched nothing durable" is a checkable property
of the records themselves, not a claim a caller has to trust.

None of these types carry raw output bytes, environment values, or secrets:
output is referenced by content-addressed artifact refs, and a sandbox grant
is an opaque durable reference redacted from ``repr`` and from validation
error text (which never reflects argv, a grant reference, or an artifact
ref's provider-influenced field values back into a message).

Future capability notes (not implemented here): resource/CPU/memory metrics,
streaming/partial output, and platform-specific sandbox limits belong in a
later, compatible V2 — V1 stays a minimal, inspectable binding contract.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError

EVIDENCE_PORT_API_VERSION = 1

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

_MAX_ARGV_ELEMENTS = 256
_MAX_ARGV_ELEMENT_CHARS = 4096
_MAX_GRANT_REF_CHARS = 4096
_MAX_MEDIA_TYPE_CHARS = 255
_MAX_ROLE_CHARS = 128
_MAX_ARTIFACT_REFS = 64

# Bounded-output ceiling for V1: both a command's requested cap and any
# reported artifact size must fit under this hard limit.
EVIDENCE_MAX_OUTPUT_BYTES = 1_073_741_824  # 1 GiB


def validate_api_version(value: object) -> int:
    """Validate a claimed evidence port API version against V1 strictly.

    ``bool`` is a subtype of ``int`` in Python (``True == 1``); it is
    rejected explicitly so a boolean flag can never be silently accepted as
    a version number.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError("api_version must be an integer")
    if value != EVIDENCE_PORT_API_VERSION:
        raise SpecError(f"unsupported evidence port api_version {value}")
    return value


def _require_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{name} must be an integer")
    return value


def _require_bool(value: object, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{name} must be a boolean")
    return value


def _require_finite_float(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, float):
        raise SpecError(f"{name} must be a float")
    if not math.isfinite(value):
        raise SpecError(f"{name} must be finite")
    return value


def _require_sha(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
        raise SpecError(f"{name} must be an exact 40-character lowercase hex SHA-1")
    return value


def _validate_argv(value: object) -> tuple[str, ...]:
    """Validate an argv tuple without reflecting its contents in errors.

    Rejects anything but a nonempty ``tuple[str, ...]``: a plain string
    (which would otherwise silently become shell text), non-string
    elements, control characters, NUL bytes, and oversized containers.
    """
    if not isinstance(value, tuple):
        raise SpecError("argv must be a tuple of strings, never a shell string")
    if len(value) == 0:
        raise SpecError("argv must be nonempty")
    if len(value) > _MAX_ARGV_ELEMENTS:
        raise SpecError("argv exceeds the maximum element count")
    for element in value:
        if not isinstance(element, str):
            raise SpecError("argv elements must be strings")
        if len(element) > _MAX_ARGV_ELEMENT_CHARS:
            raise SpecError("argv element exceeds the maximum length")
        if _CONTROL_CHAR_RE.search(element):
            raise SpecError("argv element must not contain control characters or NUL")
    return value


def _validate_grant_ref(value: object) -> str:
    if not isinstance(value, str):
        raise SpecError("grant reference must be a string")
    if not value:
        raise SpecError("grant reference must be nonempty")
    if len(value) > _MAX_GRANT_REF_CHARS:
        raise SpecError("grant reference exceeds the maximum length")
    if _CONTROL_CHAR_RE.search(value):
        raise SpecError("grant reference must not contain control characters or NUL")
    return value


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EvidencePurposeV1(StrEnum):
    BASELINE = "baseline"
    ACCEPTANCE = "acceptance"
    VERIFY = "verify"


class EvidenceSandboxRequirementV1(StrEnum):
    REQUIRED = "required"
    TRUSTED_PROJECT_GRANT = "trusted_project_grant"


class EvidenceValidityV1(StrEnum):
    VALID = "valid"
    COMMAND_FAILED = "command_failed"
    EVIDENCE_INVALID = "evidence_invalid"
    CANCELLED = "cancelled"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceCommandV1:
    """One bounded, argv-only command request against an exact source SHA.

    ``wall_timeout_seconds`` is a strict ``float`` (never ``int``/``bool``)
    so ``NaN``/``inf`` are representable and explicitly rejected rather than
    silently unrepresentable. ``sandbox_requirement`` gates ``grant_ref``:
    ``REQUIRED`` forbids one, ``TRUSTED_PROJECT_GRANT`` mandates a durable
    nonempty opaque reference. This record carries no environment values or
    secrets, and its ``repr`` redacts ``argv`` and ``grant_ref``.
    """

    command_id: str
    purpose: EvidencePurposeV1
    source_sha: str
    argv: tuple[str, ...]
    wall_timeout_seconds: float
    max_output_bytes: int
    sandbox_requirement: EvidenceSandboxRequirementV1
    grant_ref: str | None = None

    def __post_init__(self) -> None:
        validate_filesystem_id(self.command_id, ctx="command_id")
        if not isinstance(self.purpose, EvidencePurposeV1):
            raise SpecError("purpose must be an EvidencePurposeV1 member")
        _require_sha(self.source_sha, name="source_sha")
        _validate_argv(self.argv)
        timeout = _require_finite_float(self.wall_timeout_seconds, name="wall_timeout_seconds")
        if timeout <= 0:
            raise SpecError("wall_timeout_seconds must be positive")
        max_bytes = _require_int(self.max_output_bytes, name="max_output_bytes")
        if max_bytes <= 0:
            raise SpecError("max_output_bytes must be positive")
        if max_bytes > EVIDENCE_MAX_OUTPUT_BYTES:
            raise SpecError("max_output_bytes exceeds the bounded output ceiling")
        if not isinstance(self.sandbox_requirement, EvidenceSandboxRequirementV1):
            raise SpecError("sandbox_requirement must be an EvidenceSandboxRequirementV1 member")
        if self.sandbox_requirement is EvidenceSandboxRequirementV1.REQUIRED:
            if self.grant_ref is not None:
                raise SpecError("REQUIRED sandbox forbids a grant reference")
        elif self.grant_ref is None:
            raise SpecError("TRUSTED_PROJECT_GRANT requires a grant reference")
        else:
            _validate_grant_ref(self.grant_ref)

    def __repr__(self) -> str:
        grant = "<redacted>" if self.grant_ref is not None else None
        return (
            f"EvidenceCommandV1(command_id={self.command_id!r}, purpose={self.purpose!r}, "
            f"source_sha={self.source_sha!r}, argv=<{len(self.argv)} args redacted>, "
            f"wall_timeout_seconds={self.wall_timeout_seconds!r}, "
            f"max_output_bytes={self.max_output_bytes!r}, "
            f"sandbox_requirement={self.sandbox_requirement!r}, grant_ref={grant})"
        )


@dataclass(frozen=True, slots=True)
class EvidenceViewV1:
    """One fresh, disposable, isolated view of an exact source SHA.

    ``command_ordinal`` identifies this view's position among the commands
    of one evidence sequence; ``validate_fresh_views`` enforces that no view
    id backs more than one report, which is what makes "one command gets
    one fresh view" a checkable property rather than a convention.
    """

    view_id: str
    source_sha: str
    disposable: bool
    command_ordinal: int

    def __post_init__(self) -> None:
        validate_filesystem_id(self.view_id, ctx="view_id")
        _require_sha(self.source_sha, name="source_sha")
        if _require_bool(self.disposable, name="disposable") is not True:
            raise SpecError("disposable must be exactly True")
        ordinal = _require_int(self.command_ordinal, name="command_ordinal")
        if ordinal < 0:
            raise SpecError("command_ordinal must be >= 0")


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceArtifactRefV1:
    """A content-addressed reference to captured output, never raw bytes.

    ``digest``, ``media_type``, and ``role`` are provider-influenced field
    values under the V1 nonreflection contract: ``repr`` redacts all three
    and shows only ``size_bytes``, and validation error text never reflects
    their values either.
    """

    digest: str
    size_bytes: int
    media_type: str
    role: str

    def __post_init__(self) -> None:
        if not isinstance(self.digest, str) or not _ARTIFACT_DIGEST_RE.fullmatch(self.digest):
            raise SpecError("artifact digest must match sha256:<64 lowercase hex>")
        size = _require_int(self.size_bytes, name="artifact size_bytes")
        if size < 0:
            raise SpecError("artifact size_bytes must be >= 0")
        if size > EVIDENCE_MAX_OUTPUT_BYTES:
            raise SpecError("artifact size_bytes exceeds the bounded output ceiling")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise SpecError("artifact media_type must be a nonempty string")
        if len(self.media_type) > _MAX_MEDIA_TYPE_CHARS:
            raise SpecError("artifact media_type exceeds the maximum length")
        if _CONTROL_CHAR_RE.search(self.media_type):
            raise SpecError("artifact media_type must not contain control characters or NUL")
        if not isinstance(self.role, str) or not self.role:
            raise SpecError("artifact role must be a nonempty string")
        if len(self.role) > _MAX_ROLE_CHARS:
            raise SpecError("artifact role exceeds the maximum length")
        if _CONTROL_CHAR_RE.search(self.role):
            raise SpecError("artifact role must not contain control characters or NUL")

    def __repr__(self) -> str:
        return (
            f"EvidenceArtifactRefV1(digest=<redacted>, size_bytes={self.size_bytes!r}, "
            f"media_type=<redacted>, role=<redacted>)"
        )


def _allowed_validities(
    *,
    exit_status: int | None,
    head_before: str,
    head_after: str,
    source_sha: str,
    tracked_or_index_mutation: bool,
    timed_out: bool,
    cancelled: bool,
    cleanup_complete: bool,
) -> frozenset[EvidenceValidityV1]:
    """The exact set of validity values a report with these fields may carry.

    Precedence: ``cleanup_complete is False`` is checked first and is
    dispositive on its own (``CLEANUP_INCOMPLETE`` only), because until the
    process tree is proven quiescent no other observation on this record —
    including a moved HEAD or a tracked/index mutation — is stable; a live
    descendant could still be writing. Only once cleanup is proven complete
    do the remaining fields determine validity: a moved HEAD or
    tracked/index mutation is dispositive (``EVIDENCE_INVALID``) regardless
    of cancellation or timeout; cancellation and timeout each map to their
    own terminal status; a clean run is ``VALID`` only on exit 0 with the
    exact same HEAD as the exact source SHA throughout.
    """
    if not cleanup_complete:
        return frozenset({EvidenceValidityV1.CLEANUP_INCOMPLETE})
    moved = head_before != source_sha or head_after != source_sha or head_before != head_after
    if moved or tracked_or_index_mutation:
        return frozenset({EvidenceValidityV1.EVIDENCE_INVALID})
    if cancelled:
        return frozenset({EvidenceValidityV1.CANCELLED})
    if timed_out:
        return frozenset({EvidenceValidityV1.COMMAND_FAILED})
    if exit_status != 0:
        return frozenset({EvidenceValidityV1.COMMAND_FAILED})
    return frozenset({EvidenceValidityV1.VALID})


@dataclass(frozen=True, slots=True, repr=False)
class EvidenceReportV1:
    """The recorded outcome of one command executed against one view.

    ``untracked_or_ignored_observed`` never invalidates evidence by itself;
    what makes it safe is structural, not a flag on this record — a view is
    single-use (see ``validate_fresh_views``), so whatever was observed here
    can never be the thing another view or candidate sees. ``cleanup_complete``
    is the proven fact that the command's process tree reached quiescence
    (every descendant stopped, nothing can still write to the view) — until
    it is ``True`` no other field's observation is trustworthy, so
    ``validity`` can only be ``CLEANUP_INCOMPLETE``. ``validity`` is
    validated against the rest of the record's fields at construction: it is
    impossible to build an inconsistent report (see ``_allowed_validities``).
    """

    command_id: str
    view_id: str
    source_sha: str
    exit_status: int | None
    head_before: str
    head_after: str
    tracked_or_index_mutation: bool
    untracked_or_ignored_observed: bool
    output_artifacts: tuple[EvidenceArtifactRefV1, ...]
    timed_out: bool
    cancelled: bool
    cleanup_complete: bool
    validity: EvidenceValidityV1

    def __post_init__(self) -> None:
        validate_filesystem_id(self.command_id, ctx="command_id")
        validate_filesystem_id(self.view_id, ctx="view_id")
        _require_sha(self.source_sha, name="source_sha")
        if self.exit_status is not None:
            status = _require_int(self.exit_status, name="exit_status")
            if status < 0:
                raise SpecError("exit_status must be >= 0")
        _require_sha(self.head_before, name="head_before")
        _require_sha(self.head_after, name="head_after")
        _require_bool(self.tracked_or_index_mutation, name="tracked_or_index_mutation")
        _require_bool(self.untracked_or_ignored_observed, name="untracked_or_ignored_observed")
        if not isinstance(self.output_artifacts, tuple):
            raise SpecError("output_artifacts must be a tuple of EvidenceArtifactRefV1")
        if len(self.output_artifacts) > _MAX_ARTIFACT_REFS:
            raise SpecError("output_artifacts exceeds the maximum reference count")
        for artifact in self.output_artifacts:
            if not isinstance(artifact, EvidenceArtifactRefV1):
                raise SpecError("output_artifacts elements must be EvidenceArtifactRefV1")
        _require_bool(self.timed_out, name="timed_out")
        _require_bool(self.cancelled, name="cancelled")
        _require_bool(self.cleanup_complete, name="cleanup_complete")
        if not isinstance(self.validity, EvidenceValidityV1):
            raise SpecError("validity must be an EvidenceValidityV1 member")
        allowed = _allowed_validities(
            exit_status=self.exit_status,
            head_before=self.head_before,
            head_after=self.head_after,
            source_sha=self.source_sha,
            tracked_or_index_mutation=self.tracked_or_index_mutation,
            timed_out=self.timed_out,
            cancelled=self.cancelled,
            cleanup_complete=self.cleanup_complete,
        )
        if self.validity not in allowed:
            raise SpecError("validity is inconsistent with the reported result matrix")

    def __repr__(self) -> str:
        return (
            f"EvidenceReportV1(command_id={self.command_id!r}, view_id={self.view_id!r}, "
            f"source_sha={self.source_sha!r}, exit_status={self.exit_status!r}, "
            f"head_before={self.head_before!r}, head_after={self.head_after!r}, "
            f"tracked_or_index_mutation={self.tracked_or_index_mutation!r}, "
            f"untracked_or_ignored_observed={self.untracked_or_ignored_observed!r}, "
            f"output_artifacts=<{len(self.output_artifacts)} refs redacted>, "
            f"timed_out={self.timed_out!r}, cancelled={self.cancelled!r}, "
            f"cleanup_complete={self.cleanup_complete!r}, validity={self.validity!r})"
        )


# ---------------------------------------------------------------------------
# Binding invariants
# ---------------------------------------------------------------------------


def validate_report_binding(
    report: EvidenceReportV1, command: EvidenceCommandV1, view: EvidenceViewV1
) -> None:
    """Raise unless ``report`` exactly binds to ``command`` and ``view``.

    Exact-SHA verification only means something if the report is provably
    tied to the one command and the one disposable view that produced it,
    and both agreed on the same exact source SHA before execution.
    """
    if command.source_sha != view.source_sha:
        raise SpecError("command and view do not share the same exact source_sha")
    if report.command_id != command.command_id:
        raise SpecError("report command_id does not match the executed command")
    if report.view_id != view.view_id:
        raise SpecError("report view_id does not match the executed view")
    if report.source_sha != command.source_sha:
        raise SpecError("report source_sha does not match the command/view binding")


def validate_fresh_views(reports: tuple[EvidenceReportV1, ...]) -> None:
    """Raise if any view id backs more than one report in ``reports``.

    A disposable view is single-use: reusing one across commands would let
    untracked/ignored bytes (or state) observed under it leak into evidence
    for a different command, which the result matrix alone cannot detect.
    """
    seen: set[str] = set()
    for report in reports:
        if report.view_id in seen:
            raise SpecError(
                f"view {report.view_id!r} backs more than one report; "
                "each command requires a fresh view"
            )
        seen.add(report.view_id)


# ---------------------------------------------------------------------------
# Executor protocol
# ---------------------------------------------------------------------------


class EvidenceExecutorV1(Protocol):
    """Executes one command against one view. No other effect surface."""

    def execute(self, command: EvidenceCommandV1, view: EvidenceViewV1) -> EvidenceReportV1: ...
