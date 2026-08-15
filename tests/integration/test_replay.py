"""Acceptance criterion 8: replay reproduces terminal state and artifact refs."""

from __future__ import annotations

import dataclasses

import pytest

from dagvane.application.replay import fold_envelopes, fold_frames, rebuild_report
from dagvane.domain.models import NodeStatus, ReplayError, RunStatus
from dagvane.protocol.documents import build_decision_doc
from dagvane.protocol.frames import canonical_json_bytes, frame_to_envelope, sha256_hex
from helpers import CompletedRun


def test_replay_rebuilds_report_byte_identically(happy_run: CompletedRun) -> None:
    view = fold_frames(happy_run.journal_lines())
    assert canonical_json_bytes(rebuild_report(view)) == (
        happy_run.run_dir / "report.json"
    ).read_bytes()


def test_replay_rebuilds_decision_byte_identically(happy_run: CompletedRun) -> None:
    view = fold_frames(happy_run.journal_lines())
    assert view.decision is not None
    assert canonical_json_bytes(build_decision_doc(view.decision)) == (
        happy_run.run_dir / "decision.json"
    ).read_bytes()


def test_replayed_state_matches_the_terminal_run(happy_run: CompletedRun) -> None:
    view = fold_frames(happy_run.journal_lines())
    assert view.status is RunStatus.COMPLETED
    assert view.last_seq == len(happy_run.journal_lines())
    assert all(node.status is NodeStatus.COMPLETED for node in view.nodes.values())
    assert view.total_calls == 5


def test_replayed_artifact_refs_resolve_to_content(happy_run: CompletedRun) -> None:
    view = fold_frames(happy_run.journal_lines())
    assert view.artifacts, "run must reference artifacts"
    for ref in view.artifacts:
        blob = (happy_run.run_dir / "artifacts" / ref.sha256).read_bytes()
        assert sha256_hex(blob) == ref.sha256
        assert len(blob) == ref.size


def test_replay_detects_a_gap(happy_run: CompletedRun) -> None:
    lines = happy_run.journal_lines()
    with pytest.raises(ReplayError, match="gapless"):
        fold_frames(lines[:5] + lines[6:])


def test_replay_requires_a_terminal_event_by_default(happy_run: CompletedRun) -> None:
    lines = happy_run.journal_lines()[:-1]
    with pytest.raises(ReplayError, match="terminal"):
        fold_frames(lines)
    view = fold_frames(lines, require_terminal=False)
    assert view.status is RunStatus.RUNNING


def test_replay_rejects_events_after_terminal(happy_run: CompletedRun) -> None:
    envelopes = [frame_to_envelope(line) for line in happy_run.journal_lines()]
    reordered = envelopes[:-2] + [envelopes[-1], envelopes[-2]]
    renumbered = [
        dataclasses.replace(envelope, seq=index + 1)
        for index, envelope in enumerate(reordered)
    ]
    with pytest.raises(ReplayError, match="after terminal"):
        fold_envelopes(renumbered)


def test_report_matches_an_independent_naive_fold(happy_run: CompletedRun) -> None:
    """Oracle independence: a test-local reducer over the raw journal JSON.

    The executor writes report.json through fold_envelopes/rebuild_report; this
    reducer shares none of that code, so agreement here is real evidence.
    """
    import json

    @dataclasses.dataclass
    class NaiveNode:
        status: str = "running"
        reason: str | None = None
        output_sha256: str | None = None
        calls: int = 0
        input_tokens: int = 0
        output_tokens: int = 0
        cost_microusd: int = 0

    nodes: dict[str, NaiveNode] = {}
    totals = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_microusd": 0}
    artifacts: list[str] = []
    status: str | None = None
    reason: str | None = None
    decision: dict[str, object] | None = None

    for line in happy_run.journal_lines():
        event = json.loads(line)
        etype, data, node_id = event["type"], event["data"], event["node_id"]
        if etype == "node.started":
            nodes[node_id] = NaiveNode()
        elif etype == "artifact.written":
            artifacts.append(data["sha256"])
        elif etype == "model.completed":
            node = nodes[node_id]
            node.calls += 1
            node.input_tokens += data["input_tokens"]
            node.output_tokens += data["output_tokens"]
            node.cost_microusd += data["cost_microusd"]
            totals["calls"] += 1
            for key in ("input_tokens", "output_tokens", "cost_microusd"):
                totals[key] += data[key]
        elif etype == "node.completed":
            nodes[node_id].status = "completed"
            nodes[node_id].output_sha256 = data["output_sha256"]
        elif etype == "node.failed":
            nodes[node_id].status = "failed"
            nodes[node_id].reason = data["reason"]
        elif etype == "decision.recorded":
            decision = {
                "decision_version": data["decision_version"],
                "winner": data["winner"],
                "rationale": data["rationale"],
                "source_sha256": data["source_sha256"],
            }
        elif etype == "run.finished":
            status, reason = data["status"], data["reason"]

    report = json.loads((happy_run.run_dir / "report.json").read_bytes())
    assert report["status"] == status
    assert report["reason"] == reason
    assert report["decision"] == decision
    assert report["budget"]["committed"] == totals
    assert set(report["nodes"]) == set(nodes)
    for node_id, naive in nodes.items():
        assert report["nodes"][node_id] == dataclasses.asdict(naive), node_id
    assert [a["sha256"] for a in report["artifacts"]] == artifacts
