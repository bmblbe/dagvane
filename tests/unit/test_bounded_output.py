"""BoundedTextCapture / ports.output value-type unit tests (R1-B1)."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

from dagvane.adapters import bounded_output as bounded_output_module
from dagvane.adapters.bounded_output import BoundedTextCapture
from dagvane.domain import secrets as secrets_module
from dagvane.domain.secrets import REDACTION_MARKER
from dagvane.ports.output import CapturedTextV1, CaptureLimitsV1, CaptureReportV1, RetentionPolicy

_GENEROUS = CaptureLimitsV1(retained_bytes=1000, max_secret_variant_chars=1, max_secret_variants=1)


def _cap(retention: RetentionPolicy, limits: CaptureLimitsV1 = _GENEROUS) -> BoundedTextCapture:
    return BoundedTextCapture(limits, retention)


# --- incremental UTF-8 decoding -------------------------------------------


def test_multibyte_char_split_across_feed_calls_decodes_correctly() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    encoded = "abcéxyz".encode()
    for i in range(len(encoded)):
        cap.feed(encoded[i : i + 1])  # one byte at a time
    result = cap.finish(source_eof=True)
    assert result.text == "abcéxyz"
    assert result.report.raw_bytes_consumed == len(encoded)


def test_malformed_byte_expands_under_replacement_and_is_counted_exactly() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"\xff")
    result = cap.finish(source_eof=True)
    assert result.text == "�"
    assert result.report.raw_bytes_consumed == 1
    assert result.report.sanitized_bytes_seen == len("�".encode())


def test_dangling_multibyte_sequence_at_non_eof_finish_is_discarded_not_leaked() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"ok ")
    cap.feed(b"\xc3")  # first byte of a valid 2-byte sequence, held pending
    result = cap.finish(source_eof=False)
    assert result.text == "ok "
    assert result.report.overlap_discarded is True
    assert result.report.raw_bytes_consumed == 4
    assert result.report.sanitized_bytes_seen == len("ok ")


def test_same_dangling_sequence_resolves_deterministically_at_eof() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"ok ")
    cap.feed(b"\xc3")
    result = cap.finish(source_eof=True)
    assert result.text == "ok �"
    assert result.report.overlap_discarded is False


# --- HEAD/TAIL byte ceilings, multibyte-safe, exact -----------------------


def test_head_ceiling_with_multibyte_text_never_splits_a_character() -> None:
    text = "é" * 5  # 2 bytes each -> 10 bytes total
    limits = CaptureLimitsV1(retained_bytes=5, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.HEAD, limits)
    cap.feed(text.encode())
    result = cap.finish(source_eof=True)
    assert result.text == "é" * 2  # only 4 of the 5 bytes fit a whole char
    assert result.report.retained_bytes == 4
    assert result.report.cap_truncated is True


def test_tail_ceiling_with_multibyte_text_never_splits_a_character() -> None:
    text = "é" * 5
    limits = CaptureLimitsV1(retained_bytes=5, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.TAIL, limits)
    cap.feed(text.encode())
    result = cap.finish(source_eof=True)
    assert result.text == "é" * 2
    assert result.report.retained_bytes == 4
    assert result.report.cap_truncated is True


def test_head_ceiling_exact_boundary_passes_at_equal_and_fails_above() -> None:
    limits = CaptureLimitsV1(retained_bytes=5, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.HEAD, limits)
    cap.feed(b"hello")  # exactly 5 bytes
    result = cap.finish(source_eof=True)
    assert result.text == "hello"
    assert result.report.cap_truncated is False

    cap2 = _cap(RetentionPolicy.HEAD, limits)
    cap2.feed(b"hello!")  # 6 bytes, one over
    result2 = cap2.finish(source_eof=True)
    assert result2.text == "hello"
    assert result2.report.cap_truncated is True


# --- atomic redaction marker ------------------------------------------------


def test_marker_split_across_feed_calls_is_treated_atomically() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    text = f"a{REDACTION_MARKER}b"
    encoded = text.encode()
    for i in range(len(encoded)):
        cap.feed(encoded[i : i + 1])
    result = cap.finish(source_eof=True)
    assert result.text == text
    assert result.report.cap_truncated is False


def test_head_drops_whole_marker_when_cap_is_smaller_than_it() -> None:
    limits = CaptureLimitsV1(retained_bytes=5, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.HEAD, limits)
    cap.feed(REDACTION_MARKER.encode())
    result = cap.finish(source_eof=True)
    assert result.text == ""
    assert result.report.retained_bytes == 0
    assert result.report.cap_truncated is True


def test_tail_drops_whole_marker_when_cap_is_smaller_than_it() -> None:
    limits = CaptureLimitsV1(retained_bytes=5, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.TAIL, limits)
    cap.feed(REDACTION_MARKER.encode())
    result = cap.finish(source_eof=True)
    assert result.text == ""
    assert result.report.retained_bytes == 0
    assert result.report.cap_truncated is True


def test_head_drops_whole_marker_that_does_not_fit_remaining_budget() -> None:
    limits = CaptureLimitsV1(retained_bytes=13, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.HEAD, limits)
    cap.feed(f"12345678{REDACTION_MARKER}more".encode())
    result = cap.finish(source_eof=True)
    assert result.text == "12345678"
    assert REDACTION_MARKER not in result.text
    assert result.report.cap_truncated is True


def test_eof_flushes_an_unresolved_marker_prefix_fragment_as_literal_text() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"pre [redac")  # a genuine proper prefix of the marker
    result = cap.finish(source_eof=True)
    assert result.text == "pre [redac"
    assert result.report.overlap_discarded is False


def test_non_eof_finish_discards_an_unresolved_marker_prefix_fragment() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"pre [redac")
    result = cap.finish(source_eof=False)
    assert result.text == "pre "
    assert result.report.overlap_discarded is True


# --- exact reports/counters -------------------------------------------------


def test_raw_bytes_consumed_counts_every_fed_byte_exactly() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    chunks = [b"abc", b"\xc3\xa9", b"xyz"]
    for chunk in chunks:
        cap.feed(chunk)
    result = cap.finish(source_eof=True)
    assert result.report.raw_bytes_consumed == sum(len(c) for c in chunks)
    assert result.text == "abcéxyz"


def test_limit_bytes_and_retained_bytes_are_not_cross_wired() -> None:
    limits = CaptureLimitsV1(retained_bytes=4, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.HEAD, limits)
    cap.feed(b"hello world")
    result = cap.finish(source_eof=True)
    assert result.report.limit_bytes == 4
    assert result.report.retained_bytes == 4
    assert result.text == "hell"


def test_capture_rejects_wrong_limits_and_retention_types() -> None:
    with pytest.raises(ValueError):
        BoundedTextCapture("not-limits", RetentionPolicy.HEAD)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        # a plain str equal to the enum value must not pass the isinstance gate
        BoundedTextCapture(_GENEROUS, "head")  # type: ignore[arg-type]


# --- fail-closed lifecycle, no leakage --------------------------------------


def test_feed_and_finish_after_finish_fail_closed() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"hello")
    cap.finish(source_eof=True)
    with pytest.raises(RuntimeError):
        cap.feed(b"more")
    with pytest.raises(RuntimeError):
        cap.finish(source_eof=True)


def test_finish_clears_buffered_content() -> None:
    cap = _cap(RetentionPolicy.TAIL)
    cap.feed(b"some retained text")
    cap.finish(source_eof=True)
    assert len(cap._segments) == 0
    assert cap._pending == ""


def test_non_eof_finish_clears_dangling_decoder_bytes() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"ok \xc3")  # trailing first byte of a 2-byte sequence
    cap.finish(source_eof=False)
    assert cap._decoder.getstate()[0] == b""


def test_repr_never_exposes_captured_content() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"super-secret-looking-text-000")
    assert "super-secret-looking-text-000" not in repr(cap)


# --- large deterministic stream: bounded memory and retention --------------


def test_large_deterministic_stream_retention_stays_bounded() -> None:
    limits = CaptureLimitsV1(retained_bytes=1000, max_secret_variant_chars=1, max_secret_variants=1)
    cap = _cap(RetentionPolicy.TAIL, limits)
    block = ("x" * 41 + REDACTION_MARKER + "y" * 37).encode()
    for _ in range(3000):
        cap.feed(block)
    result = cap.finish(source_eof=True)
    assert result.report.retained_bytes <= 1000
    assert len(result.text.encode()) <= 1000
    assert result.report.cap_truncated is True
    assert result.report.overlap_discarded is False
    assert result.report.raw_bytes_consumed == len(block) * 3000


# --- ports.output value types: immutable, validated, correct -------------


def test_capture_limits_is_frozen_and_slotted() -> None:
    limits = CaptureLimitsV1(retained_bytes=10, max_secret_variant_chars=5, max_secret_variants=3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.retained_bytes = 20  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        limits.new_attr = 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_capture_limits_rejects_invalid_values(bad: object) -> None:
    with pytest.raises(ValueError):
        CaptureLimitsV1(
            retained_bytes=bad,  # type: ignore[arg-type]
            max_secret_variant_chars=1,
            max_secret_variants=1,
        )
    with pytest.raises(ValueError):
        CaptureLimitsV1(
            retained_bytes=1,
            max_secret_variant_chars=bad,  # type: ignore[arg-type]
            max_secret_variants=1,
        )
    with pytest.raises(ValueError):
        CaptureLimitsV1(
            retained_bytes=1,
            max_secret_variant_chars=1,
            max_secret_variants=bad,  # type: ignore[arg-type]
        )


_REPORT_BASE: dict[str, Any] = dict(
    schema_version=1,
    retention=RetentionPolicy.TAIL,
    limit_bytes=10,
    raw_bytes_consumed=5,
    sanitized_bytes_seen=5,
    retained_bytes=5,
    source_eof=True,
    cap_truncated=False,
    overlap_discarded=False,
)


def test_capture_report_is_frozen_and_slotted() -> None:
    report = CaptureReportV1(**_REPORT_BASE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.retained_bytes = 1  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        report.new_attr = 1  # type: ignore[attr-defined]


def test_capture_report_rejects_invalid_schema_version_and_types() -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(CaptureReportV1(**_REPORT_BASE), schema_version=2)
    with pytest.raises(ValueError):
        dataclasses.replace(CaptureReportV1(**_REPORT_BASE), source_eof=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(CaptureReportV1(**_REPORT_BASE), retained_bytes=True)
    with pytest.raises(ValueError):
        dataclasses.replace(CaptureReportV1(**_REPORT_BASE), retained_bytes=-1)


def test_capture_report_rejects_bool_schema_version() -> None:
    # ``True == 1`` in Python: a bare ``!=`` check would silently accept it.
    # ``bool`` is a subtype of ``int`` so this is not a static type error
    # (unlike ``retained_bytes=True`` below, which mypy also accepts) — only
    # the runtime ``__post_init__`` check catches it.
    with pytest.raises(ValueError):
        dataclasses.replace(CaptureReportV1(**_REPORT_BASE), schema_version=True)


def test_feed_rejects_non_bytes_chunk() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    with pytest.raises(ValueError):
        cap.feed("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cap.feed(bytearray(b"also-not-plain-bytes"))  # type: ignore[arg-type]


def test_finish_rejects_non_bool_source_eof() -> None:
    cap = _cap(RetentionPolicy.HEAD)
    cap.feed(b"hello")
    with pytest.raises(ValueError):
        cap.finish(source_eof=1)  # type: ignore[arg-type]


def test_capture_report_rejects_bool_as_int_on_every_counter() -> None:
    # ``bool`` is a subtype of ``int``: a bare ``!=``/range check would
    # silently accept ``True``/``False`` as ``1``/``0`` for any counter.
    base = CaptureReportV1(**_REPORT_BASE)
    with pytest.raises(ValueError):
        dataclasses.replace(base, limit_bytes=True)
    with pytest.raises(ValueError):
        dataclasses.replace(base, limit_bytes=False)
    with pytest.raises(ValueError):
        dataclasses.replace(base, raw_bytes_consumed=True)
    with pytest.raises(ValueError):
        dataclasses.replace(base, raw_bytes_consumed=False)
    with pytest.raises(ValueError):
        dataclasses.replace(base, sanitized_bytes_seen=True)
    with pytest.raises(ValueError):
        dataclasses.replace(base, sanitized_bytes_seen=False)
    with pytest.raises(ValueError):
        dataclasses.replace(base, retained_bytes=True)
    with pytest.raises(ValueError):
        dataclasses.replace(base, retained_bytes=False)


def test_capture_report_rejects_int_as_bool_on_every_flag() -> None:
    base = CaptureReportV1(**_REPORT_BASE)
    with pytest.raises(ValueError):
        dataclasses.replace(base, cap_truncated=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(base, cap_truncated=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(base, overlap_discarded=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        dataclasses.replace(base, overlap_discarded=0)  # type: ignore[arg-type]


def test_capture_report_rejects_retained_bytes_over_limit_bytes() -> None:
    # The reproduced impossible-value case: retained claims more than the
    # ceiling that was supposed to bound it.
    with pytest.raises(ValueError, match="retained_bytes"):
        dataclasses.replace(
            CaptureReportV1(**_REPORT_BASE),
            limit_bytes=1,
            sanitized_bytes_seen=999,
            retained_bytes=999,
        )


def test_capture_report_rejects_retained_bytes_over_sanitized_bytes_seen() -> None:
    # Retention can never exceed what was actually seen after sanitizing —
    # a report claiming to retain more than it saw is an impossible state.
    with pytest.raises(ValueError, match="retained_bytes"):
        dataclasses.replace(
            CaptureReportV1(**_REPORT_BASE),
            limit_bytes=100,
            sanitized_bytes_seen=0,
            retained_bytes=1,
        )


def test_capture_report_rejects_retained_over_limit_and_sanitized_together() -> None:
    # The exact reproduced probe: retained=999, limit=1, sanitized=0.
    with pytest.raises(ValueError):
        dataclasses.replace(
            CaptureReportV1(**_REPORT_BASE),
            limit_bytes=1,
            sanitized_bytes_seen=0,
            retained_bytes=999,
        )


def test_capture_report_boundary_retained_equal_to_limit_and_sanitized_passes() -> None:
    dataclasses.replace(
        CaptureReportV1(**_REPORT_BASE),
        limit_bytes=5,
        sanitized_bytes_seen=5,
        retained_bytes=5,
    )


def test_capture_report_truncated_property() -> None:
    complete = CaptureReportV1(**_REPORT_BASE)
    assert complete.truncated is False
    capped = dataclasses.replace(complete, cap_truncated=True)
    assert capped.truncated is True
    incomplete = dataclasses.replace(complete, source_eof=False)
    assert incomplete.truncated is True
    overlapped = dataclasses.replace(complete, overlap_discarded=True)
    assert overlapped.truncated is True


# --- CapturedTextV1: exact types, byte-length/report cross-check, no leak --

_SYNTHETIC_CREDENTIAL = "sk-synthetic-test-credential-000"


def _report_for(retained_bytes: int) -> CaptureReportV1:
    return dataclasses.replace(
        CaptureReportV1(**_REPORT_BASE),
        limit_bytes=max(retained_bytes, 1),
        sanitized_bytes_seen=retained_bytes,
        retained_bytes=retained_bytes,
    )


def test_captured_text_rejects_non_str_text() -> None:
    with pytest.raises(ValueError, match="text"):
        CapturedTextV1(text=123, report=_report_for(0))  # type: ignore[arg-type]


def test_captured_text_rejects_non_report_report() -> None:
    with pytest.raises(ValueError, match="report"):
        CapturedTextV1(text="hello", report="not-report")  # type: ignore[arg-type]


def test_captured_text_rejects_both_wrong_text_and_wrong_report() -> None:
    # The exact reproduced probe: text=123, report="not-report".
    with pytest.raises(ValueError):
        CapturedTextV1(text=123, report="not-report")  # type: ignore[arg-type]


def test_captured_text_rejects_byte_length_mismatch_with_report() -> None:
    with pytest.raises(ValueError, match="retained_bytes"):
        CapturedTextV1(text="hello", report=_report_for(999))


def test_captured_text_byte_length_mismatch_detects_multibyte_undercount() -> None:
    # "é" is 1 char but 2 UTF-8 bytes: a naive len(text) check would miss
    # this mismatch where a byte-accurate check catches it.
    with pytest.raises(ValueError, match="retained_bytes"):
        CapturedTextV1(text="é", report=_report_for(1))


def test_captured_text_accepts_matching_byte_length() -> None:
    captured = CapturedTextV1(text="héllo", report=_report_for(len("héllo".encode())))
    assert captured.text == "héllo"


def test_captured_text_repr_never_exposes_text_or_credential_content() -> None:
    text = f"prefix {_SYNTHETIC_CREDENTIAL} suffix"
    captured = CapturedTextV1(text=text, report=_report_for(len(text.encode())))
    rendered = repr(captured)
    assert _SYNTHETIC_CREDENTIAL not in rendered
    assert text not in rendered
    assert "chars>" in rendered


def test_captured_text_errors_never_expose_offending_text_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        CapturedTextV1(text=_SYNTHETIC_CREDENTIAL, report=_report_for(0))
    assert _SYNTHETIC_CREDENTIAL not in str(excinfo.value)

    with pytest.raises(ValueError) as excinfo:
        CapturedTextV1(text=123, report=_SYNTHETIC_CREDENTIAL)  # type: ignore[arg-type]
    assert _SYNTHETIC_CREDENTIAL not in str(excinfo.value)


def test_captured_text_is_frozen_and_slotted() -> None:
    captured = CapturedTextV1(text="hi", report=_report_for(len(b"hi")))
    with pytest.raises(dataclasses.FrozenInstanceError):
        captured.text = "bye"  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        captured.new_attr = 1  # type: ignore[attr-defined]


# --- scope guard: this core stays in-memory, stdlib-only -------------------


@pytest.mark.parametrize("module", [bounded_output_module, secrets_module])
def test_core_modules_have_no_filesystem_or_process_imports(module: object) -> None:
    source = inspect.getsource(module)  # type: ignore[arg-type]
    for token in ("import subprocess", "import tempfile", "import os", "open("):
        assert token not in source, (module, token)
