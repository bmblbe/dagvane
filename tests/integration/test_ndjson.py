"""Acceptance criterion 11: NDJSON stdout purity and journal byte-identity —
for successful and failed runs alike."""

from __future__ import annotations

from pathlib import Path

from dagvane.protocol.frames import frame_to_envelope
from helpers import FIXTURE_BAD_DECISION, TASK_BASIC, CompletedRun, run_council_cli


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


def test_failed_run_stream_is_valid_terminal_last_and_journal_identical(
    tmp_path: Path,
) -> None:
    run = run_council_cli(TASK_BASIC, FIXTURE_BAD_DECISION, tmp_path, "r-bad-decision-0001")
    assert run.returncode == 10
    lines = run.stdout.splitlines(keepends=True)
    envelopes = [frame_to_envelope(line) for line in lines]  # every frame is valid
    assert envelopes[-1].type == "run.finished"
    assert envelopes[-1].data["status"] == "failed"
    assert all(env.type != "run.finished" for env in envelopes[:-1])  # terminal-last
    assert run.stdout == run.journal_path.read_bytes()
    assert b"dagvane:" not in run.stdout
