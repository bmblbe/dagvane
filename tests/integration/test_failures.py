"""Failure paths stay durable and honest: invalid judge decision, backend error.

Every run — including failed ones — persists a RunReport; a Decision exists
only for judged runs. No silent degradation to a single-candidate council.
"""

from __future__ import annotations

import json
from pathlib import Path

from dagvane.protocol.frames import frame_to_envelope
from helpers import (
    FIXTURE_BAD_DECISION,
    FIXTURE_MISSING_MODEL,
    TASK_BASIC,
    CompletedRun,
    run_council_cli,
)


def _node_failures(run: CompletedRun) -> dict[str, str]:
    failures: dict[str, str] = {}
    for line in run.journal_lines():
        envelope = frame_to_envelope(line)
        if envelope.type == "node.failed":
            assert envelope.node_id is not None
            failures[envelope.node_id] = str(envelope.data["reason"])
    return failures


def test_malformed_judge_decision_fails_loud(tmp_path: Path) -> None:
    run = run_council_cli(TASK_BASIC, FIXTURE_BAD_DECISION, tmp_path, "r-bad-decision-0001")
    assert run.returncode == 10
    assert _node_failures(run) == {"judge": "invalid_decision"}
    report = json.loads((run.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["reason"] == "judge: invalid_decision"
    assert report["nodes"]["judge"]["calls"] == 1  # the model ran; the decision failed
    assert report["decision"] is None
    assert not (run.run_dir / "decision.json").exists()


def test_backend_error_fails_the_run_without_degradation(tmp_path: Path) -> None:
    run = run_council_cli(TASK_BASIC, FIXTURE_MISSING_MODEL, tmp_path, "r-missing-0001")
    assert run.returncode == 10
    failures = _node_failures(run)
    assert failures["review-by-b"] == "backend_error"
    assert failures["judge"] == "dependency_failed"  # no single-review shortcut to a verdict
    report = json.loads((run.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["budget"]["committed"]["calls"] == 3  # two proposers + one review
    assert report["decision"] is None
    assert not (run.run_dir / "decision.json").exists()
