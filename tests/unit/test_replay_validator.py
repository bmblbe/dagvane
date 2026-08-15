"""Replay as a fail-closed causal validator, proven on hand-built journals.

Every journal here is constructed event by event in the test — never by the
executor — so these matrices do not share the implementation's blind spots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from dagvane.application.replay import fold_envelopes
from dagvane.domain.models import (
    ArtifactWritten,
    DecisionRecorded,
    EventEnvelope,
    EventPayload,
    ModelCompleted,
    ModelDispatched,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    NodeStatus,
    ReplayError,
    RunCreated,
    RunFinished,
    RunStatus,
    payload_to_data,
)

RUN_ID = "r-causal-test"
REQ_SHA = "1" * 64
OUT_SHA = "2" * 64
JUDGE_OUT_SHA = "3" * 64


@dataclass
class Journal:
    """Hand-rolled envelope sequence with auto-assigned gapless seq."""

    seq: int = 0
    envelopes: list[EventEnvelope] = field(default_factory=list)

    def add(
        self,
        payload: EventPayload,
        *,
        node_id: str | None = None,
        operation_id: str | None = None,
        call_id: str | None = None,
    ) -> Journal:
        self.seq += 1
        self.envelopes.append(
            EventEnvelope(
                v=1,
                event_id=f"event-{self.seq}",
                run_id=RUN_ID,
                seq=self.seq,
                ts="2026-01-01T00:00:00.000Z",
                node_id=node_id,
                attempt=1 if node_id is not None else None,
                operation_id=operation_id,
                call_id=call_id,
                type=payload.TYPE,
                data=payload_to_data(payload),
            )
        )
        return self


def created(node_count: int = 1, *, max_total_tokens: int = 1_000_000) -> RunCreated:
    return RunCreated(
        engine_version="0.0.0",
        task_sha256="t" * 64,
        plan_sha256="p" * 64,
        fixture_sha256="f" * 64,
        node_count=node_count,
        max_calls=100,
        max_total_tokens=max_total_tokens,
        max_cost_microusd=1_000_000,
    )


def artifact(sha256: str, role: str = "request") -> ArtifactWritten:
    return ArtifactWritten(sha256=sha256, size=10, media_type="text/plain", role=role)


def dispatched(request_sha256: str = REQ_SHA) -> ModelDispatched:
    return ModelDispatched(
        route_id="fake/x",
        model="fake-x",
        request_sha256=request_sha256,
        reserved_calls=1,
        reserved_tokens=100,
        reserved_cost_microusd=10,
    )


def completed(output_sha256: str = OUT_SHA) -> ModelCompleted:
    return ModelCompleted(
        model="fake-x",
        input_tokens=10,
        output_tokens=5,
        cost_microusd=3,
        output_sha256=output_sha256,
    )


def finished(
    status: str = "completed",
    *,
    reason: str | None = None,
    calls: int = 1,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost_microusd: int = 3,
) -> RunFinished:
    return RunFinished(
        status=status,
        reason=reason,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=cost_microusd,
    )


def decision(source_sha256: str = JUDGE_OUT_SHA, version: int = 1) -> DecisionRecorded:
    return DecisionRecorded(
        decision_version=version,
        winner="candidate-1",
        rationale="because",
        source_sha256=source_sha256,
    )


def node_lifecycle(
    journal: Journal,
    node_id: str,
    *,
    role: str = "proposer",
    output_sha256: str = OUT_SHA,
    operation_id: str | None = None,
    call_id: str | None = None,
) -> Journal:
    op = operation_id if operation_id is not None else f"op-{node_id}"
    call = call_id if call_id is not None else f"call-{node_id}"
    journal.add(NodeStarted(role=role, route_id="fake/x"), node_id=node_id)
    journal.add(artifact(REQ_SHA), node_id=node_id)
    journal.add(dispatched(), node_id=node_id, operation_id=op, call_id=call)
    journal.add(artifact(output_sha256, role=role), node_id=node_id)
    journal.add(completed(output_sha256), node_id=node_id, operation_id=op, call_id=call)
    journal.add(NodeCompleted(output_sha256=output_sha256), node_id=node_id)
    return journal


def single_node_run() -> Journal:
    journal = Journal().add(created(1))
    node_lifecycle(journal, "n1")
    return journal


# ---------------------------------------------------------------------------
# Positive shapes the validator must keep accepting
# ---------------------------------------------------------------------------


def test_hand_built_completed_run_folds() -> None:
    journal = single_node_run().add(finished())
    view = fold_envelopes(journal.envelopes)
    assert view.status is RunStatus.COMPLETED
    assert view.nodes["n1"].status is NodeStatus.COMPLETED
    assert view.nodes["n1"].output_sha256 == OUT_SHA
    assert view.total_calls == 1


def test_failed_node_may_leave_an_abandoned_dispatch() -> None:
    # The backend-error path: dispatch happened, no completion ever arrives.
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="reviewer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(NodeFailed(reason="backend_error", message="boom"), node_id="n1")
    journal.add(finished("failed", reason="n1: backend_error", calls=0,
                         input_tokens=0, output_tokens=0, cost_microusd=0))
    view = fold_envelopes(journal.envelopes)
    assert view.status is RunStatus.FAILED
    assert view.nodes["n1"].reason == "backend_error"


def test_dependency_failed_node_needs_no_start_event() -> None:
    journal = Journal().add(created(2))
    node_lifecycle(journal, "n1")
    journal.add(
        NodeFailed(reason="dependency_failed", message="n0 did not complete"),
        node_id="n2",
    )
    journal.add(finished("failed", reason="n2: dependency_failed"))
    view = fold_envelopes(journal.envelopes)
    assert view.nodes["n2"].status is NodeStatus.FAILED


# ---------------------------------------------------------------------------
# Dispatch/completion correlation
# ---------------------------------------------------------------------------


def test_completion_without_dispatch_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(OUT_SHA), node_id="n1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-1")
    with pytest.raises(ReplayError, match="no matching open dispatch"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_duplicate_completion_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(artifact(OUT_SHA), node_id="n1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-1")
    with pytest.raises(ReplayError, match="no matching open dispatch"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_mismatched_completion_ids_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(artifact(OUT_SHA), node_id="n1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-other")
    with pytest.raises(ReplayError, match="no matching open dispatch"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_reused_operation_id_rejected() -> None:
    journal = Journal().add(created(1))
    node_lifecycle(journal, "n1", operation_id="op-1", call_id="call-1")
    journal.envelopes.pop()  # drop node.completed so n1 is still running
    journal.seq -= 1
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-2")
    with pytest.raises(ReplayError, match="operation_id 'op-1' reused"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_dispatch_without_ids_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1")
    with pytest.raises(ReplayError, match="requires operation_id and call_id"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_node_completed_with_open_dispatch_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(artifact(OUT_SHA), node_id="n1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(dispatched(), node_id="n1", operation_id="op-2", call_id="call-2")
    journal.add(NodeCompleted(output_sha256=OUT_SHA), node_id="n1")
    with pytest.raises(ReplayError, match="dispatch is still open"):
        fold_envelopes(journal.envelopes, require_terminal=False)


# ---------------------------------------------------------------------------
# Artifact linkage
# ---------------------------------------------------------------------------


def test_dispatch_referencing_unwritten_request_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    with pytest.raises(ReplayError, match="unwritten request artifact"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_completion_referencing_unwritten_output_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(completed(), node_id="n1", operation_id="op-1", call_id="call-1")
    with pytest.raises(ReplayError, match="unwritten output artifact"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_node_completed_output_must_match_model_output() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(artifact(REQ_SHA), node_id="n1")
    journal.add(dispatched(), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(artifact(OUT_SHA), node_id="n1")
    journal.add(artifact(JUDGE_OUT_SHA), node_id="n1")
    journal.add(completed(OUT_SHA), node_id="n1", operation_id="op-1", call_id="call-1")
    journal.add(NodeCompleted(output_sha256=JUDGE_OUT_SHA), node_id="n1")
    with pytest.raises(ReplayError, match="does not match its last model.completed"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_node_completed_without_any_model_call_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    journal.add(NodeCompleted(output_sha256=OUT_SHA), node_id="n1")
    with pytest.raises(ReplayError, match="without a completed model call"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_artifact_for_unstarted_node_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(artifact(REQ_SHA), node_id="n1")
    with pytest.raises(ReplayError, match="while node is pending"):
        fold_envelopes(journal.envelopes, require_terminal=False)


# ---------------------------------------------------------------------------
# Decision ordering and linkage
# ---------------------------------------------------------------------------


def judge_run_until_completion() -> Journal:
    journal = Journal().add(created(1))
    node_lifecycle(journal, "judge", role="judge", output_sha256=JUDGE_OUT_SHA)
    return journal


def test_decision_after_completed_judge_is_valid() -> None:
    journal = judge_run_until_completion().add(decision()).add(finished())
    view = fold_envelopes(journal.envelopes)
    assert view.decision is not None
    assert view.decision.winner == "candidate-1"


def test_decision_before_judge_completion_rejected() -> None:
    journal = judge_run_until_completion()
    journal.envelopes.pop()  # drop node.completed: judge is still running
    journal.seq -= 1
    journal.add(decision())
    with pytest.raises(ReplayError, match="requires a completed judge"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_decision_without_any_judge_rejected() -> None:
    journal = single_node_run().add(decision(source_sha256=OUT_SHA))
    with pytest.raises(ReplayError, match="requires a completed judge"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_decision_with_mismatched_source_rejected() -> None:
    journal = judge_run_until_completion().add(decision(source_sha256=OUT_SHA))
    with pytest.raises(ReplayError, match="requires a completed judge"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_duplicate_decision_rejected() -> None:
    journal = judge_run_until_completion().add(decision()).add(decision())
    with pytest.raises(ReplayError, match="duplicate decision.recorded"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_decision_with_unsupported_version_rejected() -> None:
    journal = judge_run_until_completion().add(decision(version=2))
    with pytest.raises(ReplayError, match="unsupported decision_version"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_decision_carrying_node_id_rejected() -> None:
    journal = judge_run_until_completion()
    journal.add(decision(), node_id="judge")
    with pytest.raises(ReplayError, match="must not carry node_id"):
        fold_envelopes(journal.envelopes, require_terminal=False)


# ---------------------------------------------------------------------------
# Terminal consistency
# ---------------------------------------------------------------------------


def test_completed_run_with_failed_node_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeFailed(reason="dependency_failed", message="x"), node_id="n1")
    journal.add(finished(calls=0, input_tokens=0, output_tokens=0, cost_microusd=0))
    with pytest.raises(ReplayError, match="completed run contains failed nodes"):
        fold_envelopes(journal.envelopes)


def test_completed_run_with_pending_node_rejected() -> None:
    journal = Journal().add(created(2))
    node_lifecycle(journal, "n1")
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n2")
    journal.add(finished())
    with pytest.raises(ReplayError, match="are not terminal"):
        fold_envelopes(journal.envelopes)


def test_completed_run_with_missing_node_rejected() -> None:
    journal = Journal().add(created(2))
    node_lifecycle(journal, "n1")
    journal.add(finished())
    with pytest.raises(ReplayError, match="run.created\\s+declared 2"):
        fold_envelopes(journal.envelopes)


def test_run_finished_totals_mismatch_rejected() -> None:
    journal = single_node_run().add(finished(input_tokens=999))
    with pytest.raises(ReplayError, match="totals do not match"):
        fold_envelopes(journal.envelopes)


def test_unknown_terminal_status_is_a_replay_error() -> None:
    journal = single_node_run().add(finished("exploded"))
    with pytest.raises(ReplayError, match="unknown status"):
        fold_envelopes(journal.envelopes)


def test_nonterminal_status_in_run_finished_rejected() -> None:
    journal = single_node_run().add(finished("running"))
    with pytest.raises(ReplayError, match="is not terminal"):
        fold_envelopes(journal.envelopes)


def test_completed_run_with_reason_rejected() -> None:
    journal = single_node_run().add(finished(reason="but why"))
    with pytest.raises(ReplayError, match="must not carry a failure reason"):
        fold_envelopes(journal.envelopes)


def test_failed_run_without_reason_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeFailed(reason="backend_error", message="x"), node_id="n1")
    journal.add(finished("failed", calls=0, input_tokens=0, output_tokens=0,
                         cost_microusd=0))
    with pytest.raises(ReplayError, match="requires a failure reason"):
        fold_envelopes(journal.envelopes)


def test_failed_run_without_failed_node_rejected() -> None:
    journal = single_node_run().add(finished("failed", reason="phantom"))
    with pytest.raises(ReplayError, match="contains no failed node"):
        fold_envelopes(journal.envelopes)


def test_completed_run_above_hard_caps_rejected() -> None:
    # Ties the budget postcondition into replay: a successfully completed run
    # whose honest totals exceed its recorded caps is causally impossible.
    journal = Journal().add(created(1, max_total_tokens=10))
    node_lifecycle(journal, "n1")
    journal.add(finished())
    with pytest.raises(ReplayError, match="exceeds its hard budget caps"):
        fold_envelopes(journal.envelopes)


def test_failed_run_may_exceed_caps_honestly() -> None:
    journal = Journal().add(created(1, max_total_tokens=10))
    node_lifecycle(journal, "n1")
    journal.envelopes.pop()  # replace node.completed with a budget failure
    journal.seq -= 1
    journal.add(NodeFailed(reason="budget_exceeded", message="over"), node_id="n1")
    journal.add(finished("failed", reason="n1: budget_exceeded"))
    view = fold_envelopes(journal.envelopes)
    assert view.status is RunStatus.FAILED
    assert view.total_input_tokens + view.total_output_tokens > 10


def test_unknown_node_failure_reason_rejected() -> None:
    journal = Journal().add(created(1))
    journal.add(NodeFailed(reason="cosmic_rays", message="x"), node_id="n1")
    with pytest.raises(ReplayError, match="unknown reason"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_run_finished_carrying_node_id_rejected() -> None:
    journal = single_node_run()
    journal.add(finished(), node_id="n1")
    with pytest.raises(ReplayError, match="must not carry node_id"):
        fold_envelopes(journal.envelopes)


def test_run_created_declaring_zero_nodes_rejected() -> None:
    journal = Journal().add(created(0))
    with pytest.raises(ReplayError, match="node_count 0"):
        fold_envelopes(journal.envelopes, require_terminal=False)


def test_first_event_must_be_run_created() -> None:
    journal = Journal()
    journal.add(NodeStarted(role="proposer", route_id="fake/x"), node_id="n1")
    with pytest.raises(ReplayError, match="first event must be run.created"):
        fold_envelopes(journal.envelopes, require_terminal=False)
