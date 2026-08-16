"""Cancellation Port V1: interface-first contract for monotonic run cancellation.

This is the R1-D0 checkpoint for `RUN-003`: the versioned value types and the
`CancellationPortV1` Protocol a future durable adapter must satisfy. It
defines no implementation, no persistence/wire schema, and no process
termination — those stay adapter- and application-layer concerns. Every
mutating Protocol method is CAS-fenced: callers pass the exact prior state
(or version) they expect to still hold, and an implementation must raise
rather than silently overwrite a concurrent writer.

Legal phase progression is monotonic and forward-only:

    NOT_REQUESTED -> REQUESTED -> STOPPING -> {QUIESCENT, CLEANUP_INCOMPLETE}
    CLEANUP_INCOMPLETE -> QUIESCENT

A retry of the identical (phase, observed_request_version) pair is
idempotent. No transition may move backwards, skip a generation/version
check, or claim the `CANCELLED` terminal outcome before quiescence is proven.

Every comparison below -- ``advance``'s CAS, ``finalize``'s CAS and binding,
and ``request_cancellation``'s CAS and binding -- matches the *entire*
immutable `CancellationScopeV1` by value, including the opaque
``owner_key``: a substituted authority handle that keeps the same
``scope_id``/``run_id``/``generation`` fails closed with a constant
`SpecError`, never a silent match on the identifiers alone.

``advance`` is fenced by the caller's exact expected *current observation*
(the full scope, ``observed_request_version`` and ``phase`` together, via
dataclass equality), not by generation alone: two writers that share a
generation and only differ by which phase they believe is current must not
both succeed. ``REQUESTED`` is never a legal ``advance`` target: entering it
is only possible through ``request_cancellation``, which durably records the
request and the resulting ``REQUESTED`` observation in one atomic write, so
no conforming implementation can ever expose a state where a request is
durably recorded but ``observe`` still reports ``NOT_REQUESTED``.
``request_cancellation`` also linearizes every later request version against
the exact prior request on record: strictly monotonic, idempotent on an
identical retry, and permanently closed once the scope has been finalized.
``finalize`` durably records the terminal `CancellationResultV1` for a
request, fenced the same way against the observation it is finalizing from,
and only accepts an observation whose phase legally binds to the claimed
outcome (see ``validate_cancellation_finalization``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError
from dagvane.ports.runtime import parse_iso_ms

CANCELLATION_PORT_API_VERSION = 1


def validate_api_version(value: object, *, ctx: str) -> int:
    """Validate ``value`` as the exact supported cancellation port API version."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{ctx}: must be a plain int")
    if value != CANCELLATION_PORT_API_VERSION:
        raise SpecError(f"{ctx}: unsupported cancellation port API version")
    return value


def _validate_token(value: object, *, ctx: str) -> int:
    """Validate a generation/fencing token or request version: a plain int >= 1.

    The error text never reflects ``value`` or its runtime type name: both
    are attacker-controlled (a crafted class can set an arbitrary
    ``__name__``), so the message is constant per ``ctx``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{ctx}: must be a plain int")
    if value < 1:
        raise SpecError(f"{ctx}: must be >= 1")
    return value


def _validate_owner_key(value: object, *, ctx: str) -> str:
    """Validate an opaque owner key without ever reflecting it in error text."""
    try:
        return validate_filesystem_id(value, ctx=ctx)
    except SpecError:
        raise SpecError(f"{ctx}: invalid opaque owner key") from None


def _validate_evidence_ref(value: object, *, ctx: str) -> str:
    """Validate a bounded opaque quiescence evidence reference.

    Same shape contract as a canonical filesystem id, but never reflected in
    error text: the ref may point at provider-controlled diagnostic data.
    """
    try:
        return validate_filesystem_id(value, ctx=ctx)
    except SpecError:
        raise SpecError(f"{ctx}: invalid opaque quiescence evidence reference") from None


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CancellationReasonV1(StrEnum):
    """Closed, vendor-neutral cancellation cause. No free-form reason in V1."""

    USER = "user"
    BUDGET = "budget"
    DEADLINE = "deadline"
    SHUTDOWN = "shutdown"


class CancellationPhaseV1(StrEnum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    STOPPING = "stopping"
    QUIESCENT = "quiescent"
    CLEANUP_INCOMPLETE = "cleanup_incomplete"


class CancellationOutcomeV1(StrEnum):
    """Closed set of terminal cancellation outcomes.

    ``CLEANUP_INCOMPLETE`` is a recoverable, nonterminal ``CancellationPhaseV1``
    (``STOPPING -> CLEANUP_INCOMPLETE -> QUIESCENT``), never a
    ``CancellationResultV1`` outcome: no terminal result is ever persisted
    while a scope sits in that phase, and the only outcome a durable
    implementation may finalize is ``CANCELLED``, from ``QUIESCENT``.
    """

    CANCELLED = "cancelled"


CANCELLATION_PHASE_TRANSITIONS: Mapping[CancellationPhaseV1, frozenset[CancellationPhaseV1]] = {
    # REQUESTED is entered only by the atomic request_cancellation operation,
    # never by the phase-only advance operation.
    CancellationPhaseV1.NOT_REQUESTED: frozenset(),
    CancellationPhaseV1.REQUESTED: frozenset({CancellationPhaseV1.STOPPING}),
    CancellationPhaseV1.STOPPING: frozenset(
        {CancellationPhaseV1.QUIESCENT, CancellationPhaseV1.CLEANUP_INCOMPLETE}
    ),
    CancellationPhaseV1.QUIESCENT: frozenset(),
    CancellationPhaseV1.CLEANUP_INCOMPLETE: frozenset({CancellationPhaseV1.QUIESCENT}),
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CancellationScopeV1:
    """Identity of one cancellable unit: a run (or sub-scope) pinned by generation.

    ``generation`` is the fencing token: a stale generation must never be
    advanced. ``owner_key`` is an opaque authority handle; it is validated
    like any canonical filesystem id but is never reflected by ``repr`` or by
    validation error text.
    """

    scope_id: str
    run_id: str
    generation: int
    owner_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_id", validate_filesystem_id(self.scope_id, ctx="scope_id")
        )
        object.__setattr__(self, "run_id", validate_filesystem_id(self.run_id, ctx="run_id"))
        object.__setattr__(
            self, "generation", _validate_token(self.generation, ctx="generation")
        )
        object.__setattr__(
            self, "owner_key", _validate_owner_key(self.owner_key, ctx="owner_key")
        )

    def __repr__(self) -> str:
        return (
            f"CancellationScopeV1(scope_id={self.scope_id!r}, run_id={self.run_id!r}, "
            f"generation={self.generation!r}, owner_key=[redacted])"
        )


@dataclass(frozen=True, slots=True)
class CancellationRequestV1:
    """One durable request to cancel ``scope``, at a monotonic version."""

    scope: CancellationScopeV1
    request_version: int
    requested_at: str
    reason: CancellationReasonV1

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CancellationScopeV1):
            raise SpecError("CancellationRequestV1.scope: must be a CancellationScopeV1")
        object.__setattr__(
            self,
            "request_version",
            _validate_token(self.request_version, ctx="request_version"),
        )
        if not isinstance(self.requested_at, str):
            raise SpecError("requested_at: must be a string")
        try:
            parse_iso_ms(self.requested_at)
        except SpecError:
            # parse_iso_ms reflects the raw value in its own error text; V1
            # requires a constant, nonreflecting message here instead.
            raise SpecError("requested_at: must be a valid ISO-8601 UTC timestamp") from None
        if not isinstance(self.reason, CancellationReasonV1):
            raise SpecError("reason: must be a CancellationReasonV1 member")


@dataclass(frozen=True, slots=True)
class CancellationObservationV1:
    """A point-in-time read of ``scope``'s cancellation phase."""

    scope: CancellationScopeV1
    observed_request_version: int | None
    phase: CancellationPhaseV1

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CancellationScopeV1):
            raise SpecError("CancellationObservationV1.scope: must be a CancellationScopeV1")
        if not isinstance(self.phase, CancellationPhaseV1):
            raise SpecError("phase: must be a CancellationPhaseV1 member")
        if self.observed_request_version is not None:
            object.__setattr__(
                self,
                "observed_request_version",
                _validate_token(self.observed_request_version, ctx="observed_request_version"),
            )
        if self.phase is CancellationPhaseV1.NOT_REQUESTED:
            if self.observed_request_version is not None:
                raise SpecError("NOT_REQUESTED must not carry an observed_request_version")
        elif self.observed_request_version is None:
            raise SpecError(f"phase {self.phase.value} requires an observed_request_version")


def advance_cancellation_observation(
    current: CancellationObservationV1, target: CancellationObservationV1
) -> CancellationObservationV1:
    """Validate a monotonic ``current`` -> ``target`` observation transition.

    Returns ``target`` on success. An identical retry (same phase, same
    ``observed_request_version``) is idempotent. Any other transition must:
    bind the exact same scope by full value -- ``scope_id``, ``run_id``,
    ``generation`` and ``owner_key`` together, so a stale generation or a
    substituted authority handle is rejected here before any phase check
    runs -- move strictly along ``CANCELLATION_PHASE_TRANSITIONS``, and leave
    ``observed_request_version`` exactly unchanged: a phase-only advance
    never changes the request version, since request-version changes are
    exclusively atomic ``request_cancellation`` operations.
    """
    if current.scope != target.scope:
        raise SpecError("cancellation observation: scope binding mismatch")
    if current.phase == target.phase and (
        current.observed_request_version == target.observed_request_version
    ):
        return target
    if target.phase not in CANCELLATION_PHASE_TRANSITIONS[current.phase]:
        raise SpecError(
            f"illegal cancellation phase transition {current.phase.value} -> "
            f"{target.phase.value}"
        )
    if target.observed_request_version != current.observed_request_version:
        raise SpecError(
            "cancellation observation: phase advance must not change request_version"
        )
    return target


def validate_cancellation_request_linearization(
    prior_request: CancellationRequestV1 | None,
    request: CancellationRequestV1,
    *,
    current_observation: CancellationObservationV1,
    scope_finalized: bool,
) -> CancellationObservationV1:
    """Validate that ``request`` may legally linearize as the next durable
    cancellation request for its scope, and return the observation that must
    be durably recorded alongside it in the same atomic write.

    ``prior_request`` is the exact request currently on durable record for
    this scope (``None`` if none yet). ``current_observation`` is the exact
    observation currently on durable record; ``request`` (and, when given,
    ``prior_request``) must bind to that same scope by full value, including
    ``owner_key`` -- a substituted authority handle with matching
    ids/generation fails closed here. ``scope_finalized`` reports whether any
    ``CancellationResultV1`` has ever been durably recorded for this scope: a
    finalized scope never reopens, no matter the requested version.

    First request (``prior_request is None``): ``current_observation`` must
    still be ``NOT_REQUESTED`` and ``request.request_version`` must be
    exactly ``1``; the returned observation is ``REQUESTED`` at that version.

    Next request (``prior_request`` given): an identical retry (same
    version, equal content) is idempotent and returns ``current_observation``
    unchanged. A conflicting request at the same version, a version
    rollback, or a skipped version all raise. A strictly-next version
    (``prior_request.request_version + 1``) returns a new observation at the
    *same* phase with ``observed_request_version`` bumped -- the phase
    itself only ever advances through ``advance()``.
    """
    if request.scope != current_observation.scope:
        raise SpecError("cancellation request: scope binding mismatch")
    if scope_finalized:
        raise SpecError("cancellation request: scope already finalized cancellation")
    if current_observation.phase is CancellationPhaseV1.QUIESCENT:
        raise SpecError("cancellation request: scope already reached a terminal phase")

    if prior_request is None:
        if current_observation.phase is not CancellationPhaseV1.NOT_REQUESTED:
            raise SpecError("cancellation request: scope already has a durable request")
        if request.request_version != 1:
            raise SpecError("cancellation request: first request_version must be 1")
        return CancellationObservationV1(
            scope=request.scope,
            observed_request_version=1,
            phase=CancellationPhaseV1.REQUESTED,
        )

    if request.scope != prior_request.scope:
        raise SpecError("cancellation request: scope binding mismatch")
    if current_observation.phase is CancellationPhaseV1.NOT_REQUESTED:
        raise SpecError("cancellation request: observation missing prior request")
    if current_observation.observed_request_version != prior_request.request_version:
        raise SpecError("cancellation request: observation/request version mismatch")

    if request.request_version == prior_request.request_version:
        if request == prior_request:
            return current_observation
        raise SpecError("cancellation request: conflicting request at same version")
    if request.request_version < prior_request.request_version:
        raise SpecError("cancellation request: request_version must not go backwards")
    if request.request_version != prior_request.request_version + 1:
        raise SpecError("cancellation request: request_version must not skip")

    return CancellationObservationV1(
        scope=request.scope,
        observed_request_version=request.request_version,
        phase=current_observation.phase,
    )


@dataclass(frozen=True, slots=True)
class CancellationResultV1:
    """Terminal cancellation outcome for one ``request_version``.

    ``CANCELLED`` is the only legal outcome and requires proven quiescence
    (``quiescent=True``) and a ``quiescence_evidence_ref``: a terminal
    success is never claimed before quiescence is proven, and that proof
    must be inspectable through the (bounded, opaque, redacted) evidence ref
    rather than asserted by a bare bool alone. A scope stuck in the
    nonterminal ``CLEANUP_INCOMPLETE`` phase never has a
    ``CancellationResultV1`` recorded for it -- there is nothing to
    construct until the scope reaches ``QUIESCENT``.
    """

    scope: CancellationScopeV1
    request_version: int
    outcome: CancellationOutcomeV1
    quiescent: bool
    quiescence_evidence_ref: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CancellationScopeV1):
            raise SpecError("CancellationResultV1.scope: must be a CancellationScopeV1")
        object.__setattr__(
            self,
            "request_version",
            _validate_token(self.request_version, ctx="request_version"),
        )
        if not isinstance(self.outcome, CancellationOutcomeV1):
            raise SpecError("outcome: must be a CancellationOutcomeV1 member")
        if not isinstance(self.quiescent, bool):
            raise SpecError("quiescent: must be a bool")
        if self.quiescence_evidence_ref is not None:
            object.__setattr__(
                self,
                "quiescence_evidence_ref",
                _validate_evidence_ref(
                    self.quiescence_evidence_ref, ctx="quiescence_evidence_ref"
                ),
            )
        if not self.quiescent:
            raise SpecError("CANCELLED requires proven quiescence (quiescent=True)")
        if self.quiescence_evidence_ref is None:
            raise SpecError("CANCELLED requires a quiescence_evidence_ref")

    def __repr__(self) -> str:
        ref_repr = "None" if self.quiescence_evidence_ref is None else "[redacted]"
        return (
            f"CancellationResultV1(scope={self.scope!r}, "
            f"request_version={self.request_version!r}, outcome={self.outcome!r}, "
            f"quiescent={self.quiescent!r}, quiescence_evidence_ref={ref_repr})"
        )


def validate_cancellation_finalization(
    observation: CancellationObservationV1, result: CancellationResultV1
) -> CancellationResultV1:
    """Validate that ``result`` may legally finalize from ``observation``.

    Returns ``result`` on success. The scope must match ``observation.scope``
    exactly by full value -- ``scope_id``, ``run_id``, ``generation`` and
    ``owner_key`` together, so a substituted authority handle with the same
    identifiers/generation fails closed -- ``result.request_version`` must
    equal ``observation.observed_request_version``, and the phase must
    legally bind to the claimed outcome: ``CANCELLED`` (the only outcome)
    only from a matching ``QUIESCENT`` observation. A ``CLEANUP_INCOMPLETE``
    observation never legally finalizes anything -- it is a recoverable
    nonterminal phase, not a terminal binding; the scope must first advance
    to ``QUIESCENT``.
    """
    if observation.scope != result.scope:
        raise SpecError("cancellation finalization: scope binding mismatch")
    if observation.observed_request_version != result.request_version:
        raise SpecError("cancellation finalization: request_version mismatch")
    if observation.phase is not CancellationPhaseV1.QUIESCENT:
        raise SpecError("CANCELLED finalization requires a matching QUIESCENT observation")
    return result


@dataclass(frozen=True, slots=True)
class CancellationSnapshotV1:
    """One atomic, point-in-time durable read of a scope's full cancellation
    state -- the current observation, the durably linearized request (if
    any), and the terminal result (if finalized) -- read together as a
    single consistent unit. This is what ``load_snapshot`` returns, and it is
    the only way to inspect a terminal ``CancellationResultV1``: there is no
    separate terminal-result read method.

    Field combinations are strictly matrixed against ``observation.phase``:
    at ``NOT_REQUESTED``, ``request`` and ``terminal_result`` must both be
    ``None``; at any later phase, ``request`` must be present and its
    ``request_version`` must equal ``observation.observed_request_version``;
    ``terminal_result``, when present, must legally finalize from
    ``observation`` (see ``validate_cancellation_finalization``), so it can
    only ever appear together with a ``QUIESCENT`` observation. Every present
    field must bind to the exact same ``scope`` by full value, including
    ``owner_key``.
    """

    scope: CancellationScopeV1
    request: CancellationRequestV1 | None
    observation: CancellationObservationV1
    terminal_result: CancellationResultV1 | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, CancellationScopeV1):
            raise SpecError("CancellationSnapshotV1.scope: must be a CancellationScopeV1")
        if not isinstance(self.observation, CancellationObservationV1):
            raise SpecError(
                "CancellationSnapshotV1.observation: must be a CancellationObservationV1"
            )
        if self.observation.scope != self.scope:
            raise SpecError("cancellation snapshot: observation scope binding mismatch")
        if self.request is not None:
            if not isinstance(self.request, CancellationRequestV1):
                raise SpecError(
                    "CancellationSnapshotV1.request: must be a CancellationRequestV1 or None"
                )
            if self.request.scope != self.scope:
                raise SpecError("cancellation snapshot: request scope binding mismatch")
            if self.request.request_version != self.observation.observed_request_version:
                raise SpecError("cancellation snapshot: request/observation version mismatch")
        if self.observation.phase is CancellationPhaseV1.NOT_REQUESTED:
            if self.request is not None:
                raise SpecError("cancellation snapshot: NOT_REQUESTED must not carry a request")
            if self.terminal_result is not None:
                raise SpecError(
                    "cancellation snapshot: NOT_REQUESTED must not carry a terminal_result"
                )
        elif self.request is None:
            raise SpecError(
                f"cancellation snapshot: phase {self.observation.phase.value} requires a "
                "durable request"
            )
        if self.terminal_result is not None:
            if not isinstance(self.terminal_result, CancellationResultV1):
                raise SpecError(
                    "CancellationSnapshotV1.terminal_result: must be a CancellationResultV1 "
                    "or None"
                )
            validate_cancellation_finalization(self.observation, self.terminal_result)

    def __repr__(self) -> str:
        request_repr = "None" if self.request is None else repr(self.request)
        result_repr = "None" if self.terminal_result is None else repr(self.terminal_result)
        return (
            f"CancellationSnapshotV1(scope={self.scope!r}, request={request_repr}, "
            f"observation={self.observation!r}, terminal_result={result_repr})"
        )


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


class CancellationPortV1(Protocol):
    """Durable cancellation port contract (interface only; no implementation).

    Every mutating method is a single atomic compare-and-swap keyed on the
    caller's expected fencing token or prior state: a mismatch must raise
    rather than silently overwrite a concurrent writer's effect. Methods only
    read or record state; none of them terminates a process.

    Persistence scope (V1 contract, no adapter yet): a conforming
    implementation must durably survive process restart for, per scope,
    the current ``generation``, the linearized durable request, the
    current phase observation, the terminal result's
    ``quiescence_evidence_ref`` (or its absence), and the terminal outcome
    itself once finalized. Building that durable adapter is future work;
    this Protocol only fixes what must be true of it.

    ``load_snapshot`` is the authoritative atomic read for crash/restart
    recovery: it returns the request, observation and terminal result
    together as one consistent ``CancellationSnapshotV1``, closing the
    window in which separate ``load_request``/``observe`` calls could
    observe two different points in time. A conforming implementation must
    keep ``load_scope``, ``load_request`` and ``observe`` in permanent
    agreement with what ``load_snapshot`` would return for the same scope --
    the simplest way to guarantee that is to implement all of them as thin
    projections over the same underlying atomic read ``load_snapshot`` uses.
    """

    def load_scope(self, *, scope_id: str, run_id: str) -> CancellationScopeV1 | None:
        """Return the durable scope record, or ``None`` if none exists yet."""
        ...

    def load_request(self, scope: CancellationScopeV1) -> CancellationRequestV1 | None:
        """Return the durably recorded cancellation request for ``scope``, or
        ``None`` if none has been linearized yet.

        A caller recovering after a crash or restart uses this together with
        ``observe`` to discover the exact prior request and observation
        before retrying ``request_cancellation`` -- it must never reconstruct
        that intent from a fresh, non-durable default, and a conforming
        implementation must never let this disagree with ``observe`` about
        whether a request exists (see ``request_cancellation``).
        """
        ...

    def observe(self, scope: CancellationScopeV1) -> CancellationObservationV1:
        """Return the current durable observation for ``scope``."""
        ...

    def request_cancellation(
        self,
        request: CancellationRequestV1,
        *,
        expected_prior_request: CancellationRequestV1 | None,
        expected_current_observation: CancellationObservationV1,
    ) -> tuple[CancellationRequestV1, CancellationObservationV1]:
        """Atomically linearize ``request`` as the next durable cancellation
        request for its scope and durably record the observation that
        results, in a single CAS-fenced write.

        This is the *only* legal way to write a durable request or to move
        phase ``NOT_REQUESTED -> REQUESTED``: request persistence and that
        phase flip must never be two unrelated writes, so no conforming
        implementation can expose a state where a request is durably
        recorded but ``observe``/``load_request`` disagree about it.

        ``expected_prior_request`` must equal the exact request currently on
        durable record (``None`` for the first request);
        ``expected_current_observation`` must equal the exact observation
        currently on durable record -- both matched by full value, including
        ``owner_key``, so a request whose scope substitutes a different
        opaque authority handle under the same ids/generation is rejected.
        Either mismatch raises. See
        ``validate_cancellation_request_linearization`` for the full
        monotonicity, idempotence and terminal-reopen contract: the first
        request must be version 1 from ``NOT_REQUESTED``; each later request
        must be an identical retry (idempotent) or the exact next version
        (never a rollback, same-version conflict, or skip); and once any
        ``CancellationResultV1`` has been finalized for this scope, every
        subsequent call must raise -- a finalized scope never reopens.
        """
        ...

    def advance(
        self,
        observation: CancellationObservationV1,
        *,
        expected_current: CancellationObservationV1,
    ) -> CancellationObservationV1:
        """Durably record a monotonic phase advance to ``observation`` iff
        the scope's stored observation currently equals exactly
        ``expected_current`` (the full scope -- including ``owner_key`` --
        ``observed_request_version`` and phase together, by value equality).
        Generation agreement alone is not sufficient fencing: two callers in
        the same generation and request version can still race different
        believed-current phases, so the full prior observation must match.
        Implementations must validate the transition (see
        ``advance_cancellation_observation``) before committing and must
        raise on any CAS mismatch or illegal transition. A retry with
        ``observation == expected_current ==`` the already-stored value is
        idempotent. Target phase ``REQUESTED`` is never legal here: entering
        it only happens inside ``request_cancellation``, atomically bound to
        the durable request it belongs to.
        """
        ...

    def finalize(
        self,
        result: CancellationResultV1,
        *,
        expected_observation: CancellationObservationV1,
    ) -> CancellationResultV1:
        """Durably record the terminal ``result`` iff the scope's stored
        observation currently equals exactly ``expected_observation`` and
        that observation legally binds to ``result`` (see
        ``validate_cancellation_finalization``): ``CANCELLED`` -- the only
        outcome -- only from a matching ``QUIESCENT`` observation. A scope
        sitting in ``CLEANUP_INCOMPLETE`` can never be finalized from there;
        it must first ``advance`` to ``QUIESCENT``. Must raise on any CAS
        mismatch or illegal binding. An identical retry of an
        already-finalized ``request_version`` (identical ``result`` and
        ``expected_observation``) is idempotent and returns the recorded
        result unchanged; any conflicting or new terminal mutation after
        finalization -- a different result, a mismatched
        ``expected_observation``, or any ``request_cancellation``/``advance``
        call for this scope -- must raise rather than mutate the recorded
        terminal outcome. Once finalized, the terminal result is inspectable
        only via ``load_snapshot``; there is no separate terminal-result read
        method.
        """
        ...

    def load_snapshot(self, scope: CancellationScopeV1) -> CancellationSnapshotV1:
        """Atomically return the full durable state for ``scope`` -- the
        current observation, the durably recorded request (if any) and the
        terminal result (if finalized) -- read together as a single
        consistent ``CancellationSnapshotV1``.

        This is the authoritative read for crash/restart recovery: a caller
        rebuilding ``expected_prior_request``/``expected_current_observation``
        for a subsequent ``request_cancellation``, ``advance`` or
        ``finalize`` call should prefer this single call over composing
        ``load_request`` and ``observe`` separately, precisely because it
        removes the window in which they could disagree. It is also the only
        way to read a terminal ``CancellationResultV1`` once finalized.
        """
        ...
