"""Dagvane domain model: frozen dataclasses, state machines, closed event registry.

This module performs no I/O and imports nothing outside the standard library's
data-modelling facilities. Serialization of these types happens at the protocol
boundary (``dagvane.protocol``).
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, TypeVar

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DagvaneError(Exception):
    """Base class for all engine errors."""


class SpecError(DagvaneError):
    """Invalid external input document (task file, fixture file, decision text)."""


class PlanValidationError(DagvaneError):
    """A plan violates the structural council contract."""


class BudgetRejectedError(DagvaneError):
    """Budget admission rejected a dispatch before backend invocation."""

    def __init__(self, *, dimension: str, requested: int, used: int, cap: int) -> None:
        self.dimension = dimension
        self.requested = requested
        self.used = used
        self.cap = cap
        super().__init__(
            f"budget rejected on {dimension}: requested {requested}, used {used}, cap {cap}"
        )


class BudgetExceededError(DagvaneError):
    """Committed actual usage pushed totals beyond a hard cap (post-hoc breach).

    The actual usage is still recorded honestly in the ledger and journal; the
    run must fail rather than complete successfully above its configured caps.
    """

    def __init__(self, *, dimension: str, committed: int, cap: int) -> None:
        self.dimension = dimension
        self.committed = committed
        self.cap = cap
        super().__init__(
            f"budget exceeded on {dimension}: committed {committed} above cap {cap}"
        )


class BackendError(DagvaneError):
    """A chat backend failed to produce a result."""


# Closed set of normalized live-dispatch failure kinds (G1).
DISPATCH_KIND_AUTH = "auth"
DISPATCH_KIND_API = "api"
DISPATCH_KIND_RATE_LIMIT = "rate_limit"
DISPATCH_KIND_TIMEOUT = "timeout"
DISPATCH_KIND_CONNECTION = "connection"
DISPATCH_KIND_PROTOCOL = "protocol"
DISPATCH_KIND_USAGE_MISSING = "usage_missing"
# A dispatch cancelled after it may have reached the provider: the ambiguous
# potentially-sent state is closed durably at the reservation ceiling.
DISPATCH_KIND_CANCELLED = "cancelled"

BACKEND_DISPATCH_KINDS: frozenset[str] = frozenset(
    {
        DISPATCH_KIND_AUTH,
        DISPATCH_KIND_API,
        DISPATCH_KIND_RATE_LIMIT,
        DISPATCH_KIND_TIMEOUT,
        DISPATCH_KIND_CONNECTION,
        DISPATCH_KIND_PROTOCOL,
        DISPATCH_KIND_USAGE_MISSING,
        DISPATCH_KIND_CANCELLED,
    }
)


class BackendDispatchError(BackendError):
    """A normalized live-dispatch failure with honest billing semantics.

    ``billed=True`` means the provider may have processed (and billed) the
    request — the caller must commit the dispatch against the budget instead
    of releasing its reservation. ``usage`` carries whatever per-component
    usage the provider reported for the failed call (possibly partial); a
    known component must never be discarded because another one is missing.
    Messages must already be redacted by the adapter that raises this.
    """

    def __init__(
        self,
        *,
        kind: str,
        message: str,
        billed: bool,
        usage: PartialUsage | None = None,
        receipt: InvocationReceipt | None = None,
    ) -> None:
        if kind not in BACKEND_DISPATCH_KINDS:
            raise ValueError(f"unknown dispatch failure kind {kind!r}")
        self.kind = kind
        self.billed = billed
        self.usage = usage
        self.receipt = receipt
        super().__init__(f"{kind}: {message}")


class StorageError(DagvaneError):
    """Durable storage invariant violation (run dir, journal, artifacts)."""


class ProtocolError(DagvaneError):
    """Malformed protocol frame or event payload."""


class ReplayError(DagvaneError):
    """The event journal violates the run event contract."""


class TransitionError(DagvaneError):
    """Illegal run/node state machine transition."""


# ---------------------------------------------------------------------------
# State machines
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


RUN_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED}
)

RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING}),
    RunStatus.RUNNING: frozenset({RunStatus.COMPLETED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
}


def advance_run_status(current: RunStatus, target: RunStatus) -> RunStatus:
    if target not in RUN_TRANSITIONS[current]:
        raise TransitionError(f"illegal run transition {current.value} -> {target.value}")
    return target


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


NODE_TRANSITIONS: Mapping[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.RUNNING, NodeStatus.FAILED}),
    NodeStatus.RUNNING: frozenset({NodeStatus.COMPLETED, NodeStatus.FAILED}),
    NodeStatus.COMPLETED: frozenset(),
    NodeStatus.FAILED: frozenset(),
}


def advance_node_status(current: NodeStatus, target: NodeStatus) -> NodeStatus:
    if target not in NODE_TRANSITIONS[current]:
        raise TransitionError(f"illegal node transition {current.value} -> {target.value}")
    return target


# Closed set of node failure reasons (G0 set + the G1 cancellation reason).
REASON_DEPENDENCY_FAILED = "dependency_failed"
REASON_BUDGET_REJECTED = "budget_rejected"
REASON_BUDGET_EXCEEDED = "budget_exceeded"
REASON_BACKEND_ERROR = "backend_error"
REASON_INVALID_DECISION = "invalid_decision"
REASON_UNEXPECTED_ERROR = "unexpected_error"
REASON_CANCELLED = "cancelled"

NODE_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_DEPENDENCY_FAILED,
        REASON_BUDGET_REJECTED,
        REASON_BUDGET_EXCEEDED,
        REASON_BACKEND_ERROR,
        REASON_INVALID_DECISION,
        REASON_UNEXPECTED_ERROR,
        REASON_CANCELLED,
    }
)


# ---------------------------------------------------------------------------
# Core value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Budget:
    """Multidimensional hard caps. Money is integer micro-USD; never floats."""

    max_calls: int
    max_total_tokens: int
    max_cost_microusd: int


@dataclass(frozen=True, slots=True)
class BudgetOverrides:
    """Optional per-task overrides applied over template defaults."""

    max_calls: int | None = None
    max_total_tokens: int | None = None
    max_cost_microusd: int | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class PartialUsage:
    """Per-component provider-reported usage where any component may be unknown.

    ``None`` means the provider did not report that component — it is *unknown*,
    not zero. Accounting substitutes the conservative reservation ceiling for
    unknown components only; a known component is committed exactly as reported.
    """

    input_tokens: int | None
    output_tokens: int | None

    def is_empty(self) -> bool:
        return self.input_tokens is None and self.output_tokens is None

    def is_complete(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate used for reservations and fake usage: ceil(len/4)."""
    return (len(text) + 3) // 4


@dataclass(frozen=True, slots=True)
class Pricing:
    """Pinned per-route pricing in micro-USD per million tokens."""

    input_microusd_per_mtok: int
    output_microusd_per_mtok: int


@dataclass(frozen=True, slots=True)
class ModelRoute:
    route_id: str
    model: str
    backend: str
    pricing: Pricing
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class InvocationReceipt:
    """Physical-invocation provenance reported by a live backend adapter."""

    backend_kind: str
    connection_id: str
    provider_request_id: str | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    title: str
    statement: str
    acceptance_criteria: tuple[str, ...]
    budget_overrides: BudgetOverrides


ENTRY_KIND_TASK = "task"
ENTRY_KIND_PROPOSAL = "proposal"
ENTRY_KIND_REVIEW = "review"
ENTRY_KINDS: frozenset[str] = frozenset(
    {ENTRY_KIND_TASK, ENTRY_KIND_PROPOSAL, ENTRY_KIND_REVIEW}
)


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One input of a plan node.

    ``producer`` is the node id whose output artifact backs this entry (None
    for the task itself). The producer identity is structural metadata: it is
    recorded in the sealed run manifest and used by validation, and is never
    rendered into any model-visible context.
    """

    kind: str
    label: str
    producer: str | None


@dataclass(frozen=True, slots=True)
class InputManifest:
    entries: tuple[ManifestEntry, ...]


ROLE_PROPOSER = "proposer"
ROLE_REVIEWER = "reviewer"
ROLE_JUDGE = "judge"


@dataclass(frozen=True, slots=True)
class PlanNode:
    node_id: str
    role: str
    identity: str
    route_id: str
    depends_on: tuple[str, ...]
    input_manifest: InputManifest


@dataclass(frozen=True, slots=True)
class Plan:
    plan_version: int
    nodes: tuple[PlanNode, ...]
    anonymization: Mapping[str, str]  # sealed: candidate label -> producer node id

    def __post_init__(self) -> None:
        # Seal the mapping deeply: a private copy behind an immutable view, so
        # no caller-held reference can mutate the anonymization after build.
        object.__setattr__(
            self, "anonymization", types.MappingProxyType(dict(self.anonymization))
        )


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    task: TaskSpec
    plan: Plan
    routes: Mapping[str, ModelRoute]
    budget: Budget
    created_ts: str


@dataclass(frozen=True, slots=True)
class Attempt:
    node_id: str
    index: int


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    sha256: str
    size: int
    media_type: str
    role: str


@dataclass(frozen=True, slots=True)
class Decision:
    decision_version: int
    winner: str
    rationale: str


# ---------------------------------------------------------------------------
# Event envelope and closed payload registry
# ---------------------------------------------------------------------------

ENVELOPE_VERSION = 1


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    v: int
    event_id: str
    run_id: str
    seq: int
    ts: str
    node_id: str | None
    attempt: int | None
    operation_id: str | None
    call_id: str | None
    type: str
    data: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class RunCreated:
    TYPE: ClassVar[str] = "run.created"
    engine_version: str
    task_sha256: str
    plan_sha256: str
    fixture_sha256: str
    node_count: int
    max_calls: int
    max_total_tokens: int
    max_cost_microusd: int


@dataclass(frozen=True, slots=True)
class NodeStarted:
    TYPE: ClassVar[str] = "node.started"
    role: str
    route_id: str


@dataclass(frozen=True, slots=True)
class ArtifactWritten:
    TYPE: ClassVar[str] = "artifact.written"
    sha256: str
    size: int
    media_type: str
    role: str


@dataclass(frozen=True, slots=True)
class ModelDispatched:
    TYPE: ClassVar[str] = "model.dispatched"
    route_id: str
    model: str
    request_sha256: str
    reserved_calls: int
    reserved_tokens: int
    reserved_cost_microusd: int


@dataclass(frozen=True, slots=True)
class ModelCompleted:
    TYPE: ClassVar[str] = "model.completed"
    model: str
    input_tokens: int
    output_tokens: int
    cost_microusd: int
    output_sha256: str


# How a billed failed dispatch attributed its committed usage.
USAGE_SOURCE_PROVIDER = "provider"  # provider reported every component
USAGE_SOURCE_CEILING = "ceiling"  # no component known: full reservation ceiling
USAGE_SOURCE_MIXED = "mixed"  # known components as reported, unknown at ceiling
USAGE_SOURCES: frozenset[str] = frozenset(
    {USAGE_SOURCE_PROVIDER, USAGE_SOURCE_CEILING, USAGE_SOURCE_MIXED}
)


@dataclass(frozen=True, slots=True)
class ModelFailed:
    """A billed dispatch failure (G1 live backends): closes its open dispatch.

    Emitted only when the failed call was (possibly) billed by the provider;
    the billed amounts are committed against the run budget. Non-billed
    failures keep the G0 shape: released reservation, no ``model.failed``.
    """

    TYPE: ClassVar[str] = "model.failed"
    reason: str  # a BACKEND_DISPATCH_KINDS member
    message: str
    billed_input_tokens: int
    billed_output_tokens: int
    billed_cost_microusd: int
    usage_source: str  # a USAGE_SOURCES member


@dataclass(frozen=True, slots=True)
class NodeCompleted:
    TYPE: ClassVar[str] = "node.completed"
    output_sha256: str


@dataclass(frozen=True, slots=True)
class NodeFailed:
    TYPE: ClassVar[str] = "node.failed"
    reason: str
    message: str


@dataclass(frozen=True, slots=True)
class BudgetRejected:
    TYPE: ClassVar[str] = "budget.rejected"
    route_id: str
    dimension: str
    requested: int
    used: int
    cap: int


@dataclass(frozen=True, slots=True)
class DecisionRecorded:
    TYPE: ClassVar[str] = "decision.recorded"
    decision_version: int
    winner: str
    rationale: str
    source_sha256: str


@dataclass(frozen=True, slots=True)
class RunFinished:
    TYPE: ClassVar[str] = "run.finished"
    status: str
    reason: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_microusd: int


EventPayload = (
    RunCreated
    | NodeStarted
    | ArtifactWritten
    | ModelDispatched
    | ModelCompleted
    | ModelFailed
    | NodeCompleted
    | NodeFailed
    | BudgetRejected
    | DecisionRecorded
    | RunFinished
)

EVENT_REGISTRY: Mapping[str, type[EventPayload]] = {
    cls.TYPE: cls
    for cls in (
        RunCreated,
        NodeStarted,
        ArtifactWritten,
        ModelDispatched,
        ModelCompleted,
        ModelFailed,
        NodeCompleted,
        NodeFailed,
        BudgetRejected,
        DecisionRecorded,
        RunFinished,
    )
}

RUN_FINISHED_STATUSES: frozenset[str] = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value}
)

_P = TypeVar("_P")


def payload_to_data(payload: EventPayload) -> dict[str, object]:
    """Serialize a payload to a plain JSON-able mapping (flat scalars only)."""
    return dataclasses.asdict(payload)


def _check_field(name: str, value: object, annotation: object) -> object:
    if annotation is str:
        if not isinstance(value, str):
            raise ValueError(f"field {name!r} must be a string")
        return value
    if annotation is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"field {name!r} must be an integer")
        return value
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = set(typing.get_args(annotation))
        if args == {str, type(None)}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"field {name!r} must be a string or null")
            return value
        if args == {int, type(None)}:
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"field {name!r} must be an integer or null")
            return value
    raise ValueError(f"field {name!r} has unsupported payload type {annotation!r}")


def payload_from_data(cls: type[_P], data: Mapping[str, object]) -> _P:
    """Strictly decode a payload mapping: exact key set, exact scalar types."""
    hints = typing.get_type_hints(cls)
    field_names = [f.name for f in dataclasses.fields(cls)]  # type: ignore[arg-type]
    if set(data.keys()) != set(field_names):
        raise ValueError(
            f"payload keys {sorted(data.keys())!r} do not match fields {sorted(field_names)!r}"
        )
    kwargs = {name: _check_field(name, data[name], hints[name]) for name in field_names}
    return cls(**kwargs)


def decode_payload(event_type: str, data: Mapping[str, object]) -> EventPayload:
    """Decode ``data`` for ``event_type`` against the closed registry."""
    cls = EVENT_REGISTRY.get(event_type)
    if cls is None:
        raise ProtocolError(f"unknown event type {event_type!r}")
    try:
        return payload_from_data(cls, data)
    except ValueError as exc:
        raise ProtocolError(f"malformed {event_type} payload: {exc}") from exc
