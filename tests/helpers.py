"""Shared test helpers: paths, CLI runner, journal readers."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = TESTS_DIR / "fixtures"

TASK_BASIC = FIXTURES_DIR / "task_basic.json"
TASK_LOW_BUDGET = FIXTURES_DIR / "task_low_budget.json"
FIXTURE_HAPPY = FIXTURES_DIR / "fixture_happy.json"
FIXTURE_BAD_DECISION = FIXTURES_DIR / "fixture_bad_decision.json"
FIXTURE_MISSING_MODEL = FIXTURES_DIR / "fixture_missing_model.json"

HAPPY_RUN_ID = "r-happy-0001"


def run_cli(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m dagvane`` in ``cwd`` with the src tree importable (uninstalled)."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "dagvane", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
    )


@dataclass(frozen=True)
class CompletedRun:
    """A finished CLI council run and where its durable state lives."""

    cwd: Path
    run_id: str
    stdout: bytes
    stderr: bytes
    returncode: int

    @property
    def run_dir(self) -> Path:
        return self.cwd / ".dagvane" / "runs" / self.run_id

    @property
    def journal_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    def journal_lines(self) -> list[bytes]:
        return self.journal_path.read_bytes().splitlines(keepends=True)


def run_council_cli(
    task_file: Path, fixture_file: Path, cwd: Path, run_id: str, output: str = "ndjson"
) -> CompletedRun:
    proc = run_cli(
        ["council", str(task_file), "--fixture", str(fixture_file), "--output", output], cwd
    )
    return CompletedRun(
        cwd=cwd,
        run_id=run_id,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )
