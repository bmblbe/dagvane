"""Opt-in live smoke test — real providers, real money (tightly capped).

Skipped by default; never runs in CI. To run it locally:

    export DAGVANE_LIVE_TESTS=1
    export DAGVANE_LIVE_PROFILE=/path/to/live-profile.toml   # + credential env vars
    uv run pytest tests/live -q

The profile decides which providers participate; no credentials or provider
names live in this repository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from helpers import run_cli

pytestmark = pytest.mark.skipif(
    os.environ.get("DAGVANE_LIVE_TESTS") != "1",
    reason="live tests are opt-in: set DAGVANE_LIVE_TESTS=1 and DAGVANE_LIVE_PROFILE",
)

LIVE_TASK = {
    "task_version": 1,
    "task_id": "live-smoke-0001",
    "title": "Live smoke",
    "statement": (
        "Answer with one short sentence: what is the primary benefit of "
        "append-only event journals?"
    ),
    "acceptance_criteria": ["One sentence."],
    "budget": {"max_calls": 6, "max_total_tokens": 20000, "max_cost_microusd": 500000},
}


def test_live_council_smoke(tmp_path: Path) -> None:
    profile = os.environ.get("DAGVANE_LIVE_PROFILE")
    assert profile, "DAGVANE_LIVE_PROFILE must point to a live profile TOML"

    task_file = tmp_path / "task.json"
    task_file.write_text(json.dumps(LIVE_TASK), encoding="utf-8")

    proc = run_cli(
        ["council", str(task_file), "--profile", profile, "--output", "ndjson"], tmp_path
    )
    assert proc.returncode in (0, 10), proc.stderr.decode()

    frames = [json.loads(line) for line in proc.stdout.splitlines()]
    assert frames, "live run must stream NDJSON frames"
    assert frames[0]["type"] == "run.created"
    assert frames[-1]["type"] == "run.finished"

    run_id = frames[0]["run_id"]
    run_dir = tmp_path / ".dagvane" / "runs" / run_id
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "report.json").exists()
    receipts = [
        f
        for f in frames
        if f["type"] == "artifact.written" and f["data"]["role"] == "receipt"
    ]
    assert receipts, "live dispatches must persist receipts"
