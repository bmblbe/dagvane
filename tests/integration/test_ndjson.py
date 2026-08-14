"""Acceptance criterion 11: NDJSON stdout purity and journal byte-identity."""

from __future__ import annotations

from dagvane.protocol.frames import frame_to_envelope
from helpers import CompletedRun


def test_stdout_carries_only_valid_frames(happy_run: CompletedRun) -> None:
    lines = happy_run.stdout.splitlines(keepends=True)
    assert lines, "ndjson output must not be empty"
    for line in lines:
        frame_to_envelope(line)  # strict: envelope shape + closed payload registry


def test_stdout_is_byte_identical_to_the_journal(happy_run: CompletedRun) -> None:
    assert happy_run.stdout == happy_run.journal_path.read_bytes()


def test_diagnostics_go_to_stderr_only(happy_run: CompletedRun) -> None:
    assert b"dagvane:" in happy_run.stderr  # the seeded diagnostic exists
    assert happy_run.run_id.encode() in happy_run.stderr
    assert b"dagvane:" not in happy_run.stdout
