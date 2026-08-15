"""The council-v1 engine: fixed template, validator, budget ledger, worker, executor.

Consolidated application module for the G0 walking skeleton (per the approved
plan): everything that turns a TaskSpec into a durable, judged council run.
The executor knows no concrete backend or store — it works against the ports.

Durable-event ordering per action: serialize → journal append (fsync) → frame
to the output sink → proceed. The terminal ``run.finished`` event is the last
event of the run; ``report.json``/``decision.json`` are derived views written
after it via the same fold used for replay.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from dagvane import __version__
from dagvane.application.replay import fold_envelopes, rebuild_report
from dagvane.domain.models import (
    DISPATCH_KIND_CANCELLED,
    DISPATCH_KIND_PROTOCOL,
    ENTRY_KIND_PROPOSAL,
    ENTRY_KIND_REVIEW,
    ENTRY_KIND_TASK,
    ENTRY_KINDS,
    ENVELOPE_VERSION,
    REASON_BACKEND_ERROR,
    REASON_BUDGET_EXCEEDED,
    REASON_BUDGET_REJECTED,
    REASON_CANCELLED,
    REASON_DEPENDENCY_FAILED,
    REASON_INVALID_DECISION,
    REASON_UNEXPECTED_ERROR,
    ROLE_JUDGE,
    ROLE_PROPOSER,
    ROLE_REVIEWER,
    USAGE_SOURCE_CEILING,
    USAGE_SOURCE_MIXED,
    USAGE_SOURCE_PROVIDER,
    ArtifactRef,
    ArtifactWritten,
    Attempt,
    BackendDispatchError,
    BackendError,
    Budget,
    BudgetExceededError,
    BudgetRejected,
    BudgetRejectedError,
    Decision,
    DecisionRecorded,
    EventEnvelope,
    EventPayload,
    InputManifest,
    InvocationReceipt,
    ManifestEntry,
    ModelCompleted,
    ModelDispatched,
    ModelFailed,
    ModelRoute,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    NodeStatus,
    PartialUsage,
    Plan,
    PlanNode,
    PlanValidationError,
    Pricing,
    Run,
    RunCreated,
    RunFinished,
    RunStatus,
    SpecError,
    StorageError,
    TaskSpec,
    Usage,
    advance_run_status,
    estimate_tokens,
    payload_to_data,
)
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.backend import ChatBackend, PreparedRequest
from dagvane.ports.runtime import Clock, FixedClock, IdSource, SequentialIds, SystemClock, SystemIds
from dagvane.ports.storage import ArtifactStore, RunStore
from dagvane.protocol.documents import (
    FixtureSpec,
    LoadedTask,
    build_budget_doc,
    build_decision_doc,
    build_manifest_doc,
    build_plan_doc,
    build_routes_doc,
    parse_decision,
)
from dagvane.protocol.frames import canonical_json_bytes, sha256_hex
from dagvane.protocol.profiles import COUNCIL_ROLE_SLOTS, ProfileSpec

FrameSink = Callable[[bytes, EventEnvelope], None]

COUNCIL_PLAN_VERSION = 1

# Journaled failure text must never grow a frame toward the 1 MiB protocol
# limit; adapters bound their messages at ~2000 chars, this is defense in
# depth for every other error source.
_MAX_EVENT_MESSAGE_CHARS = 4000


def _bound_event_message(message: str) -> str:
    if len(message) <= _MAX_EVENT_MESSAGE_CHARS:
        return message
    over = len(message) - _MAX_EVENT_MESSAGE_CHARS
    return message[:_MAX_EVENT_MESSAGE_CHARS] + f"... [truncated {over} chars]"

# Pinned fake-route pricing (micro-USD per million tokens) and limits.
_FAKE_PRICING = Pricing(input_microusd_per_mtok=3_000_000, output_microusd_per_mtok=15_000_000)
_FAKE_MAX_OUTPUT_TOKENS = 2048

# Template default caps, overridable per task (Round 4 council defaults).
DEFAULT_COUNCIL_BUDGET = Budget(
    max_calls=60, max_total_tokens=2_000_000, max_cost_microusd=8_000_000
)

SYSTEM_PROMPTS: Mapping[str, str] = {
    ROLE_PROPOSER: (
        "You are one of several independent proposers in a Dagvane council. "
        "Produce your best independent proposal for the task. "
        "Do not assume any other proposal exists."
    ),
    ROLE_REVIEWER: (
        "You are a blind reviewer in a Dagvane council. Review each candidate "
        "proposal strictly on its merits. Candidate authorship is hidden."
    ),
    ROLE_JUDGE: (
        "You are the judge in a Dagvane council. Weigh the candidate proposals "
        "and their reviews, then output a JSON object with exactly these keys: "
        'decision_version (must be 1), winner (a candidate label such as "candidate-1"), '
        "and rationale (a non-empty string)."
    ),
}


# ---------------------------------------------------------------------------
# Fixed council template
# ---------------------------------------------------------------------------


class CouncilTemplate:
    """The fixed council-v1 plan: 2 proposers → barrier → blind cross-review → judge."""

    @staticmethod
    def resolve_budget(task: TaskSpec) -> Budget:
        overrides = task.budget_overrides
        return Budget(
            max_calls=overrides.max_calls or DEFAULT_COUNCIL_BUDGET.max_calls,
            max_total_tokens=(
                overrides.max_total_tokens or DEFAULT_COUNCIL_BUDGET.max_total_tokens
            ),
            max_cost_microusd=(
                overrides.max_cost_microusd or DEFAULT_COUNCIL_BUDGET.max_cost_microusd
            ),
        )

    @staticmethod
    def fake_role_routes() -> dict[str, ModelRoute]:
        """The G0 deterministic routes, one per council role slot."""

        def route(name: str) -> ModelRoute:
            return ModelRoute(
                route_id=f"fake/{name}",
                model=f"fake-{name}",
                backend="fake",
                pricing=_FAKE_PRICING,
                max_output_tokens=_FAKE_MAX_OUTPUT_TOKENS,
            )

        return {
            "proposer_a": route("proposer-a"),
            "proposer_b": route("proposer-b"),
            "reviewer_a": route("reviewer-a"),
            "reviewer_b": route("reviewer-b"),
            "judge": route("judge"),
        }

    @staticmethod
    def build(
        task: TaskSpec, role_routes: Mapping[str, ModelRoute] | None = None
    ) -> tuple[Plan, dict[str, ModelRoute], Budget]:
        if role_routes is None:
            role_routes = CouncilTemplate.fake_role_routes()
        if set(role_routes.keys()) != set(COUNCIL_ROLE_SLOTS):
            raise PlanValidationError(
                f"council role routes must cover exactly {sorted(COUNCIL_ROLE_SLOTS)!r}, "
                f"got {sorted(role_routes.keys())!r}"
            )
        routes = {r.route_id: r for r in role_routes.values()}
        task_entry = ManifestEntry(kind=ENTRY_KIND_TASK, label="task", producer=None)
        candidate_1 = ManifestEntry(
            kind=ENTRY_KIND_PROPOSAL, label="candidate-1", producer="proposer-a"
        )
        candidate_2 = ManifestEntry(
            kind=ENTRY_KIND_PROPOSAL, label="candidate-2", producer="proposer-b"
        )
        nodes = (
            PlanNode(
                node_id="proposer-a",
                role=ROLE_PROPOSER,
                identity="A",
                route_id=role_routes["proposer_a"].route_id,
                depends_on=(),
                input_manifest=InputManifest(entries=(task_entry,)),
            ),
            PlanNode(
                node_id="proposer-b",
                role=ROLE_PROPOSER,
                identity="B",
                route_id=role_routes["proposer_b"].route_id,
                depends_on=(),
                input_manifest=InputManifest(entries=(task_entry,)),
            ),
            # The hard barrier: every review depends on *all* proposers, while its
            # manifest exposes only the sibling identity's proposal (blind, no self).
            PlanNode(
                node_id="review-by-a",
                role=ROLE_REVIEWER,
                identity="A",
                route_id=role_routes["reviewer_a"].route_id,
                depends_on=("proposer-a", "proposer-b"),
                input_manifest=InputManifest(entries=(task_entry, candidate_2)),
            ),
            PlanNode(
                node_id="review-by-b",
                role=ROLE_REVIEWER,
                identity="B",
                route_id=role_routes["reviewer_b"].route_id,
                depends_on=("proposer-a", "proposer-b"),
                input_manifest=InputManifest(entries=(task_entry, candidate_1)),
            ),
            PlanNode(
                node_id="judge",
                role=ROLE_JUDGE,
                identity="J",
                route_id=role_routes["judge"].route_id,
                depends_on=("review-by-a", "review-by-b"),
                input_manifest=InputManifest(
                    entries=(
                        task_entry,
                        candidate_1,
                        candidate_2,
                        ManifestEntry(
                            kind=ENTRY_KIND_REVIEW,
                            label="review-of-candidate-1",
                            producer="review-by-b",
                        ),
                        ManifestEntry(
                            kind=ENTRY_KIND_REVIEW,
                            label="review-of-candidate-2",
                            producer="review-by-a",
                        ),
                    )
                ),
            ),
        )
        plan = Plan(
            plan_version=COUNCIL_PLAN_VERSION,
            nodes=nodes,
            anonymization={"candidate-1": "proposer-a", "candidate-2": "proposer-b"},
        )
        return plan, routes, CouncilTemplate.resolve_budget(task)


# ---------------------------------------------------------------------------
# Plan validation (structural council contract)
# ---------------------------------------------------------------------------


class PlanValidator:
    def validate(self, plan: Plan, routes: Mapping[str, ModelRoute]) -> None:
        nodes = {node.node_id: node for node in plan.nodes}
        if len(nodes) != len(plan.nodes):
            raise PlanValidationError("node ids must be unique")

        for node in plan.nodes:
            for dep in node.depends_on:
                if dep not in nodes:
                    raise PlanValidationError(
                        f"node {node.node_id!r} depends on unknown node {dep!r}"
                    )
            if node.route_id not in routes:
                raise PlanValidationError(
                    f"node {node.node_id!r} references unknown route {node.route_id!r}"
                )

        sorter = TopologicalSorter({n.node_id: set(n.depends_on) for n in plan.nodes})
        try:
            sorter.prepare()
        except CycleError as exc:
            raise PlanValidationError(f"plan has a dependency cycle: {exc}") from exc

        ancestors = {node_id: self._ancestors(node_id, nodes) for node_id in nodes}
        proposers = [n for n in plan.nodes if n.role == ROLE_PROPOSER]
        reviewers = [n for n in plan.nodes if n.role == ROLE_REVIEWER]
        judges = [n for n in plan.nodes if n.role == ROLE_JUDGE]
        # Exact council-v1 topology: two proposers, two opposite reviewers, one judge.
        if len(proposers) != 2:
            raise PlanValidationError(
                f"council-v1 requires exactly two proposers, found {len(proposers)}"
            )
        if len(reviewers) != 2:
            raise PlanValidationError(
                f"council-v1 requires exactly two reviewers, found {len(reviewers)}"
            )
        if len(judges) != 1:
            raise PlanValidationError(
                f"council-v1 requires exactly one judge, found {len(judges)}"
            )
        proposer_identities = {n.identity for n in proposers}
        if len(proposer_identities) != 2:
            raise PlanValidationError("proposers must carry two distinct identities")
        if {n.identity for n in reviewers} != proposer_identities:
            raise PlanValidationError(
                "reviewer identities must be exactly the proposer identities "
                "(one opposite reviewer per proposer)"
            )

        for node in plan.nodes:
            self._validate_manifest(node, nodes, ancestors[node.node_id])
            entries = node.input_manifest.entries
            if node.role == ROLE_PROPOSER:
                # Structural anti-anchoring: proposer context is the task alone.
                for entry in entries:
                    if entry.kind != ENTRY_KIND_TASK:
                        raise PlanValidationError(
                            f"proposer {node.node_id!r} manifest may only contain the task, "
                            f"found {entry.kind!r} entry {entry.label!r}"
                        )
                if len(entries) != 1:
                    raise PlanValidationError(
                        f"proposer {node.node_id!r} manifest must be exactly one task "
                        f"entry, found {len(entries)} entries"
                    )
            elif node.role == ROLE_REVIEWER:
                missing = [p.node_id for p in proposers if p.node_id not in node.depends_on]
                if missing:
                    raise PlanValidationError(
                        f"review node {node.node_id!r} must depend on all proposers "
                        f"(hard barrier); missing {missing!r}"
                    )
                reviewed = [e for e in entries if e.kind == ENTRY_KIND_PROPOSAL]
                tasks = [e for e in entries if e.kind == ENTRY_KIND_TASK]
                if len(tasks) != 1 or len(reviewed) != 1 or len(entries) != 2:
                    raise PlanValidationError(
                        f"review node {node.node_id!r} manifest must be exactly the task "
                        "plus one opposite proposal"
                    )
                entry = reviewed[0]
                assert entry.producer is not None  # _validate_manifest guarantees this
                if nodes[entry.producer].identity == node.identity:
                    raise PlanValidationError(
                        f"self-review: node {node.node_id!r} (identity "
                        f"{node.identity!r}) would review its own identity's "
                        f"proposal {entry.producer!r}"
                    )
            elif node.role == ROLE_JUDGE:
                missing = [r.node_id for r in reviewers if r.node_id not in node.depends_on]
                if missing:
                    raise PlanValidationError(
                        f"judge {node.node_id!r} must depend on all reviews; "
                        f"missing {missing!r}"
                    )
                judged = [e for e in entries if e.kind == ENTRY_KIND_PROPOSAL]
                heard = [e for e in entries if e.kind == ENTRY_KIND_REVIEW]
                tasks = [e for e in entries if e.kind == ENTRY_KIND_TASK]
                if len(tasks) != 1 or len(entries) != 1 + len(proposers) + len(reviewers):
                    raise PlanValidationError(
                        f"judge {node.node_id!r} manifest must be exactly the task, "
                        "every proposal, and every review"
                    )
                if {e.producer for e in judged} != {p.node_id for p in proposers}:
                    raise PlanValidationError(
                        f"judge {node.node_id!r} must receive exactly one proposal "
                        "from every proposer"
                    )
                if {e.producer for e in heard} != {r.node_id for r in reviewers}:
                    raise PlanValidationError(
                        f"judge {node.node_id!r} must receive exactly one review "
                        "from every reviewer"
                    )
            else:
                raise PlanValidationError(
                    f"node {node.node_id!r} has unknown role {node.role!r}"
                )

        # Cross-manifest label consistency: a proposal label is one producer, everywhere.
        proposal_labels: dict[str, str] = {}
        for node in plan.nodes:
            for entry in node.input_manifest.entries:
                if entry.kind != ENTRY_KIND_PROPOSAL:
                    continue
                assert entry.producer is not None  # _validate_manifest guarantees this
                existing = proposal_labels.get(entry.label)
                if existing is not None and existing != entry.producer:
                    raise PlanValidationError(
                        f"proposal label {entry.label!r} maps to conflicting producers "
                        f"{existing!r} and {entry.producer!r}"
                    )
                proposal_labels[entry.label] = entry.producer

        # The sealed mapping must be an exact bijection: every key is a label the
        # council actually uses, every proposer has exactly one label, no extras.
        for label, producer in sorted(plan.anonymization.items()):
            if producer not in nodes or nodes[producer].role != ROLE_PROPOSER:
                raise PlanValidationError(
                    f"anonymization label {label!r} maps to non-proposer {producer!r}"
                )
        if set(plan.anonymization.keys()) != set(proposal_labels.keys()):
            raise PlanValidationError(
                f"sealed anonymization labels {sorted(plan.anonymization)!r} must be "
                f"exactly the proposal labels in use {sorted(proposal_labels)!r}"
            )
        for proposal_label, proposal_producer in proposal_labels.items():
            if plan.anonymization.get(proposal_label) != proposal_producer:
                raise PlanValidationError(
                    f"proposal label {proposal_label!r} is not consistent with the sealed "
                    "anonymization mapping"
                )
        if sorted(plan.anonymization.values()) != sorted(p.node_id for p in proposers):
            raise PlanValidationError(
                "sealed anonymization must map exactly one label to every proposer"
            )

    @staticmethod
    def _ancestors(node_id: str, nodes: Mapping[str, PlanNode]) -> frozenset[str]:
        seen: set[str] = set()
        stack = list(nodes[node_id].depends_on)
        while stack:
            current = stack.pop()
            if current in seen or current not in nodes:
                continue
            seen.add(current)
            stack.extend(nodes[current].depends_on)
        return frozenset(seen)

    @staticmethod
    def _validate_manifest(
        node: PlanNode, nodes: Mapping[str, PlanNode], ancestors: frozenset[str]
    ) -> None:
        labels = [entry.label for entry in node.input_manifest.entries]
        if len(labels) != len(set(labels)):
            raise PlanValidationError(f"node {node.node_id!r} manifest labels must be unique")
        for entry in node.input_manifest.entries:
            if entry.kind not in ENTRY_KINDS:
                raise PlanValidationError(
                    f"node {node.node_id!r} manifest entry {entry.label!r} has "
                    f"unknown kind {entry.kind!r}"
                )
            if entry.kind == ENTRY_KIND_TASK:
                if entry.producer is not None:
                    raise PlanValidationError(
                        f"task entry {entry.label!r} of node {node.node_id!r} "
                        "must not have a producer"
                    )
                continue
            if entry.producer is None:
                raise PlanValidationError(
                    f"{entry.kind} entry {entry.label!r} of node {node.node_id!r} "
                    "requires a producer"
                )
            if entry.producer not in ancestors:
                raise PlanValidationError(
                    f"node {node.node_id!r} manifest references {entry.producer!r}, "
                    "which is not an ancestor (inputs must be behind the barrier)"
                )
            expected_role = (
                ROLE_PROPOSER if entry.kind == ENTRY_KIND_PROPOSAL else ROLE_REVIEWER
            )
            if nodes[entry.producer].role != expected_role:
                raise PlanValidationError(
                    f"{entry.kind} entry {entry.label!r} of node {node.node_id!r} must be "
                    f"produced by a {expected_role}, got {nodes[entry.producer].role!r}"
                )


# ---------------------------------------------------------------------------
# Budget ledger (multidimensional reserve → commit under a lock)
# ---------------------------------------------------------------------------

DIMENSION_CALLS = "calls"
DIMENSION_TOKENS = "total_tokens"
DIMENSION_COST = "cost_microusd"


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    calls: int
    tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_microusd: int


@dataclass(frozen=True, slots=True)
class BudgetBreach:
    """A commit pushed honest committed totals beyond a hard cap."""

    dimension: str
    committed: int
    cap: int


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def dispatch_cost_microusd(input_tokens: int, output_tokens: int, pricing: Pricing) -> int:
    return _ceil_div(input_tokens * pricing.input_microusd_per_mtok, 1_000_000) + _ceil_div(
        output_tokens * pricing.output_microusd_per_mtok, 1_000_000
    )


class BudgetLedger:
    """Hard admission plus an honest commit postcondition.

    Every dispatch reserves atomically before backend invocation; actual usage
    is committed exactly as the backend reported it, and a commit that pushes
    totals beyond a cap reports a breach the caller must turn into run failure.
    Fake backends bill nothing on failure, so failed dispatches release their
    reservation at zero; live backends (G1) will commit failures at the ceiling.
    """

    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._lock = threading.Lock()
        self._committed_calls = 0
        self._committed_input_tokens = 0
        self._committed_output_tokens = 0
        self._committed_cost = 0
        self._inflight_calls = 0
        self._inflight_tokens = 0
        self._inflight_cost = 0

    def reserve(self, *, tokens: int, cost_microusd: int) -> BudgetReservation:
        with self._lock:
            checks = (
                (DIMENSION_CALLS, 1, self._committed_calls + self._inflight_calls,
                 self._budget.max_calls),
                (DIMENSION_TOKENS, tokens,
                 self._committed_input_tokens + self._committed_output_tokens
                 + self._inflight_tokens,
                 self._budget.max_total_tokens),
                (DIMENSION_COST, cost_microusd, self._committed_cost + self._inflight_cost,
                 self._budget.max_cost_microusd),
            )
            for dimension, requested, used, cap in checks:
                if used + requested > cap:
                    raise BudgetRejectedError(
                        dimension=dimension, requested=requested, used=used, cap=cap
                    )
            self._inflight_calls += 1
            self._inflight_tokens += tokens
            self._inflight_cost += cost_microusd
            return BudgetReservation(calls=1, tokens=tokens, cost_microusd=cost_microusd)

    def _drop_inflight(self, reservation: BudgetReservation) -> None:
        self._inflight_calls -= reservation.calls
        self._inflight_tokens -= reservation.tokens
        self._inflight_cost -= reservation.cost_microusd

    def commit(
        self, reservation: BudgetReservation, usage: Usage, cost_microusd: int
    ) -> BudgetBreach | None:
        """Record actual usage honestly; report a breach if a hard cap is now exceeded.

        Actuals are always recorded — accounting never lies about what a backend
        reported — but the caller must fail the run on a reported breach: a run
        must never complete successfully above its configured caps.
        """
        with self._lock:
            self._drop_inflight(reservation)
            self._committed_calls += reservation.calls
            self._committed_input_tokens += usage.input_tokens
            self._committed_output_tokens += usage.output_tokens
            self._committed_cost += cost_microusd
            checks = (
                (DIMENSION_CALLS, self._committed_calls, self._budget.max_calls),
                (DIMENSION_TOKENS,
                 self._committed_input_tokens + self._committed_output_tokens,
                 self._budget.max_total_tokens),
                (DIMENSION_COST, self._committed_cost, self._budget.max_cost_microusd),
            )
            for dimension, committed, cap in checks:
                if committed > cap:
                    return BudgetBreach(dimension=dimension, committed=committed, cap=cap)
            return None

    def release(self, reservation: BudgetReservation) -> None:
        with self._lock:
            self._drop_inflight(reservation)

    def totals(self) -> BudgetTotals:
        with self._lock:
            return BudgetTotals(
                calls=self._committed_calls,
                input_tokens=self._committed_input_tokens,
                output_tokens=self._committed_output_tokens,
                cost_microusd=self._committed_cost,
            )


# ---------------------------------------------------------------------------
# One-shot model worker
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """A manifest entry resolved to renderable content. Carries no producer identity."""

    kind: str
    label: str
    content: str


def render_task_block(task: TaskSpec) -> str:
    lines = [f"Title: {task.title}", "", task.statement]
    if task.acceptance_criteria:
        lines.append("")
        lines.append("Acceptance criteria:")
        lines.extend(f"- {criterion}" for criterion in task.acceptance_criteria)
    return "\n".join(lines)


def render_user_text(entries: tuple[ResolvedInput, ...]) -> str:
    return "\n\n".join(f"## {entry.label} ({entry.kind})\n{entry.content}" for entry in entries)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    text: str
    output_ref: ArtifactRef
    usage: Usage
    cost_microusd: int


def route_fingerprint(route: ModelRoute) -> str:
    """Content hash of one route's canonical document (including its id)."""
    doc = build_routes_doc({route.route_id: route})
    return sha256_hex(canonical_json_bytes(doc))


def validate_backend_coverage(
    routes: Mapping[str, ModelRoute], backends: Mapping[str, ChatBackend]
) -> None:
    """Every route must resolve to a registered backend connection — before the run."""
    missing = sorted({route.backend for route in routes.values()} - set(backends.keys()))
    if missing:
        raise PlanValidationError(
            f"routes reference unknown backend connections {missing!r}; "
            f"registered: {sorted(backends.keys())!r}"
        )


class OneShotModelWorker:
    """Executes one node attempt: render → snapshot → reserve → dispatch → persist.

    The prompt is assembled exclusively from resolved manifest entries (kind,
    label, content) — producer identities are structurally unavailable here.
    """

    def __init__(
        self,
        *,
        backends: Mapping[str, ChatBackend],
        ledger: BudgetLedger,
        artifacts: ArtifactStore,
        ids: IdSource,
        emit: Callable[..., None],
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._backends = backends
        self._ledger = ledger
        self._artifacts = artifacts
        self._ids = ids
        self._emit = emit
        self._scrubber = scrubber if scrubber is not None else process_scrubber()

    def _emit_receipt(
        self,
        *,
        node: PlanNode,
        attempt: Attempt,
        route: ModelRoute,
        receipt: InvocationReceipt,
        request_sha256: str,
        response_sha256: str | None,
        usage: Usage | PartialUsage | None,
        cost_microusd: int | None,
        error_kind: str | None,
        usage_source: str | None,
    ) -> None:
        """Persist per-dispatch provenance (the G1 ContextSnapshot seam, ADR-0001)."""
        doc: dict[str, object] = {
            "receipt_version": 1,
            "backend_kind": receipt.backend_kind,
            "connection_id": receipt.connection_id,
            "provider_request_id": receipt.provider_request_id,
            "latency_ms": receipt.latency_ms,
            "model": route.model,
            "route_id": route.route_id,
            "route_fingerprint": route_fingerprint(route),
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "cost_microusd": cost_microusd,
            "error_kind": error_kind,
            "usage_source": usage_source,
        }
        ref = self._artifacts.put(
            canonical_json_bytes(doc), media_type="application/json", role="receipt"
        )
        self._emit(
            ArtifactWritten(
                sha256=ref.sha256, size=ref.size, media_type=ref.media_type, role=ref.role
            ),
            node_id=node.node_id,
            attempt=attempt.index,
        )

    async def execute(
        self,
        *,
        node: PlanNode,
        route: ModelRoute,
        attempt: Attempt,
        entries: tuple[ResolvedInput, ...],
    ) -> WorkerResult:
        system = SYSTEM_PROMPTS[node.role]
        user_text = render_user_text(entries)
        request = PreparedRequest(
            model=route.model,
            max_output_tokens=route.max_output_tokens,
            system=system,
            user_text=user_text,
        )
        request_bytes = canonical_json_bytes(
            {
                "model": request.model,
                "max_output_tokens": request.max_output_tokens,
                "system": request.system,
                "user_text": request.user_text,
            }
        )
        request_ref = self._artifacts.put(
            request_bytes, media_type="application/json", role="request"
        )
        self._emit(
            ArtifactWritten(
                sha256=request_ref.sha256,
                size=request_ref.size,
                media_type=request_ref.media_type,
                role=request_ref.role,
            ),
            node_id=node.node_id,
            attempt=attempt.index,
        )

        estimated_input = estimate_tokens(system + user_text)
        reserve_tokens = estimated_input + route.max_output_tokens
        reserve_cost = dispatch_cost_microusd(
            estimated_input, route.max_output_tokens, route.pricing
        )
        try:
            reservation = self._ledger.reserve(
                tokens=reserve_tokens, cost_microusd=reserve_cost
            )
        except BudgetRejectedError as exc:
            self._emit(
                BudgetRejected(
                    route_id=route.route_id,
                    dimension=exc.dimension,
                    requested=exc.requested,
                    used=exc.used,
                    cap=exc.cap,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
            )
            raise

        # Every step between a successful reservation and the backend await is
        # inside this cleanup boundary: an id-allocation, serialization, frame
        # size, or journal failure must not leak an in-flight reservation.
        try:
            operation_id = self._ids.new_id("op")
            call_id = self._ids.new_id("call")
            self._emit(
                ModelDispatched(
                    route_id=route.route_id,
                    model=route.model,
                    request_sha256=request_ref.sha256,
                    reserved_calls=reservation.calls,
                    reserved_tokens=reservation.tokens,
                    reserved_cost_microusd=reservation.cost_microusd,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
                operation_id=operation_id,
                call_id=call_id,
            )
            backend = self._backends[route.backend]
        except BaseException:
            self._ledger.release(reservation)
            raise
        try:
            result = await backend.complete(request)
        except asyncio.CancelledError:
            # The dispatch entered an ambiguous potentially-sent state: the
            # provider may have received (and may bill) the request. Close the
            # dispatch durably at the reservation ceiling before propagating —
            # cancellation must never silently release a possibly billed call.
            billed_usage = Usage(
                input_tokens=estimated_input, output_tokens=route.max_output_tokens
            )
            self._ledger.commit(reservation, billed_usage, reserve_cost)
            self._emit(
                ModelFailed(
                    reason=DISPATCH_KIND_CANCELLED,
                    message=(
                        "dispatch cancelled while the request may have reached "
                        "the provider; committed at the reservation ceiling"
                    ),
                    billed_input_tokens=billed_usage.input_tokens,
                    billed_output_tokens=billed_usage.output_tokens,
                    billed_cost_microusd=reserve_cost,
                    usage_source=USAGE_SOURCE_CEILING,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
                operation_id=operation_id,
                call_id=call_id,
            )
            raise
        except BackendDispatchError as exc:
            # A normalized live-dispatch failure. If the provider may have
            # billed the call — or *reported usage* for it, which is the
            # provider stating what it processed — commit it honestly
            # (reported actuals when available, otherwise the reservation
            # ceiling) and close the dispatch with a durable model.failed.
            # Only a failure that is both non-billed by classification and
            # carries no reported usage releases its reservation.
            if not exc.billed and exc.usage is None:
                self._ledger.release(reservation)
                if exc.receipt is not None:
                    self._emit_receipt(
                        node=node, attempt=attempt, route=route, receipt=exc.receipt,
                        request_sha256=request_ref.sha256, response_sha256=None,
                        usage=None, cost_microusd=None, error_kind=exc.kind,
                        usage_source=None,
                    )
                raise
            # Per-component accounting: every provider-known component is
            # committed exactly as reported (never clamped, never replaced by
            # a smaller local estimate); unknown components are committed at
            # their reservation-ceiling component.
            known_input = exc.usage.input_tokens if exc.usage is not None else None
            known_output = exc.usage.output_tokens if exc.usage is not None else None
            if known_input is not None and known_output is not None:
                billed_usage = Usage(input_tokens=known_input, output_tokens=known_output)
                usage_source = USAGE_SOURCE_PROVIDER
                billed_cost = dispatch_cost_microusd(
                    billed_usage.input_tokens, billed_usage.output_tokens, route.pricing
                )
            elif known_input is None and known_output is None:
                billed_usage = Usage(
                    input_tokens=estimated_input, output_tokens=route.max_output_tokens
                )
                usage_source = USAGE_SOURCE_CEILING
                billed_cost = reserve_cost
            else:
                billed_usage = Usage(
                    input_tokens=known_input if known_input is not None else estimated_input,
                    output_tokens=(
                        known_output if known_output is not None else route.max_output_tokens
                    ),
                )
                usage_source = USAGE_SOURCE_MIXED
                billed_cost = dispatch_cost_microusd(
                    billed_usage.input_tokens, billed_usage.output_tokens, route.pricing
                )
            # Honest accounting first; a breach here is not raised separately —
            # the node already fails as backend_error and the run cannot
            # complete successfully with a failed node.
            self._ledger.commit(reservation, billed_usage, billed_cost)
            if exc.receipt is not None:
                self._emit_receipt(
                    node=node, attempt=attempt, route=route, receipt=exc.receipt,
                    request_sha256=request_ref.sha256, response_sha256=None,
                    usage=exc.usage, cost_microusd=billed_cost, error_kind=exc.kind,
                    usage_source=usage_source,
                )
            self._emit(
                ModelFailed(
                    reason=exc.kind,
                    message=_bound_event_message(self._scrubber.scrub(str(exc))),
                    billed_input_tokens=billed_usage.input_tokens,
                    billed_output_tokens=billed_usage.output_tokens,
                    billed_cost_microusd=billed_cost,
                    usage_source=usage_source,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
                operation_id=operation_id,
                call_id=call_id,
            )
            raise
        except BaseException:
            # Fake backends bill nothing on failure (see BudgetLedger docstring):
            # release the reservation on any failure so admission stays exact.
            self._ledger.release(reservation)
            raise

        if result.usage.output_tokens > route.max_output_tokens:
            # A backend reporting more output than the request permitted violates
            # the backend contract; normalize it instead of trusting the claim.
            message = (
                f"backend reported {result.usage.output_tokens} output tokens, "
                f"above the route limit {route.max_output_tokens}"
            )
            if result.receipt is None:
                # Fake/test backends bill nothing (the G0 fake-billing rule).
                self._ledger.release(reservation)
                raise BackendError(message)
            # A live provider processed — and billed — this call: commit the
            # reported actuals honestly, persist the evidence and the receipt,
            # and close the dispatch with a durable model.failed.
            over_cost = dispatch_cost_microusd(
                result.usage.input_tokens, result.usage.output_tokens, route.pricing
            )
            self._ledger.commit(reservation, result.usage, over_cost)
            over_output_ref = self._artifacts.put(
                result.text.encode("utf-8"), media_type="text/plain", role=node.role
            )
            self._emit(
                ArtifactWritten(
                    sha256=over_output_ref.sha256,
                    size=over_output_ref.size,
                    media_type=over_output_ref.media_type,
                    role=over_output_ref.role,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
            )
            self._emit_receipt(
                node=node, attempt=attempt, route=route, receipt=result.receipt,
                request_sha256=request_ref.sha256,
                response_sha256=over_output_ref.sha256,
                usage=result.usage, cost_microusd=over_cost,
                error_kind=DISPATCH_KIND_PROTOCOL, usage_source=USAGE_SOURCE_PROVIDER,
            )
            self._emit(
                ModelFailed(
                    reason=DISPATCH_KIND_PROTOCOL,
                    message=message,
                    billed_input_tokens=result.usage.input_tokens,
                    billed_output_tokens=result.usage.output_tokens,
                    billed_cost_microusd=over_cost,
                    usage_source=USAGE_SOURCE_PROVIDER,
                ),
                node_id=node.node_id,
                attempt=attempt.index,
                operation_id=operation_id,
                call_id=call_id,
            )
            raise BackendError(message)

        cost = dispatch_cost_microusd(
            result.usage.input_tokens, result.usage.output_tokens, route.pricing
        )
        # Honest accounting first: the breach (if any) is raised only after the
        # actual usage has been durably journaled below.
        breach = self._ledger.commit(reservation, result.usage, cost)
        output_ref = self._artifacts.put(
            result.text.encode("utf-8"), media_type="text/plain", role=node.role
        )
        self._emit(
            ArtifactWritten(
                sha256=output_ref.sha256,
                size=output_ref.size,
                media_type=output_ref.media_type,
                role=output_ref.role,
            ),
            node_id=node.node_id,
            attempt=attempt.index,
        )
        self._emit(
            ModelCompleted(
                model=result.model,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cost_microusd=cost,
                output_sha256=output_ref.sha256,
            ),
            node_id=node.node_id,
            attempt=attempt.index,
            operation_id=operation_id,
            call_id=call_id,
        )
        if result.receipt is not None:
            self._emit_receipt(
                node=node, attempt=attempt, route=route, receipt=result.receipt,
                request_sha256=request_ref.sha256, response_sha256=output_ref.sha256,
                usage=result.usage, cost_microusd=cost, error_kind=None,
                usage_source=USAGE_SOURCE_PROVIDER,
            )
        if breach is not None:
            raise BudgetExceededError(
                dimension=breach.dimension, committed=breach.committed, cap=breach.cap
            )
        return WorkerResult(
            text=result.text, output_ref=output_ref, usage=result.usage, cost_microusd=cost
        )


# ---------------------------------------------------------------------------
# Run executor
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _NodeOutcome:
    status: NodeStatus
    text: str | None = None
    reason: str | None = None


class RunExecutor:
    """Barrier-scheduled DAG execution over an asyncio TaskGroup."""

    def __init__(
        self,
        *,
        run: Run,
        store: RunStore,
        backends: Mapping[str, ChatBackend],
        clock: Clock,
        ids: IdSource,
        run_created: RunCreated,
        sink: FrameSink | None,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._run = run
        self._store = store
        self._backends = backends
        self._clock = clock
        self._ids = ids
        self._run_created = run_created
        self._sink = sink
        self._scrubber = scrubber if scrubber is not None else process_scrubber()
        self._ledger = BudgetLedger(run.budget)
        self._envelopes: list[EventEnvelope] = []
        self._events: dict[str, asyncio.Event] = {}
        self._outcomes: dict[str, _NodeOutcome] = {}
        self._failures: list[tuple[str, str]] = []
        self._decision: Decision | None = None
        self._status = RunStatus.CREATED
        self._sink_error: str | None = None
        # Dagvane-owned abort signal: set only when the engine itself is
        # tearing the run down (durable storage failed). A CancelledError seen
        # by a node without this flag is *not* an engine cancellation and must
        # close the node durably instead of propagating.
        self._engine_abort = False

    @property
    def sink_error(self) -> str | None:
        """Set when the output sink failed mid-run and streaming was disabled."""
        return self._sink_error

    def _emit(
        self,
        payload: EventPayload,
        *,
        node_id: str | None = None,
        attempt: int | None = None,
        operation_id: str | None = None,
        call_id: str | None = None,
    ) -> None:
        envelope = EventEnvelope(
            v=ENVELOPE_VERSION,
            event_id=self._ids.new_id("event"),
            run_id=self._run.run_id,
            seq=self._journal.next_seq,
            ts=self._clock.now_iso(),
            node_id=node_id,
            attempt=attempt,
            operation_id=operation_id,
            call_id=call_id,
            type=payload.TYPE,
            data=payload_to_data(payload),
        )
        line = self._journal.append(envelope)
        self._envelopes.append(envelope)
        if self._sink is not None:
            try:
                self._sink(line, envelope)
            except Exception as exc:
                # The journal is authoritative; the sink is an observer. A broken
                # sink degrades streaming only — it must not fail a healthy run.
                self._sink = None
                self._sink_error = f"{type(exc).__name__}: {exc}"

    async def execute(self) -> tuple[RunStatus, dict[str, object]]:
        self._journal = self._store.open_journal(self._run.run_id)
        try:
            artifacts = self._store.artifact_store(self._run.run_id)
            worker = OneShotModelWorker(
                backends=self._backends,
                ledger=self._ledger,
                artifacts=artifacts,
                ids=self._ids,
                emit=self._emit,
                scrubber=self._scrubber,
            )
            for node in self._run.plan.nodes:
                self._events[node.node_id] = asyncio.Event()

            self._emit(self._run_created)
            self._status = advance_run_status(self._status, RunStatus.RUNNING)

            try:
                async with asyncio.TaskGroup() as group:
                    for node in self._run.plan.nodes:
                        group.create_task(self._run_node(node, worker))
            except BaseExceptionGroup as group_exc:
                # Only durability failures (StorageError) escape the per-node
                # taxonomy. Unwrap the first leaf so callers see the normalized
                # engine error, and claim no terminal state for this run.
                cause: BaseException = group_exc
                while isinstance(cause, BaseExceptionGroup):
                    cause = cause.exceptions[0]
                raise cause from group_exc

            status = RunStatus.COMPLETED if not self._failures else RunStatus.FAILED
            reason = None
            if self._failures:
                failed_node, failed_reason = self._failures[0]
                reason = f"{failed_node}: {failed_reason}"
            totals = self._ledger.totals()
            self._emit(
                RunFinished(
                    status=status.value,
                    reason=reason,
                    calls=totals.calls,
                    input_tokens=totals.input_tokens,
                    output_tokens=totals.output_tokens,
                    cost_microusd=totals.cost_microusd,
                )
            )
            self._status = advance_run_status(self._status, status)
        finally:
            self._journal.close()

        view = fold_envelopes(self._envelopes)
        report_doc = rebuild_report(view)
        self._store.write_report(self._run.run_id, report_doc)
        if self._decision is not None:
            self._store.write_decision(self._run.run_id, build_decision_doc(self._decision))
        return self._status, report_doc

    def _fail_node(self, node: PlanNode, attempt: Attempt, reason: str, message: str) -> None:
        self._emit(
            NodeFailed(
                reason=reason,
                message=_bound_event_message(self._scrubber.scrub(message)),
            ),
            node_id=node.node_id,
            attempt=attempt.index,
        )
        self._outcomes[node.node_id] = _NodeOutcome(status=NodeStatus.FAILED, reason=reason)
        self._failures.append((node.node_id, reason))

    async def _run_node(self, node: PlanNode, worker: OneShotModelWorker) -> None:
        attempt = Attempt(node_id=node.node_id, index=1)
        try:
            try:
                for dep in node.depends_on:
                    await self._events[dep].wait()
                failed_dep = next(
                    (
                        dep
                        for dep in node.depends_on
                        if self._outcomes[dep].status is not NodeStatus.COMPLETED
                    ),
                    None,
                )
                if failed_dep is not None:
                    self._fail_node(
                        node,
                        attempt,
                        REASON_DEPENDENCY_FAILED,
                        f"dependency {failed_dep} did not complete",
                    )
                    return

                try:
                    await self._execute_node(node, worker, attempt)
                except BudgetRejectedError as exc:
                    self._fail_node(node, attempt, REASON_BUDGET_REJECTED, str(exc))
                except BudgetExceededError as exc:
                    self._fail_node(node, attempt, REASON_BUDGET_EXCEEDED, str(exc))
                except BackendError as exc:
                    self._fail_node(node, attempt, REASON_BACKEND_ERROR, str(exc))
                except StorageError:
                    # Durable storage failed: the run must not fabricate
                    # terminal state on a store that just failed a write. Mark
                    # the engine-owned abort so sibling cancellations are
                    # distinguishable from backend-leaked ones, then abort.
                    self._engine_abort = True
                    raise
                except Exception as exc:
                    # Unexpected worker/runtime failure: durable failed-node
                    # semantics instead of an abandoned, non-terminal run.
                    self._fail_node(
                        node,
                        attempt,
                        REASON_UNEXPECTED_ERROR,
                        f"{type(exc).__name__}: {exc}",
                    )
            except asyncio.CancelledError:
                # This guard covers the *whole* node lifetime — dependency
                # waits included, not just the dispatch stage.
                if self._engine_abort:
                    # Dagvane itself is tearing the run down (storage failure):
                    # propagate without touching the failed store.
                    raise
                # Either an external (owner/loop) cancellation or a backend/SDK
                # cancelling its own worker task. `Task.cancelling()` cannot
                # distinguish the two, so both close the node durably — no
                # terminal journal may contain a non-terminal cancelled node,
                # and any open dispatch was already committed and closed by
                # the worker. External teardown still aborts the run through
                # the parent task's own pending cancellation.
                self._fail_node(
                    node,
                    attempt,
                    REASON_CANCELLED,
                    "cancellation delivered during node execution; any open "
                    "dispatch was committed at the reservation ceiling",
                )
                current = asyncio.current_task()
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
        finally:
            if node.node_id not in self._outcomes:
                self._outcomes[node.node_id] = _NodeOutcome(
                    status=NodeStatus.FAILED, reason="internal_error"
                )
            self._events[node.node_id].set()

    async def _execute_node(
        self, node: PlanNode, worker: OneShotModelWorker, attempt: Attempt
    ) -> None:
        self._emit(
            NodeStarted(role=node.role, route_id=node.route_id),
            node_id=node.node_id,
            attempt=attempt.index,
        )
        entries = self._resolve_manifest(node)
        route = self._run.routes[node.route_id]
        result = await worker.execute(
            node=node, route=route, attempt=attempt, entries=entries
        )

        if node.role == ROLE_JUDGE:
            # Allowed winners come from the proposals the judge actually saw,
            # not from the anonymization mapping keys.
            allowed_winners = frozenset(
                entry.label
                for entry in node.input_manifest.entries
                if entry.kind == ENTRY_KIND_PROPOSAL
            )
            try:
                decision = parse_decision(result.text, allowed_winners)
            except SpecError as exc:
                self._fail_node(node, attempt, REASON_INVALID_DECISION, str(exc))
                return
            self._emit(
                NodeCompleted(output_sha256=result.output_ref.sha256),
                node_id=node.node_id,
                attempt=attempt.index,
            )
            self._decision = decision
            self._emit(
                DecisionRecorded(
                    decision_version=decision.decision_version,
                    winner=decision.winner,
                    rationale=decision.rationale,
                    source_sha256=result.output_ref.sha256,
                )
            )
        else:
            self._emit(
                NodeCompleted(output_sha256=result.output_ref.sha256),
                node_id=node.node_id,
                attempt=attempt.index,
            )
        self._outcomes[node.node_id] = _NodeOutcome(
            status=NodeStatus.COMPLETED, text=result.text
        )

    def _resolve_manifest(self, node: PlanNode) -> tuple[ResolvedInput, ...]:
        resolved: list[ResolvedInput] = []
        for entry in node.input_manifest.entries:
            if entry.kind == ENTRY_KIND_TASK:
                content = render_task_block(self._run.task)
            else:
                assert entry.producer is not None  # validated at plan time
                outcome = self._outcomes[entry.producer]
                assert outcome.text is not None  # barrier: producer completed
                content = outcome.text
            resolved.append(ResolvedInput(kind=entry.kind, label=entry.label, content=content))
        return tuple(resolved)


# ---------------------------------------------------------------------------
# Entry points (composition is the interface layer's job; these take ports)
# ---------------------------------------------------------------------------


def plan_council_doc(task: LoadedTask) -> dict[str, object]:
    """Dry-run document: validate the task, build and validate the plan, no I/O."""
    plan, routes, budget = CouncilTemplate.build(task.spec)
    PlanValidator().validate(plan, routes)
    plan_doc = build_plan_doc(plan)
    return {
        "dry_run": True,
        "task_sha256": task.sha256,
        "plan_sha256": sha256_hex(canonical_json_bytes(plan_doc)),
        "plan": plan_doc,
        "routes": build_routes_doc(routes),
        "budget_caps": build_budget_doc(budget),
    }


@dataclass(frozen=True, slots=True)
class CouncilRunResult:
    run_id: str
    status: RunStatus
    report_doc: dict[str, object]
    sink_error: str | None = None


async def _aclose_backend(backend: ChatBackend) -> None:
    """Duck-typed close: live adapters expose ``aclose``; fakes need not."""
    aclose = getattr(backend, "aclose", None)
    if not callable(aclose):
        return
    result = aclose()
    if result is not None and hasattr(result, "__await__"):
        await result


def _execute_council(
    *,
    task: LoadedTask,
    plan: Plan,
    routes: dict[str, ModelRoute],
    budget: Budget,
    backends: Mapping[str, ChatBackend],
    clock: Clock,
    ids: IdSource,
    run_id: str,
    config_sha256: str,
    determinism_doc: dict[str, object],
    store: RunStore,
    sink: FrameSink | None,
    scrubber: SecretScrubber | None = None,
) -> CouncilRunResult:
    """Shared execution path for fixture and live councils."""
    run = Run(
        run_id=run_id,
        task=task.spec,
        plan=plan,
        routes=routes,
        budget=budget,
        created_ts=clock.now_iso(),
    )
    plan_doc = build_plan_doc(plan)
    plan_sha256 = sha256_hex(canonical_json_bytes(plan_doc))

    store.create_run(run_id)
    store.write_manifest(
        run_id,
        build_manifest_doc(
            run_id=run_id,
            created_ts=run.created_ts,
            engine_version=__version__,
            task_doc=task.doc,
            task_sha256=task.sha256,
            plan_doc=plan_doc,
            plan_sha256=plan_sha256,
            routes_doc=build_routes_doc(routes),
            budget_doc=build_budget_doc(budget),
            fixture_sha256=config_sha256,
            determinism_doc=determinism_doc,
        ),
    )
    executor = RunExecutor(
        run=run,
        store=store,
        backends=backends,
        clock=clock,
        ids=ids,
        run_created=RunCreated(
            engine_version=__version__,
            task_sha256=task.sha256,
            plan_sha256=plan_sha256,
            fixture_sha256=config_sha256,
            node_count=len(plan.nodes),
            max_calls=budget.max_calls,
            max_total_tokens=budget.max_total_tokens,
            max_cost_microusd=budget.max_cost_microusd,
        ),
        sink=sink,
        scrubber=scrubber,
    )

    async def _run_and_close() -> tuple[RunStatus, dict[str, object]]:
        # Adapter transport resources are owned by this run: they are closed
        # inside the same event loop that used them, before asyncio.run tears
        # the loop down — never left to interpreter finalization.
        try:
            return await executor.execute()
        finally:
            for connection_id in sorted(backends):
                try:
                    await _aclose_backend(backends[connection_id])
                except Exception:  # noqa: BLE001 — a close failure must not
                    pass  # clobber the run's real outcome

    status, report_doc = asyncio.run(_run_and_close())
    return CouncilRunResult(
        run_id=run_id, status=status, report_doc=report_doc, sink_error=executor.sink_error
    )


def run_council(
    *,
    task: LoadedTask,
    fixture: FixtureSpec,
    store: RunStore,
    backend: ChatBackend,
    sink: FrameSink | None = None,
) -> CouncilRunResult:
    """Execute one deterministic fixture council run (the G0 path, unchanged)."""
    plan, routes, budget = CouncilTemplate.build(task.spec)
    PlanValidator().validate(plan, routes)
    backends: Mapping[str, ChatBackend] = {"fake": backend}
    validate_backend_coverage(routes, backends)

    clock: Clock
    if fixture.clock_start is not None and fixture.clock_step_ms is not None:
        clock = FixedClock(fixture.clock_start, fixture.clock_step_ms)
    else:
        clock = SystemClock()
    ids: IdSource
    ids = SequentialIds(fixture.ids_seed) if fixture.ids_seed is not None else SystemIds()
    run_id = fixture.run_id if fixture.run_id is not None else ids.new_id("run")

    return _execute_council(
        task=task,
        plan=plan,
        routes=routes,
        budget=budget,
        backends=backends,
        clock=clock,
        ids=ids,
        run_id=run_id,
        config_sha256=fixture.sha256,
        determinism_doc=fixture.determinism_doc(),
        store=store,
        sink=sink,
    )


def run_council_live(
    *,
    task: LoadedTask,
    profile: ProfileSpec,
    backends: Mapping[str, ChatBackend],
    store: RunStore,
    sink: FrameSink | None = None,
    clock: Clock | None = None,
    ids: IdSource | None = None,
    scrubber: SecretScrubber | None = None,
) -> CouncilRunResult:
    """Execute one live council run against profile-configured backends.

    ``backends`` maps connection ids to constructed adapters (the composition
    root builds them; credential values never travel through this layer —
    ``scrubber`` is the shared registry those adapters already use, reused
    here for the durable-event path). Adapters are closed by this call before
    its event loop is torn down. The manifest/``run.created`` config hash is
    the profile file's sha256.
    """
    plan, routes, budget = CouncilTemplate.build(task.spec, profile.role_routes())
    PlanValidator().validate(plan, routes)
    validate_backend_coverage(routes, backends)

    resolved_clock: Clock = clock if clock is not None else SystemClock()
    resolved_ids: IdSource = ids if ids is not None else SystemIds()
    run_id = resolved_ids.new_id("run")

    return _execute_council(
        task=task,
        plan=plan,
        routes=routes,
        budget=budget,
        backends=backends,
        clock=resolved_clock,
        ids=resolved_ids,
        run_id=run_id,
        config_sha256=profile.sha256,
        determinism_doc={"run_id_pinned": False, "clock": None, "ids_seed": None},
        store=store,
        sink=sink,
        scrubber=scrubber,
    )
