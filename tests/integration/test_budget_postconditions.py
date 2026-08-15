"""Hard-budget postconditions end to end: honest actuals, no successful overrun.

Fixture files deliberately accept arbitrary usage values — that is the
adversarial-input channel these tests use to prove the enforcement lives in
the worker/ledger, not in fixture parsing.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import DEFAULT_COUNCIL_BUDGET, run_council
from dagvane.application.replay import fold_frames
from dagvane.domain.models import RunStatus, Usage
from dagvane.protocol.documents import (
    FixtureSpec,
    load_fixture_file,
    load_task_file,
)
from dagvane.protocol.frames import frame_to_envelope
from helpers import FIXTURE_HAPPY, TASK_BASIC


def _fixture_with_usage(model: str, usage: Usage, run_id: str) -> FixtureSpec:
    fixture = load_fixture_file(FIXTURE_HAPPY)
    responses = dict(fixture.responses)
    responses[model] = dataclasses.replace(responses[model], usage=usage)
    return dataclasses.replace(fixture, responses=responses, run_id=run_id)


def test_usage_above_caps_fails_the_run_with_honest_accounting(tmp_path: Path) -> None:
    # Reservation admits on the small estimate; the backend then claims three
    # million input tokens — far above the 2M default token cap.
    huge = Usage(input_tokens=3_000_000, output_tokens=10)
    fixture = _fixture_with_usage("fake-proposer-a", huge, "r-overrun-0001")
    store = FilesystemRunStore(tmp_path)

    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=fixture,
        store=store,
        backend=FakeBackend(fixture.responses),
        sink=None,
    )

    assert result.status is RunStatus.FAILED
    run_dir = store.run_dir(result.run_id)
    report = json.loads((run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["nodes"]["proposer-a"]["reason"] == "budget_exceeded"
    assert report["decision"] is None
    # Actual usage is recorded honestly — above the cap — while the run fails.
    committed = report["budget"]["committed"]
    caps = report["budget"]["caps"]
    assert caps["max_total_tokens"] == DEFAULT_COUNCIL_BUDGET.max_total_tokens
    assert committed["input_tokens"] >= 3_000_000
    assert committed["input_tokens"] + committed["output_tokens"] > caps["max_total_tokens"]

    lines = (run_dir / "events.jsonl").read_bytes().splitlines(keepends=True)
    model_completed = [
        frame_to_envelope(line)
        for line in lines
        if frame_to_envelope(line).type == "model.completed"
        and frame_to_envelope(line).node_id == "proposer-a"
    ]
    assert len(model_completed) == 1
    assert model_completed[0].data["input_tokens"] == 3_000_000  # journaled honestly
    # The strict replay validator accepts this journal: failed runs may
    # honestly overshoot their caps.
    assert fold_frames(lines).status is RunStatus.FAILED


def test_output_tokens_above_route_limit_is_a_normalized_backend_error(
    tmp_path: Path,
) -> None:
    # The council routes pin max_output_tokens=2048; a backend claiming more
    # violates its contract and must not be trusted or billed (fake billing rule).
    lying = Usage(input_tokens=10, output_tokens=5_000)
    fixture = _fixture_with_usage("fake-proposer-a", lying, "r-liar-0001")
    store = FilesystemRunStore(tmp_path)

    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=fixture,
        store=store,
        backend=FakeBackend(fixture.responses),
        sink=None,
    )

    assert result.status is RunStatus.FAILED
    report = json.loads((store.run_dir(result.run_id) / "report.json").read_bytes())
    node = report["nodes"]["proposer-a"]
    assert node["reason"] == "backend_error"
    assert node["calls"] == 0  # rejected before commit: billed at zero
    assert node["input_tokens"] == 0 and node["output_tokens"] == 0
    lines = (store.run_dir(result.run_id) / "events.jsonl").read_bytes().splitlines(
        keepends=True
    )
    failures = [
        frame_to_envelope(line)
        for line in lines
        if frame_to_envelope(line).type == "node.failed"
        and frame_to_envelope(line).node_id == "proposer-a"
    ]
    assert len(failures) == 1
    assert "above the route limit" in str(failures[0].data["message"])
