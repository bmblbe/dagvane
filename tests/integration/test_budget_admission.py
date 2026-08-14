"""Acceptance criterion 10: budget admission rejects before backend invocation."""

from __future__ import annotations

import json
from pathlib import Path

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council
from dagvane.domain.models import RunStatus
from dagvane.protocol.documents import load_fixture_file, load_task_file
from dagvane.protocol.frames import frame_to_envelope
from helpers import (
    FIXTURE_HAPPY,
    HAPPY_RUN_ID,
    TASK_LOW_BUDGET,
    run_council_cli,
)


def test_rejected_dispatches_never_reach_the_backend(tmp_path: Path) -> None:
    fixture = load_fixture_file(FIXTURE_HAPPY)
    backend = FakeBackend(fixture.responses)
    store = FilesystemRunStore(tmp_path)
    result = run_council(
        task=load_task_file(TASK_LOW_BUDGET),
        fixture=fixture,
        store=store,
        backend=backend,
        sink=None,
    )
    assert result.status is RunStatus.FAILED
    # max_calls=2: exactly the two proposers were invoked; nothing after rejection.
    assert [inv.model for inv in backend.invocations] == [
        "fake-proposer-a",
        "fake-proposer-b",
    ]
    envelopes = [
        frame_to_envelope(line) for line in store.iter_frames(result.run_id)
    ]
    by_type: dict[str, int] = {}
    for envelope in envelopes:
        by_type[envelope.type] = by_type.get(envelope.type, 0) + 1
    assert by_type["budget.rejected"] == 2  # both reviewers rejected at admission
    assert by_type["model.dispatched"] == 2
    assert by_type["model.completed"] == 2
    assert by_type["node.failed"] == 3  # two reviewers + judge (dependency_failed)
    assert "decision.recorded" not in by_type


def test_over_budget_run_fails_gracefully_with_report(tmp_path: Path) -> None:
    run = run_council_cli(TASK_LOW_BUDGET, FIXTURE_HAPPY, tmp_path, HAPPY_RUN_ID)
    assert run.returncode == 10
    report = json.loads((run.run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["reason"] == "review-by-a: budget_rejected"
    assert report["budget"]["committed"]["calls"] == 2
    assert report["budget"]["caps"]["max_calls"] == 2
    assert report["decision"] is None
    assert not (run.run_dir / "decision.json").exists()
    assert (run.run_dir / "events.jsonl").is_file()  # partial progress persisted
