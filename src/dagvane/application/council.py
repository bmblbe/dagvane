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
    ENTRY_KIND_PROPOSAL,
    ENTRY_KIND_REVIEW,
    ENTRY_KIND_TASK,
    ENTRY_KINDS,
    ENVELOPE_VERSION,
    REASON_BACKEND_ERROR,
    REASON_BUDGET_REJECTED,
    REASON_DEPENDENCY_FAILED,
    REASON_INVALID_DECISION,
    ROLE_JUDGE,
    ROLE_PROPOSER,
    ROLE_REVIEWER,
    ArtifactRef,
    ArtifactWritten,
    Attempt,
    BackendError,
    Budget,
    BudgetRejected,
    BudgetRejectedError,
    Decision,
    DecisionRecorded,
    EventEnvelope,
    EventPayload,
    InputManifest,
    ManifestEntry,
    ModelCompleted,
    ModelDispatched,
    ModelRoute,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    NodeStatus,
    Plan,
    PlanNode,
    PlanValidationError,
    Pricing,
    Run,
    RunCreated,
    RunFinished,
    RunStatus,
    SpecError,
    TaskSpec,
    Usage,
    advance_run_status,
    estimate_tokens,
    payload_to_data,
)
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

FrameSink = Callable[[bytes, EventEnvelope], None]

COUNCIL_PLAN_VERSION = 1

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
    def build(task: TaskSpec) -> tuple[Plan, dict[str, ModelRoute], Budget]:
        def route(name: str) -> ModelRoute:
            return ModelRoute(
                route_id=f"fake/{name}",
                model=f"fake-{name}",
                backend="fake",
                pricing=_FAKE_PRICING,
                max_output_tokens=_FAKE_MAX_OUTPUT_TOKENS,
            )

        routes = {
            r.route_id: r
            for r in (
                route("proposer-a"),
                route("proposer-b"),
                route("reviewer-a"),
                route("reviewer-b"),
                route("judge"),
            )
        }
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
                route_id="fake/proposer-a",
                depends_on=(),
                input_manifest=InputManifest(entries=(task_entry,)),
            ),
            PlanNode(
                node_id="proposer-b",
                role=ROLE_PROPOSER,
                identity="B",
                route_id="fake/proposer-b",
                depends_on=(),
                input_manifest=InputManifest(entries=(task_entry,)),
            ),
            # The hard barrier: every review depends on *all* proposers, while its
            # manifest exposes only the sibling identity's proposal (blind, no self).
            PlanNode(
                node_id="review-by-a",
                role=ROLE_REVIEWER,
                identity="A",
                route_id="fake/reviewer-a",
                depends_on=("proposer-a", "proposer-b"),
                input_manifest=InputManifest(entries=(task_entry, candidate_2)),
            ),
            PlanNode(
                node_id="review-by-b",
                role=ROLE_REVIEWER,
                identity="B",
                route_id="fake/reviewer-b",
                depends_on=("proposer-a", "proposer-b"),
                input_manifest=InputManifest(entries=(task_entry, candidate_1)),
            ),
            PlanNode(
                node_id="judge",
                role=ROLE_JUDGE,
                identity="J",
                route_id="fake/judge",
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
        proposers = [n.node_id for n in plan.nodes if n.role == ROLE_PROPOSER]
        reviewers = [n.node_id for n in plan.nodes if n.role == ROLE_REVIEWER]
        judges = [n.node_id for n in plan.nodes if n.role == ROLE_JUDGE]
        if len(proposers) < 2:
            raise PlanValidationError("a council needs at least two proposers")
        if len(judges) != 1:
            raise PlanValidationError("a council needs exactly one judge")

        for node in plan.nodes:
            self._validate_manifest(node, nodes, ancestors[node.node_id])
            if node.role == ROLE_PROPOSER:
                # Structural anti-anchoring: proposer context is the task alone.
                for entry in node.input_manifest.entries:
                    if entry.kind != ENTRY_KIND_TASK:
                        raise PlanValidationError(
                            f"proposer {node.node_id!r} manifest may only contain the task, "
                            f"found {entry.kind!r} entry {entry.label!r}"
                        )
            elif node.role == ROLE_REVIEWER:
                missing = [p for p in proposers if p not in node.depends_on]
                if missing:
                    raise PlanValidationError(
                        f"review node {node.node_id!r} must depend on all proposers "
                        f"(hard barrier); missing {missing!r}"
                    )
                reviewed = [
                    e for e in node.input_manifest.entries if e.kind == ENTRY_KIND_PROPOSAL
                ]
                if not reviewed:
                    raise PlanValidationError(
                        f"review node {node.node_id!r} reviews no proposal"
                    )
                for entry in reviewed:
                    assert entry.producer is not None  # _validate_manifest guarantees this
                    if nodes[entry.producer].identity == node.identity:
                        raise PlanValidationError(
                            f"self-review: node {node.node_id!r} (identity "
                            f"{node.identity!r}) would review its own identity's "
                            f"proposal {entry.producer!r}"
                        )
            elif node.role == ROLE_JUDGE:
                missing = [r for r in reviewers if r not in node.depends_on]
                if missing:
                    raise PlanValidationError(
                        f"judge {node.node_id!r} must depend on all reviews; "
                        f"missing {missing!r}"
                    )
            else:
                raise PlanValidationError(
                    f"node {node.node_id!r} has unknown role {node.role!r}"
                )

        for label, producer in sorted(plan.anonymization.items()):
            if producer not in nodes or nodes[producer].role != ROLE_PROPOSER:
                raise PlanValidationError(
                    f"anonymization label {label!r} maps to non-proposer {producer!r}"
                )
        proposal_labels = {
            entry.label: entry.producer
            for node in plan.nodes
            for entry in node.input_manifest.entries
            if entry.kind == ENTRY_KIND_PROPOSAL
        }
        for proposal_label, proposal_producer in proposal_labels.items():
            if plan.anonymization.get(proposal_label) != proposal_producer:
                raise PlanValidationError(
                    f"proposal label {proposal_label!r} is not consistent with the sealed "
                    "anonymization mapping"
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


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def dispatch_cost_microusd(input_tokens: int, output_tokens: int, pricing: Pricing) -> int:
    return _ceil_div(input_tokens * pricing.input_microusd_per_mtok, 1_000_000) + _ceil_div(
        output_tokens * pricing.output_microusd_per_mtok, 1_000_000
    )


class BudgetLedger:
    """Hard admission: every dispatch reserves atomically before backend invocation.

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

    def commit(self, reservation: BudgetReservation, usage: Usage, cost_microusd: int) -> None:
        with self._lock:
            self._drop_inflight(reservation)
            self._committed_calls += reservation.calls
            self._committed_input_tokens += usage.input_tokens
            self._committed_output_tokens += usage.output_tokens
            self._committed_cost += cost_microusd

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


class OneShotModelWorker:
    """Executes one node attempt: render → snapshot → reserve → dispatch → persist.

    The prompt is assembled exclusively from resolved manifest entries (kind,
    label, content) — producer identities are structurally unavailable here.
    """

    def __init__(
        self,
        *,
        backend: ChatBackend,
        ledger: BudgetLedger,
        artifacts: ArtifactStore,
        ids: IdSource,
        emit: Callable[..., None],
    ) -> None:
        self._backend = backend
        self._ledger = ledger
        self._artifacts = artifacts
        self._ids = ids
        self._emit = emit

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
        try:
            result = await self._backend.complete(request)
        except BackendError:
            self._ledger.release(reservation)
            raise

        cost = dispatch_cost_microusd(
            result.usage.input_tokens, result.usage.output_tokens, route.pricing
        )
        self._ledger.commit(reservation, result.usage, cost)
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
        backend: ChatBackend,
        clock: Clock,
        ids: IdSource,
        run_created: RunCreated,
        sink: FrameSink | None,
    ) -> None:
        self._run = run
        self._store = store
        self._backend = backend
        self._clock = clock
        self._ids = ids
        self._run_created = run_created
        self._sink = sink
        self._ledger = BudgetLedger(run.budget)
        self._envelopes: list[EventEnvelope] = []
        self._events: dict[str, asyncio.Event] = {}
        self._outcomes: dict[str, _NodeOutcome] = {}
        self._failures: list[tuple[str, str]] = []
        self._decision: Decision | None = None
        self._status = RunStatus.CREATED

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
            self._sink(line, envelope)

    async def execute(self) -> tuple[RunStatus, dict[str, object]]:
        self._journal = self._store.open_journal(self._run.run_id)
        try:
            artifacts = self._store.artifact_store(self._run.run_id)
            worker = OneShotModelWorker(
                backend=self._backend,
                ledger=self._ledger,
                artifacts=artifacts,
                ids=self._ids,
                emit=self._emit,
            )
            for node in self._run.plan.nodes:
                self._events[node.node_id] = asyncio.Event()

            self._emit(self._run_created)
            self._status = advance_run_status(self._status, RunStatus.RUNNING)

            async with asyncio.TaskGroup() as group:
                for node in self._run.plan.nodes:
                    group.create_task(self._run_node(node, worker))

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
            NodeFailed(reason=reason, message=message),
            node_id=node.node_id,
            attempt=attempt.index,
        )
        self._outcomes[node.node_id] = _NodeOutcome(status=NodeStatus.FAILED, reason=reason)
        self._failures.append((node.node_id, reason))

    async def _run_node(self, node: PlanNode, worker: OneShotModelWorker) -> None:
        attempt = Attempt(node_id=node.node_id, index=1)
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

            self._emit(
                NodeStarted(role=node.role, route_id=node.route_id),
                node_id=node.node_id,
                attempt=attempt.index,
            )
            entries = self._resolve_manifest(node)
            route = self._run.routes[node.route_id]
            try:
                result = await worker.execute(
                    node=node, route=route, attempt=attempt, entries=entries
                )
            except BudgetRejectedError as exc:
                self._fail_node(node, attempt, REASON_BUDGET_REJECTED, str(exc))
                return
            except BackendError as exc:
                self._fail_node(node, attempt, REASON_BACKEND_ERROR, str(exc))
                return

            if node.role == ROLE_JUDGE:
                try:
                    decision = parse_decision(
                        result.text, frozenset(self._run.plan.anonymization.keys())
                    )
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
        finally:
            if node.node_id not in self._outcomes:
                self._outcomes[node.node_id] = _NodeOutcome(
                    status=NodeStatus.FAILED, reason="internal_error"
                )
            self._events[node.node_id].set()

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


def run_council(
    *,
    task: LoadedTask,
    fixture: FixtureSpec,
    store: RunStore,
    backend: ChatBackend,
    sink: FrameSink | None = None,
) -> CouncilRunResult:
    """Execute one council run to a terminal state and persist all derived views."""
    plan, routes, budget = CouncilTemplate.build(task.spec)
    PlanValidator().validate(plan, routes)

    clock: Clock
    if fixture.clock_start is not None and fixture.clock_step_ms is not None:
        clock = FixedClock(fixture.clock_start, fixture.clock_step_ms)
    else:
        clock = SystemClock()
    ids: IdSource
    ids = SequentialIds(fixture.ids_seed) if fixture.ids_seed is not None else SystemIds()
    run_id = fixture.run_id if fixture.run_id is not None else ids.new_id("run")

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
            fixture_sha256=fixture.sha256,
            determinism_doc=fixture.determinism_doc(),
        ),
    )
    executor = RunExecutor(
        run=run,
        store=store,
        backend=backend,
        clock=clock,
        ids=ids,
        run_created=RunCreated(
            engine_version=__version__,
            task_sha256=task.sha256,
            plan_sha256=plan_sha256,
            fixture_sha256=fixture.sha256,
            node_count=len(plan.nodes),
            max_calls=budget.max_calls,
            max_total_tokens=budget.max_total_tokens,
            max_cost_microusd=budget.max_cost_microusd,
        ),
        sink=sink,
    )
    status, report_doc = asyncio.run(executor.execute())
    return CouncilRunResult(run_id=run_id, status=status, report_doc=report_doc)
