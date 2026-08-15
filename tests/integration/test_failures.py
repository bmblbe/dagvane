"""Failure paths stay durable and honest: invalid judge decision, backend error.

Every run — including failed ones — persists a RunReport; a Decision exists
only for judged runs. No silent degradation to a single-candidate council.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council
from dagvane.domain.models import RunStatus
from dagvane.protocol.documents import FixtureResponse, load_fixture_file, load_task_file
from dagvane.protocol.frames import frame_to_envelope
from helpers import (
    FIXTURE_BAD_DECISION,
    FIXTURE_HAPPY,
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


def test_forged_ghost_winner_cannot_survive_end_to_end(tmp_path: Path) -> None:
    """The executor's winner universe is the judge's own manifest labels, so a
    syntactically valid decision naming an unseen candidate fails the run."""
    fixture = load_fixture_file(FIXTURE_HAPPY)
    responses = dict(fixture.responses)
    responses["fake-judge"] = FixtureResponse(
        text='{"decision_version": 1, "winner": "candidate-ghost", "rationale": "forged"}',
        usage=None,
    )
    forged = dataclasses.replace(fixture, responses=responses, run_id="r-ghost-0001")
    store = FilesystemRunStore(tmp_path)

    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=forged,
        store=store,
        backend=FakeBackend(forged.responses),
        sink=None,
    )

    assert result.status is RunStatus.FAILED
    run_dir = store.run_dir(result.run_id)
    report = json.loads((run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["nodes"]["judge"]["reason"] == "invalid_decision"
    assert report["decision"] is None
    assert not (run_dir / "decision.json").exists()
    lines = (run_dir / "events.jsonl").read_bytes().splitlines(keepends=True)
    assert all(frame_to_envelope(line).type != "decision.recorded" for line in lines)


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
