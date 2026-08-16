"""Stdlib-only bounded, sanitized output capture.

Decodes fed bytes incrementally as UTF-8 — malformed sequences are replaced
deterministically with U+FFFD, independent of how bytes are split across
``feed`` calls — and retains only a bounded HEAD or TAIL byte window of the
decoded text. The retained window is built from marker-atomic segments: the
redaction marker literal is treated as one indivisible unit, whether its
characters arrive whole in one chunk or split across several ``feed`` calls,
so truncation can never emit a partial marker.

This module does no secret scrubbing itself; text fed here is assumed
already scrubbed by the caller ("sanitized" means UTF-8-repaired, not
secret-free). It exists to bound memory and byte counts on whatever text a
caller chooses to retain — never to decide what is secret. No filesystem,
tempfile, subprocess, or alternate raw spool: everything lives in memory for
the lifetime of one capture session.
"""

from __future__ import annotations

from collections import deque
from encodings.utf_8 import IncrementalDecoder as Utf8IncrementalDecoder

from dagvane.domain.secrets import REDACTION_MARKER
from dagvane.ports.output import (
    CapturedTextV1,
    CaptureLimitsV1,
    CaptureReportV1,
    RetentionPolicy,
)

_MARKER = REDACTION_MARKER
_MARKER_LEN = len(_MARKER)


def _split_marker_segments(text: str) -> tuple[list[str], str]:
    """Split ``text`` into marker-atomic segments — a marker occurrence is
    one segment, everything else is single characters — plus any trailing
    fragment that could still be an incomplete marker prefix awaiting more
    text. Only the last ``_MARKER_LEN - 1`` characters can be ambiguous: a
    marker occurrence resolved anywhere earlier is already decided.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        remaining = n - i
        if remaining >= _MARKER_LEN and text[i : i + _MARKER_LEN] == _MARKER:
            out.append(_MARKER)
            i += _MARKER_LEN
            continue
        if remaining < _MARKER_LEN and _MARKER.startswith(text[i:]):
            return out, text[i:]
        out.append(text[i])
        i += 1
    return out, ""


class BoundedTextCapture:
    """A ``BoundedOutputCapture``: incremental UTF-8 decode plus
    marker-atomic HEAD/TAIL byte retention over the decoded text."""

    __slots__ = (
        "_limits",
        "_retention",
        "_decoder",
        "_pending",
        "_raw_bytes_consumed",
        "_sanitized_bytes_seen",
        "_segments",
        "_retained_bytes",
        "_cap_truncated",
        "_head_done",
        "_finished",
    )

    def __init__(self, limits: CaptureLimitsV1, retention: RetentionPolicy) -> None:
        if not isinstance(limits, CaptureLimitsV1):
            raise ValueError(f"limits must be a CaptureLimitsV1, got {limits!r}")
        if not isinstance(retention, RetentionPolicy):
            raise ValueError(f"retention must be a RetentionPolicy, got {retention!r}")
        self._limits = limits
        self._retention = retention
        self._decoder: Utf8IncrementalDecoder = Utf8IncrementalDecoder(errors="replace")
        self._pending = ""
        self._raw_bytes_consumed = 0
        self._sanitized_bytes_seen = 0
        self._segments: deque[str] = deque()
        self._retained_bytes = 0
        self._cap_truncated = False
        self._head_done = False
        self._finished = False

    def __repr__(self) -> str:  # never expose captured content
        return f"BoundedTextCapture(retention={self._retention.value})"

    def feed(self, chunk: bytes) -> None:
        if self._finished:
            raise RuntimeError("feed() called after finish()")
        if type(chunk) is not bytes:
            raise ValueError(f"feed() chunk must be bytes, got {type(chunk).__name__}")
        self._raw_bytes_consumed += len(chunk)
        decoded = self._decoder.decode(chunk, final=False)
        self._absorb(decoded)

    def _absorb(self, decoded: str) -> None:
        if not decoded:
            return
        self._sanitized_bytes_seen += len(decoded.encode("utf-8"))
        if self._retention is RetentionPolicy.HEAD and self._head_done:
            return  # nothing further is ever retained once HEAD has capped
        buffer = self._pending + decoded
        segments, pending = _split_marker_segments(buffer)
        self._pending = pending
        self._append_segments(segments)

    def _append_segments(self, segments: list[str]) -> None:
        limit = self._limits.retained_bytes
        if self._retention is RetentionPolicy.HEAD:
            for seg in segments:
                seg_bytes = len(seg.encode("utf-8"))
                if self._retained_bytes + seg_bytes > limit:
                    self._head_done = True
                    self._cap_truncated = True
                    self._pending = ""
                    return
                self._segments.append(seg)
                self._retained_bytes += seg_bytes
        else:
            for seg in segments:
                seg_bytes = len(seg.encode("utf-8"))
                self._segments.append(seg)
                self._retained_bytes += seg_bytes
                while self._retained_bytes > limit and self._segments:
                    dropped = self._segments.popleft()
                    self._retained_bytes -= len(dropped.encode("utf-8"))
                    self._cap_truncated = True

    def finish(self, *, source_eof: bool) -> CapturedTextV1:
        if self._finished:
            raise RuntimeError("finish() called after finish()")
        if type(source_eof) is not bool:
            raise ValueError(
                f"finish() source_eof must be a bool, got {type(source_eof).__name__}"
            )
        self._finished = True
        overlap_discarded = False
        if source_eof:
            tail = self._decoder.decode(b"", final=True)
            self._absorb(tail)
            if self._pending:
                leftover = list(self._pending)
                self._pending = ""
                # No more data is coming: this fragment can never complete
                # into the marker, so it is provably ordinary literal text.
                if not (self._retention is RetentionPolicy.HEAD and self._head_done):
                    self._append_segments(leftover)
        else:
            # Neither the marker-prefix fragment nor any byte-incomplete
            # UTF-8 sequence still buffered inside the decoder can be told
            # apart from one truncated mid-stream: discard both unread.
            dangling = bool(self._decoder.getstate()[0])
            overlap_discarded = bool(self._pending) or dangling
            self._pending = ""
        text = "".join(self._segments)
        report = CaptureReportV1(
            schema_version=1,
            retention=self._retention,
            limit_bytes=self._limits.retained_bytes,
            raw_bytes_consumed=self._raw_bytes_consumed,
            sanitized_bytes_seen=self._sanitized_bytes_seen,
            retained_bytes=self._retained_bytes,
            source_eof=source_eof,
            cap_truncated=self._cap_truncated,
            overlap_discarded=overlap_discarded,
        )
        self._segments = deque()
        self._pending = ""
        self._decoder.reset()  # drop any dangling bytes; nothing survives finish
        return CapturedTextV1(text=text, report=report)
