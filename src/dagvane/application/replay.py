"""Event journal replay: a fail-closed causal validator plus derived views.

``events.jsonl`` is authoritative for run state (``manifest.json`` is the
sealed pre-run configuration it references by hash). This module is the single
code path that turns an event stream into run state — the executor builds
``report.json`` through the same fold, which is what guarantees that replaying
a journal reproduces the persisted derived views byte-identically.

Folding validates causality, not just shape: dispatch/completion correlation,
artifact linkage, node-state/run-status consistency, decision ordering, and
terminal-last semantics. Violations raise a normalized ``ReplayError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from dagvane.domain.models import (
    BACKEND_DISPATCH_KINDS,
    NODE_FAILURE_REASONS,
    ROLE_JUDGE,
    RUN_TERMINAL_STATUSES,
    USAGE_SOURCES,
    ArtifactWritten,
    Budget,
    BudgetRejected,
    Decision,
    DecisionRecorded,
    EventEnvelope,
    ModelCompleted,
    ModelDispatched,
    ModelFailed,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    NodeStatus,
    ReplayError,
    RunCreated,
    RunFinished,
    RunStatus,
    TransitionError,
    advance_node_status,
    advance_run_status,
    decode_payload,
)
from dagvane.protocol.documents import DECISION_VERSION, REPORT_VERSION, build_budget_doc
from dagvane.protocol.frames import frame_to_envelope


@dataclass(slots=True)
class NodeView:
    status: NodeStatus = NodeStatus.PENDING
    reason: str | None = None
    output_sha256: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_microusd: int = 0


@dataclass(frozen=True, slots=True)
class ArtifactView:
    sha256: str
    size: int
    media_type: str
    role: str
    node_id: str | None


@dataclass(slots=True)
class RunView:
    run_id: str
    engine_version: str
    task_sha256: str
    plan_sha256: str
    fixture_sha256: str
    caps: Budget
    status: RunStatus = RunStatus.RUNNING
    reason: str | None = None
    finished_ts: str | None = None
    last_seq: int = 0
    nodes: dict[str, NodeView] = field(default_factory=dict)
    artifacts: list[ArtifactView] = field(default_factory=list)
    decision: Decision | None = None
    decision_source_sha256: str | None = None
    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_microusd: int = 0


def _node_for(view: RunView, envelope: EventEnvelope) -> tuple[str, NodeView]:
    node_id = envelope.node_id
    if node_id is None:
        raise ReplayError(f"event {envelope.type} at seq {envelope.seq} requires node_id")
    return node_id, view.nodes.setdefault(node_id, NodeView())


def _require_run_level(envelope: EventEnvelope) -> None:
    if envelope.node_id is not None:
        raise ReplayError(
            f"run-level event {envelope.type} at seq {envelope.seq} must not carry node_id"
        )


@dataclass(slots=True)
class _CausalState:
    """Fold-local correlation state; never part of the derived views."""

    expected_nodes: int = 0
    open_dispatches: set[tuple[str, str, str]] = field(default_factory=set)
    seen_operation_ids: set[str] = field(default_factory=set)
    seen_call_ids: set[str] = field(default_factory=set)
    artifact_hashes: set[str] = field(default_factory=set)
    node_roles: dict[str, str] = field(default_factory=dict)
    last_output_sha: dict[str, str] = field(default_factory=dict)


def _check_terminal(
    view: RunView, payload: RunFinished, causal: _CausalState, seq: int
) -> RunStatus:
    try:
        target = RunStatus(payload.status)
    except ValueError as exc:
        raise ReplayError(
            f"run.finished at seq {seq} has unknown status {payload.status!r}"
        ) from exc
    if target not in RUN_TERMINAL_STATUSES:
        raise ReplayError(f"run.finished status {payload.status!r} is not terminal")
    if (
        payload.calls != view.total_calls
        or payload.input_tokens != view.total_input_tokens
        or payload.output_tokens != view.total_output_tokens
        or payload.cost_microusd != view.total_cost_microusd
    ):
        raise ReplayError(
            "run.finished totals do not match accumulated "
            "model.completed + billed model.failed usage"
        )
    if len(view.nodes) != causal.expected_nodes:
        raise ReplayError(
            f"run.finished with {len(view.nodes)} tracked nodes; run.created "
            f"declared {causal.expected_nodes}"
        )
    nonterminal = sorted(
        node_id
        for node_id, node in view.nodes.items()
        if node.status not in (NodeStatus.COMPLETED, NodeStatus.FAILED)
    )
    if nonterminal:
        raise ReplayError(f"run.finished while nodes {nonterminal!r} are not terminal")
    failed = sorted(
        node_id for node_id, node in view.nodes.items() if node.status is NodeStatus.FAILED
    )
    if target is RunStatus.COMPLETED:
        if failed:
            raise ReplayError(f"completed run contains failed nodes {failed!r}")
        if payload.reason is not None:
            raise ReplayError("completed run must not carry a failure reason")
        if causal.open_dispatches:
            raise ReplayError(
                "completed run leaves model dispatches without matching completions"
            )
        if (
            view.total_calls > view.caps.max_calls
            or view.total_input_tokens + view.total_output_tokens
            > view.caps.max_total_tokens
            or view.total_cost_microusd > view.caps.max_cost_microusd
        ):
            raise ReplayError("completed run exceeds its hard budget caps")
    else:
        if payload.reason is None:
            raise ReplayError("failed run requires a failure reason")
        if not failed:
            raise ReplayError("failed run contains no failed node")
    return target


def fold_envelopes(
    envelopes: Iterable[EventEnvelope], *, require_terminal: bool = True
) -> RunView:
    view: RunView | None = None
    causal = _CausalState()
    expected_seq = 1
    terminal_seen = False

    for envelope in envelopes:
        if envelope.seq != expected_seq:
            raise ReplayError(
                f"gapless seq violation: expected {expected_seq}, got {envelope.seq}"
            )
        expected_seq += 1
        if terminal_seen:
            raise ReplayError(f"event {envelope.type} at seq {envelope.seq} after terminal event")
        payload = decode_payload(envelope.type, envelope.data)

        try:
            if isinstance(payload, RunCreated):
                if view is not None:
                    raise ReplayError(f"duplicate run.created at seq {envelope.seq}")
                _require_run_level(envelope)
                if payload.node_count < 1:
                    raise ReplayError(
                        f"run.created declares node_count {payload.node_count}"
                    )
                causal.expected_nodes = payload.node_count
                view = RunView(
                    run_id=envelope.run_id,
                    engine_version=payload.engine_version,
                    task_sha256=payload.task_sha256,
                    plan_sha256=payload.plan_sha256,
                    fixture_sha256=payload.fixture_sha256,
                    caps=Budget(
                        max_calls=payload.max_calls,
                        max_total_tokens=payload.max_total_tokens,
                        max_cost_microusd=payload.max_cost_microusd,
                    ),
                    status=advance_run_status(RunStatus.CREATED, RunStatus.RUNNING),
                )
                view.last_seq = envelope.seq
                continue

            if view is None:
                raise ReplayError(
                    f"first event must be run.created, got {envelope.type} at seq {envelope.seq}"
                )
            if envelope.run_id != view.run_id:
                raise ReplayError(
                    f"run_id mismatch at seq {envelope.seq}: "
                    f"{envelope.run_id!r} != {view.run_id!r}"
                )
            view.last_seq = envelope.seq

            if isinstance(payload, NodeStarted):
                node_id, node = _node_for(view, envelope)
                node.status = advance_node_status(node.status, NodeStatus.RUNNING)
                causal.node_roles[node_id] = payload.role
            elif isinstance(payload, ArtifactWritten):
                node_id, node = _node_for(view, envelope)
                if node.status is not NodeStatus.RUNNING:
                    raise ReplayError(
                        f"artifact.written for {node_id!r} while node is {node.status.value}"
                    )
                causal.artifact_hashes.add(payload.sha256)
                view.artifacts.append(
                    ArtifactView(
                        sha256=payload.sha256,
                        size=payload.size,
                        media_type=payload.media_type,
                        role=payload.role,
                        node_id=envelope.node_id,
                    )
                )
            elif isinstance(payload, ModelDispatched):
                node_id, node = _node_for(view, envelope)
                if node.status is not NodeStatus.RUNNING:
                    raise ReplayError(
                        f"model.dispatched for {node_id!r} while node is {node.status.value}"
                    )
                operation_id, call_id = envelope.operation_id, envelope.call_id
                if operation_id is None or call_id is None:
                    raise ReplayError(
                        f"model.dispatched at seq {envelope.seq} requires "
                        "operation_id and call_id"
                    )
                if operation_id in causal.seen_operation_ids:
                    raise ReplayError(f"operation_id {operation_id!r} reused at dispatch")
                if call_id in causal.seen_call_ids:
                    raise ReplayError(f"call_id {call_id!r} reused at dispatch")
                if payload.request_sha256 not in causal.artifact_hashes:
                    raise ReplayError(
                        f"model.dispatched for {node_id!r} references unwritten "
                        f"request artifact {payload.request_sha256!r}"
                    )
                causal.seen_operation_ids.add(operation_id)
                causal.seen_call_ids.add(call_id)
                causal.open_dispatches.add((node_id, operation_id, call_id))
            elif isinstance(payload, ModelCompleted):
                node_id, node = _node_for(view, envelope)
                if node.status is not NodeStatus.RUNNING:
                    raise ReplayError(
                        f"model.completed for {node_id!r} while node is {node.status.value}"
                    )
                operation_id, call_id = envelope.operation_id, envelope.call_id
                if operation_id is None or call_id is None:
                    raise ReplayError(
                        f"model.completed at seq {envelope.seq} requires "
                        "operation_id and call_id"
                    )
                key = (node_id, operation_id, call_id)
                if key not in causal.open_dispatches:
                    raise ReplayError(
                        f"model.completed for {node_id!r} has no matching open "
                        f"dispatch (operation {operation_id!r}, call {call_id!r})"
                    )
                causal.open_dispatches.remove(key)
                if payload.output_sha256 not in causal.artifact_hashes:
                    raise ReplayError(
                        f"model.completed for {node_id!r} references unwritten "
                        f"output artifact {payload.output_sha256!r}"
                    )
                if (
                    payload.input_tokens < 0
                    or payload.output_tokens < 0
                    or payload.cost_microusd < 0
                ):
                    raise ReplayError(
                        f"model.completed for {node_id!r} carries negative usage or cost"
                    )
                causal.last_output_sha[node_id] = payload.output_sha256
                node.calls += 1
                node.input_tokens += payload.input_tokens
                node.output_tokens += payload.output_tokens
                node.cost_microusd += payload.cost_microusd
                view.total_calls += 1
                view.total_input_tokens += payload.input_tokens
                view.total_output_tokens += payload.output_tokens
                view.total_cost_microusd += payload.cost_microusd
            elif isinstance(payload, ModelFailed):
                node_id, node = _node_for(view, envelope)
                if node.status is not NodeStatus.RUNNING:
                    raise ReplayError(
                        f"model.failed for {node_id!r} while node is {node.status.value}"
                    )
                operation_id, call_id = envelope.operation_id, envelope.call_id
                if operation_id is None or call_id is None:
                    raise ReplayError(
                        f"model.failed at seq {envelope.seq} requires "
                        "operation_id and call_id"
                    )
                key = (node_id, operation_id, call_id)
                if key not in causal.open_dispatches:
                    raise ReplayError(
                        f"model.failed for {node_id!r} has no matching open "
                        f"dispatch (operation {operation_id!r}, call {call_id!r})"
                    )
                causal.open_dispatches.remove(key)
                if payload.reason not in BACKEND_DISPATCH_KINDS:
                    raise ReplayError(
                        f"model.failed for {node_id!r} has unknown reason "
                        f"{payload.reason!r}"
                    )
                if payload.usage_source not in USAGE_SOURCES:
                    raise ReplayError(
                        f"model.failed for {node_id!r} has unknown usage_source "
                        f"{payload.usage_source!r}"
                    )
                if (
                    payload.billed_input_tokens < 0
                    or payload.billed_output_tokens < 0
                    or payload.billed_cost_microusd < 0
                ):
                    raise ReplayError(
                        f"model.failed for {node_id!r} carries negative billed amounts"
                    )
                node.calls += 1
                node.input_tokens += payload.billed_input_tokens
                node.output_tokens += payload.billed_output_tokens
                node.cost_microusd += payload.billed_cost_microusd
                view.total_calls += 1
                view.total_input_tokens += payload.billed_input_tokens
                view.total_output_tokens += payload.billed_output_tokens
                view.total_cost_microusd += payload.billed_cost_microusd
            elif isinstance(payload, NodeCompleted):
                node_id, node = _node_for(view, envelope)
                node.status = advance_node_status(node.status, NodeStatus.COMPLETED)
                if node.calls < 1:
                    raise ReplayError(
                        f"node.completed for {node_id!r} without a completed model call"
                    )
                if payload.output_sha256 != causal.last_output_sha.get(node_id):
                    raise ReplayError(
                        f"node.completed for {node_id!r} output does not match its "
                        "last model.completed output"
                    )
                if any(open_id == node_id for open_id, _, _ in causal.open_dispatches):
                    raise ReplayError(
                        f"node.completed for {node_id!r} while a dispatch is still open"
                    )
                node.output_sha256 = payload.output_sha256
            elif isinstance(payload, NodeFailed):
                node_id, node = _node_for(view, envelope)
                node.status = advance_node_status(node.status, NodeStatus.FAILED)
                if payload.reason not in NODE_FAILURE_REASONS:
                    raise ReplayError(
                        f"node.failed for {node_id!r} has unknown reason "
                        f"{payload.reason!r}"
                    )
                node.reason = payload.reason
            elif isinstance(payload, BudgetRejected):
                node_id, node = _node_for(view, envelope)
                if node.status is not NodeStatus.RUNNING:
                    raise ReplayError(
                        f"budget.rejected for {node_id!r} while node is {node.status.value}"
                    )
            elif isinstance(payload, DecisionRecorded):
                _require_run_level(envelope)
                if view.decision is not None:
                    raise ReplayError(f"duplicate decision.recorded at seq {envelope.seq}")
                if payload.decision_version != DECISION_VERSION:
                    raise ReplayError(
                        f"decision.recorded has unsupported decision_version "
                        f"{payload.decision_version}"
                    )
                judged = [
                    node_id
                    for node_id, role in causal.node_roles.items()
                    if role == ROLE_JUDGE
                    and view.nodes[node_id].status is NodeStatus.COMPLETED
                    and view.nodes[node_id].output_sha256 == payload.source_sha256
                ]
                if not judged:
                    raise ReplayError(
                        "decision.recorded requires a completed judge node whose "
                        f"output is {payload.source_sha256!r}"
                    )
                view.decision = Decision(
                    decision_version=payload.decision_version,
                    winner=payload.winner,
                    rationale=payload.rationale,
                )
                view.decision_source_sha256 = payload.source_sha256
            elif isinstance(payload, RunFinished):
                _require_run_level(envelope)
                target = _check_terminal(view, payload, causal, envelope.seq)
                view.status = advance_run_status(view.status, target)
                view.reason = payload.reason
                view.finished_ts = envelope.ts
                terminal_seen = True
        except TransitionError as exc:
            raise ReplayError(f"seq {envelope.seq}: {exc}") from exc

    if view is None:
        raise ReplayError("empty journal")
    if require_terminal and not terminal_seen:
        raise ReplayError("journal has no terminal run.finished event")
    return view


def fold_frames(frames: Iterable[bytes], *, require_terminal: bool = True) -> RunView:
    return fold_envelopes(
        (frame_to_envelope(frame) for frame in frames), require_terminal=require_terminal
    )


def _decision_report_doc(view: RunView) -> dict[str, object] | None:
    if view.decision is None:
        return None
    return {
        "decision_version": view.decision.decision_version,
        "winner": view.decision.winner,
        "rationale": view.decision.rationale,
        "source_sha256": view.decision_source_sha256,
    }


def rebuild_report(view: RunView) -> dict[str, object]:
    """Build the RunReport document. Derivable from the journal alone."""
    return {
        "report_version": REPORT_VERSION,
        "run_id": view.run_id,
        "engine_version": view.engine_version,
        "status": view.status.value,
        "reason": view.reason,
        "task_sha256": view.task_sha256,
        "plan_sha256": view.plan_sha256,
        "fixture_sha256": view.fixture_sha256,
        "finished_ts": view.finished_ts,
        "last_seq": view.last_seq,
        "nodes": {
            node_id: {
                "status": node.status.value,
                "reason": node.reason,
                "output_sha256": node.output_sha256,
                "calls": node.calls,
                "input_tokens": node.input_tokens,
                "output_tokens": node.output_tokens,
                "cost_microusd": node.cost_microusd,
            }
            for node_id, node in sorted(view.nodes.items())
        },
        "artifacts": [
            {
                "sha256": artifact.sha256,
                "size": artifact.size,
                "media_type": artifact.media_type,
                "role": artifact.role,
                "node_id": artifact.node_id,
            }
            for artifact in view.artifacts
        ],
        "budget": {
            "caps": build_budget_doc(view.caps),
            "committed": {
                "calls": view.total_calls,
                "input_tokens": view.total_input_tokens,
                "output_tokens": view.total_output_tokens,
                "cost_microusd": view.total_cost_microusd,
            },
        },
        "decision": _decision_report_doc(view),
    }


def derived_status_doc(view: RunView) -> dict[str, object]:
    """Small derived view for ``runs show`` (works for non-terminal journals)."""
    return {
        "status": view.status.value,
        "reason": view.reason,
        "last_seq": view.last_seq,
        "finished_ts": view.finished_ts,
        "nodes": {node_id: node.status.value for node_id, node in sorted(view.nodes.items())},
        "decision": _decision_report_doc(view),
    }
