"""Domain model tests: state machines, closed event registry, payload codec."""

from __future__ import annotations

import pytest

from dagvane.domain.models import (
    EVENT_REGISTRY,
    NodeFailed,
    NodeStatus,
    ProtocolError,
    RunCreated,
    RunFinished,
    RunStatus,
    TransitionError,
    advance_node_status,
    advance_run_status,
    decode_payload,
    estimate_tokens,
    payload_to_data,
)


def test_run_transitions_legal_path() -> None:
    status = RunStatus.CREATED
    status = advance_run_status(status, RunStatus.RUNNING)
    assert advance_run_status(status, RunStatus.COMPLETED) is RunStatus.COMPLETED
    assert advance_run_status(status, RunStatus.FAILED) is RunStatus.FAILED


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (RunStatus.CREATED, RunStatus.COMPLETED),
        (RunStatus.CREATED, RunStatus.FAILED),
        (RunStatus.COMPLETED, RunStatus.RUNNING),
        (RunStatus.FAILED, RunStatus.COMPLETED),
        (RunStatus.RUNNING, RunStatus.CREATED),
    ],
)
def test_run_transitions_illegal(current: RunStatus, target: RunStatus) -> None:
    with pytest.raises(TransitionError):
        advance_run_status(current, target)


def test_node_transitions() -> None:
    assert advance_node_status(NodeStatus.PENDING, NodeStatus.RUNNING) is NodeStatus.RUNNING
    assert advance_node_status(NodeStatus.PENDING, NodeStatus.FAILED) is NodeStatus.FAILED
    assert advance_node_status(NodeStatus.RUNNING, NodeStatus.COMPLETED) is NodeStatus.COMPLETED
    with pytest.raises(TransitionError):
        advance_node_status(NodeStatus.COMPLETED, NodeStatus.RUNNING)
    with pytest.raises(TransitionError):
        advance_node_status(NodeStatus.PENDING, NodeStatus.COMPLETED)


def test_registry_is_closed_and_consistent() -> None:
    assert set(EVENT_REGISTRY.keys()) == {
        "run.created",
        "node.started",
        "artifact.written",
        "model.dispatched",
        "model.completed",
        "model.failed",
        "node.completed",
        "node.failed",
        "budget.rejected",
        "decision.recorded",
        "run.finished",
    }
    for event_type, cls in EVENT_REGISTRY.items():
        assert cls.TYPE == event_type


def test_payload_round_trip() -> None:
    payloads = (
        RunCreated(
            engine_version="0.0.0",
            task_sha256="t" * 64,
            plan_sha256="p" * 64,
            fixture_sha256="f" * 64,
            node_count=5,
            max_calls=60,
            max_total_tokens=1000,
            max_cost_microusd=8_000_000,
        ),
        NodeFailed(reason="backend_error", message="boom"),
        RunFinished(
            status="completed",
            reason=None,
            calls=5,
            input_tokens=10,
            output_tokens=20,
            cost_microusd=30,
        ),
    )
    for payload in payloads:
        data = payload_to_data(payload)
        assert decode_payload(payload.TYPE, data) == payload


def test_decode_unknown_type_rejected() -> None:
    with pytest.raises(ProtocolError, match="unknown event type"):
        decode_payload("run.exploded", {})


def test_decode_rejects_missing_extra_and_mistyped_keys() -> None:
    good = payload_to_data(NodeFailed(reason="x", message="y"))
    with pytest.raises(ProtocolError):
        decode_payload("node.failed", {k: v for k, v in good.items() if k != "reason"})
    with pytest.raises(ProtocolError):
        decode_payload("node.failed", {**good, "extra": 1})
    with pytest.raises(ProtocolError):
        decode_payload("node.failed", {**good, "reason": 7})
    with pytest.raises(ProtocolError):
        decode_payload("run.finished", {**payload_to_data(
            RunFinished(
                status="completed",
                reason=None,
                calls=1,
                input_tokens=1,
                output_tokens=1,
                cost_microusd=1,
            )
        ), "calls": True})  # bool is not an acceptable integer


def test_estimate_tokens() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
