"""CLI surface: help/version without SDKs, dry-run planning, run inspection,
catch-up event streaming, and the exit-code contract (0/2/10/40 subset).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from helpers import (
    FIXTURE_HAPPY,
    HAPPY_RUN_ID,
    SRC_DIR,
    TASK_BASIC,
    CompletedRun,
    run_cli,
    run_council_cli,
)


def test_help_and_import_need_no_provider_sdk(tmp_path: Path) -> None:
    proc = run_cli(["--help"], tmp_path)
    assert proc.returncode == 0
    assert b"dagvane" in proc.stdout
    check = subprocess.run(
        [sys.executable, "-c", "import dagvane; print(dagvane.__version__)"],
        env={"PYTHONPATH": str(SRC_DIR), "PATH": "/usr/bin"},
        capture_output=True,
        check=False,
    )
    assert check.returncode == 0
    assert b"0.2.0.dev0" in check.stdout


def test_version(tmp_path: Path) -> None:
    proc = run_cli(["--version"], tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.strip() == b"dagvane 0.2.0.dev0"


def test_missing_arguments_is_a_usage_error(tmp_path: Path) -> None:
    assert run_cli([], tmp_path).returncode == 2
    assert run_cli(["council", str(TASK_BASIC)], tmp_path).returncode == 2  # no --fixture


def test_plan_council_dry_run_prints_the_plan_and_touches_nothing(tmp_path: Path) -> None:
    proc = run_cli(
        ["plan", "council", str(TASK_BASIC), "--dry-run", "--output", "json"], tmp_path
    )
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert doc["dry_run"] is True
    assert len(doc["plan"]["nodes"]) == 5
    assert len(doc["plan_sha256"]) == 64
    assert sorted(doc["routes"]) == [
        "fake/judge",
        "fake/proposer-a",
        "fake/proposer-b",
        "fake/reviewer-a",
        "fake/reviewer-b",
    ]
    assert not (tmp_path / ".dagvane").exists()


def test_invalid_task_file_is_a_usage_error(tmp_path: Path) -> None:
    bad_task = tmp_path / "bad-task.json"
    bad_task.write_text('{"task_version": 1, "task_id": "x", "title": "t"}')
    for args in (
        ["plan", "council", str(bad_task), "--dry-run", "--output", "json"],
        ["council", str(bad_task), "--fixture", str(FIXTURE_HAPPY)],
    ):
        proc = run_cli(args, tmp_path)
        assert proc.returncode == 2
        assert b"dagvane: error:" in proc.stderr
        assert proc.stdout == b""


def test_invalid_fixture_file_is_a_usage_error(tmp_path: Path) -> None:
    bad_fixture = tmp_path / "bad-fixture.json"
    bad_fixture.write_text('{"fixture_version": 1, "responses": {}, "surprise": true}')
    proc = run_cli(
        ["council", str(TASK_BASIC), "--fixture", str(bad_fixture)], tmp_path
    )
    assert proc.returncode == 2
    assert b"unknown keys" in proc.stderr


def test_runs_show_merges_manifest_and_derived_status(happy_run: CompletedRun) -> None:
    proc = run_cli(["runs", "show", HAPPY_RUN_ID, "--output", "json"], happy_run.cwd)
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert doc["manifest"]["run_id"] == HAPPY_RUN_ID
    assert doc["manifest"]["budget_caps"]["max_calls"] == 60
    assert doc["derived"]["status"] == "completed"
    assert doc["derived"]["decision"]["winner"] == "candidate-1"


def test_runs_show_unknown_run_is_a_usage_error(tmp_path: Path) -> None:
    proc = run_cli(["runs", "show", "r-none", "--output", "json"], tmp_path)
    assert proc.returncode == 2
    assert b"unknown run" in proc.stderr


def test_events_replays_canonical_bytes_with_catch_up(happy_run: CompletedRun) -> None:
    journal = happy_run.journal_path.read_bytes()
    full = run_cli(["events", HAPPY_RUN_ID, "--output", "ndjson"], happy_run.cwd)
    assert full.returncode == 0
    assert full.stdout == journal

    lines = happy_run.journal_lines()
    tail = run_cli(["events", HAPPY_RUN_ID, "--since", "31"], happy_run.cwd)
    assert tail.stdout == b"".join(lines[31:])
    beyond = run_cli(["events", HAPPY_RUN_ID, "--since", str(len(lines))], happy_run.cwd)
    assert beyond.stdout == b""
    assert run_cli(["events", HAPPY_RUN_ID, "--since", "-1"], happy_run.cwd).returncode == 2
    assert run_cli(["events", "r-none"], happy_run.cwd).returncode == 2


def test_council_json_output_is_the_canonical_report(tmp_path: Path) -> None:
    run = run_council_cli(TASK_BASIC, FIXTURE_HAPPY, tmp_path, HAPPY_RUN_ID, output="json")
    assert run.returncode == 0
    assert run.stdout == (run.run_dir / "report.json").read_bytes()
    assert json.loads(run.stdout)["status"] == "completed"


def test_council_text_output_renders_the_event_stream(tmp_path: Path) -> None:
    run = run_council_cli(TASK_BASIC, FIXTURE_HAPPY, tmp_path, HAPPY_RUN_ID, output="text")
    assert run.returncode == 0
    text = run.stdout.decode()
    assert f"run {HAPPY_RUN_ID}: created (5 nodes)" in text
    assert "decision: winner candidate-1" in text
    assert f"run {HAPPY_RUN_ID}: completed" in text
    assert text.count("\n") == len(run.journal_lines())  # one rendered line per frame


def test_rerunning_a_pinned_run_id_is_an_internal_error(tmp_path: Path) -> None:
    first = run_council_cli(TASK_BASIC, FIXTURE_HAPPY, tmp_path, HAPPY_RUN_ID)
    assert first.returncode == 0
    second = run_cli(
        ["council", str(TASK_BASIC), "--fixture", str(FIXTURE_HAPPY)], tmp_path
    )
    assert second.returncode == 40
    assert b"already exists" in second.stderr
    # the first run's durable state is untouched
    assert first.journal_path.read_bytes()
