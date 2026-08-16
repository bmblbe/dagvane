"""Ports for streaming, secret-scrubbed process output capture.

These types are the stable contract between a byte-producing source (a
subprocess, a live model stream) and a bounded, sanitized text capture. They
carry no I/O and no scrubbing logic themselves — only the shapes an adapter
must produce and the report a caller can trust without re-deriving anything
from the adapter's internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class RetentionPolicy(StrEnum):
    HEAD = "head"
    TAIL = "tail"


def _require_positive_int(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _require_nonneg_int(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")


def _require_bool(name: str, value: bool) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a bool, got {value!r}")


@dataclass(frozen=True, slots=True)
class CaptureLimitsV1:
    """Configured ceilings for one bounded capture session.

    Strict positive-finite-integer validation: a ``bool`` is not accepted as
    an integer here, even though ``bool`` is a Python ``int`` subclass.
    """

    retained_bytes: int
    max_secret_variant_chars: int
    max_secret_variants: int

    def __post_init__(self) -> None:
        _require_positive_int("retained_bytes", self.retained_bytes)
        _require_positive_int("max_secret_variant_chars", self.max_secret_variant_chars)
        _require_positive_int("max_secret_variants", self.max_secret_variants)


@dataclass(frozen=True, slots=True)
class CaptureReportV1:
    """Exact, honest accounting for one finished capture session."""

    schema_version: int
    retention: RetentionPolicy
    limit_bytes: int
    raw_bytes_consumed: int
    sanitized_bytes_seen: int
    retained_bytes: int
    source_eof: bool
    cap_truncated: bool
    overlap_discarded: bool

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError(f"schema_version must be 1, got {self.schema_version!r}")
        if not isinstance(self.retention, RetentionPolicy):
            raise ValueError(f"retention must be a RetentionPolicy, got {self.retention!r}")
        _require_positive_int("limit_bytes", self.limit_bytes)
        _require_nonneg_int("raw_bytes_consumed", self.raw_bytes_consumed)
        _require_nonneg_int("sanitized_bytes_seen", self.sanitized_bytes_seen)
        _require_nonneg_int("retained_bytes", self.retained_bytes)
        _require_bool("source_eof", self.source_eof)
        _require_bool("cap_truncated", self.cap_truncated)
        _require_bool("overlap_discarded", self.overlap_discarded)
        # Retention is always a subset of what was configured and of what
        # was actually seen: a report claiming to have retained more than
        # either ceiling is an impossible accounting state, not a value a
        # caller should ever have to defend against downstream.
        if self.retained_bytes > self.limit_bytes:
            raise ValueError(
                f"retained_bytes ({self.retained_bytes}) must not exceed "
                f"limit_bytes ({self.limit_bytes})"
            )
        if self.retained_bytes > self.sanitized_bytes_seen:
            raise ValueError(
                f"retained_bytes ({self.retained_bytes}) must not exceed "
                f"sanitized_bytes_seen ({self.sanitized_bytes_seen})"
            )

    @property
    def truncated(self) -> bool:
        """True unless the capture is a complete, uncapped, unambiguous
        record of its source: any cap truncation, any non-EOF finish, or any
        discarded ambiguous overlap all make the retained text incomplete."""
        return self.cap_truncated or not self.source_eof or self.overlap_discarded


@dataclass(frozen=True, slots=True)
class CapturedTextV1:
    """The sanitized, retained text of one finished capture plus its report.

    ``text``'s UTF-8 byte length must equal ``report.retained_bytes`` — the
    report's own accounting of what survived retention — so a caller can
    trust the count without re-encoding to check it. ``repr()`` deliberately
    never reveals ``text`` (provider-derived output, credential-adjacent even
    after scrubbing) or any field of ``report`` that could echo it back.
    """

    text: str
    report: CaptureReportV1

    def __post_init__(self) -> None:
        if type(self.text) is not str:
            raise ValueError(f"text must be a str, got {type(self.text).__name__}")
        if type(self.report) is not CaptureReportV1:
            raise ValueError(f"report must be a CaptureReportV1, got {type(self.report).__name__}")
        text_bytes = len(self.text.encode("utf-8"))
        if text_bytes != self.report.retained_bytes:
            raise ValueError(
                f"text UTF-8 byte length ({text_bytes}) must equal "
                f"report.retained_bytes ({self.report.retained_bytes})"
            )

    def __repr__(self) -> str:  # never expose captured/provider-derived content
        return f"CapturedTextV1(text=<{len(self.text)} chars>, report={self.report!r})"


class BoundedOutputCapture(Protocol):
    def feed(self, chunk: bytes) -> None: ...

    def finish(self, *, source_eof: bool) -> CapturedTextV1: ...
