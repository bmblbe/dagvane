"""Pure lack-of-progress and escalation contract (R1-G0, API v1).

The port accepts only structured identities and content digests.  Provider
output is deliberately outside this boundary: callers must validate review
documents and derive a stable finding fingerprint before constructing these
records.  This keeps raw provider text out of representations and validation
errors.

Candidate commits are provenance, not progress.  Progress is credited only
for a newly satisfied acceptance subject or an evidenced closure of a finding
that is currently unresolved.  Unrelated progress never erases another
finding's observation history.

State is bound to the exact policy that produced it (a stable digest over
every decision-affecting field) and, once ``BLOCKED``, carries that terminal
disposition durably.  A restart that supplies a mismatched policy, or any
attempt to route or consume new evidence past ``BLOCKED``, fails closed
instead of silently re-deriving a looser answer from raw thresholds.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Protocol

API_VERSION: Final = 1

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class EscalationContractError(ValueError):
    """An invalid v1 escalation record.

    Messages name only the violated field or invariant.  Rejected values are
    never interpolated because they may have originated in provider output.
    """


class FindingSeverity(StrEnum):
    """Blocking review severities tracked by this port."""

    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"


class ProgressKind(StrEnum):
    """Closed set of evidence that can be meaningful progress."""

    ACCEPTANCE_GAIN = "ACCEPTANCE_GAIN"
    FINDING_CLOSED = "FINDING_CLOSED"


class EscalationTier(StrEnum):
    """The bounded R1-G implementation-resource ladder."""

    STANDARD = "STANDARD"
    STRONG = "STRONG"
    CRITICAL = "CRITICAL"


class EscalationAction(StrEnum):
    ROUTE = "ROUTE"
    BLOCKED = "BLOCKED"


def _require_sha256(value: object, field: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise EscalationContractError(f"{field} must be a lowercase SHA-256 digest")


def _require_git_sha(value: object, field: str) -> None:
    if not isinstance(value, str) or _GIT_SHA_RE.fullmatch(value) is None:
        raise EscalationContractError(f"{field} must be a full lowercase Git SHA")


def _require_plain_int(value: object, field: str, *, minimum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise EscalationContractError(f"{field} must be an integer >= {minimum}")


def _require_tuple(value: object, field: str) -> None:
    if not isinstance(value, tuple):
        raise EscalationContractError(f"{field} must be a tuple")


def _require_api_version(value: object) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value != API_VERSION
    ):
        raise EscalationContractError("unsupported escalation API version")


def _require_optional_sha256(value: object, field: str) -> None:
    if value is not None:
        _require_sha256(value, field)


@dataclass(frozen=True, slots=True)
class FindingIdentity:
    """Stable semantic identity, independent of candidate SHA and wording.

    ``fingerprint_sha256`` is computed by the validated review boundary from
    canonical structured fields.  This port never receives the description or
    other raw provider text.
    """

    fingerprint_sha256: str

    def __post_init__(self) -> None:
        _require_sha256(self.fingerprint_sha256, "finding fingerprint")


@dataclass(frozen=True, slots=True)
class FindingObservation:
    """One validated blocking observation bound to candidate and evidence."""

    finding: FindingIdentity
    severity: FindingSeverity
    candidate_sha: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.finding, FindingIdentity):
            raise EscalationContractError("finding observation identity is invalid")
        if not isinstance(self.severity, FindingSeverity):
            raise EscalationContractError("finding observation severity is invalid")
        _require_git_sha(self.candidate_sha, "finding observation candidate_sha")
        _require_sha256(self.evidence_sha256, "finding observation evidence_sha256")


@dataclass(frozen=True, slots=True)
class FindingHistory:
    """Append-only observations for one currently unresolved finding."""

    finding: FindingIdentity
    observations: tuple[FindingObservation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.finding, FindingIdentity):
            raise EscalationContractError("finding history identity is invalid")
        _require_tuple(self.observations, "finding history observations")
        if not self.observations:
            raise EscalationContractError("finding history observations must not be empty")
        occurrences: set[tuple[str, str]] = set()
        for observation in self.observations:
            if not isinstance(observation, FindingObservation):
                raise EscalationContractError("finding history observation is invalid")
            if observation.finding != self.finding:
                raise EscalationContractError("finding history identities must match")
            occurrence = (observation.candidate_sha, observation.evidence_sha256)
            if occurrence in occurrences:
                raise EscalationContractError(
                    "finding history candidate/evidence pairs must be unique"
                )
            occurrences.add(occurrence)

    @property
    def failure_count(self) -> int:
        return len(self.observations)


@dataclass(frozen=True, slots=True)
class ProgressEvidence:
    """A structured claim of meaningful progress, with no descriptive text.

    For ``ACCEPTANCE_GAIN``, ``subject_sha256`` identifies the immutable
    acceptance condition.  For ``FINDING_CLOSED``, it is the corresponding
    ``FindingIdentity.fingerprint_sha256``.
    """

    kind: ProgressKind
    subject_sha256: str
    candidate_sha: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProgressKind):
            raise EscalationContractError("progress kind is invalid")
        _require_sha256(self.subject_sha256, "progress subject_sha256")
        _require_git_sha(self.candidate_sha, "progress candidate_sha")
        _require_sha256(self.evidence_sha256, "progress evidence_sha256")


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    """Validated evidence for one candidate evaluation.

    A different ``candidate_sha`` with empty ``progress`` is explicitly a
    no-progress evaluation.
    """

    candidate_sha: str
    findings: tuple[FindingObservation, ...] = ()
    progress: tuple[ProgressEvidence, ...] = ()

    def __post_init__(self) -> None:
        _require_git_sha(self.candidate_sha, "evaluation candidate_sha")
        _require_tuple(self.findings, "evaluation findings")
        _require_tuple(self.progress, "evaluation progress")

        finding_ids: set[str] = set()
        for observation in self.findings:
            if not isinstance(observation, FindingObservation):
                raise EscalationContractError("evaluation finding is invalid")
            if observation.candidate_sha != self.candidate_sha:
                raise EscalationContractError(
                    "evaluation finding candidate_sha must match evaluation"
                )
            fingerprint = observation.finding.fingerprint_sha256
            if fingerprint in finding_ids:
                raise EscalationContractError(
                    "evaluation finding identities must be unique"
                )
            finding_ids.add(fingerprint)

        progress_keys: set[tuple[ProgressKind, str]] = set()
        closed_ids: set[str] = set()
        for item in self.progress:
            if not isinstance(item, ProgressEvidence):
                raise EscalationContractError("evaluation progress item is invalid")
            if item.candidate_sha != self.candidate_sha:
                raise EscalationContractError(
                    "evaluation progress candidate_sha must match evaluation"
                )
            key = (item.kind, item.subject_sha256)
            if key in progress_keys:
                raise EscalationContractError(
                    "evaluation progress subjects must be unique per kind"
                )
            progress_keys.add(key)
            if item.kind is ProgressKind.FINDING_CLOSED:
                closed_ids.add(item.subject_sha256)

        if finding_ids & closed_ids:
            raise EscalationContractError(
                "an evaluation cannot observe and close the same finding"
            )


@dataclass(frozen=True, slots=True)
class EscalationState:
    """Immutable reducer state; safe to persist with an API version wrapper.

    ``policy_digest`` binds this state to the exact policy that produced it;
    it is absent before the first evaluation and bound forever after.
    ``terminal_action``/``terminal_evidence``/``terminal_meaningful_progress``
    persist the reducer's own ``BLOCKED`` disposition once reached, including
    whether that exact terminal evaluation carried newly credited progress,
    so restart never has to re-derive terminality (or that flag) from raw
    thresholds under whatever policy is handed back.
    """

    evaluation_count: int = 0
    consecutive_no_progress: int = 0
    unresolved: tuple[FindingHistory, ...] = ()
    credited_progress: tuple[ProgressEvidence, ...] = ()
    policy_digest: str | None = None
    terminal_action: EscalationAction | None = None
    terminal_evidence: EvaluationEvidence | None = None
    terminal_meaningful_progress: bool | None = None

    def __post_init__(self) -> None:
        _require_plain_int(self.evaluation_count, "evaluation_count", minimum=0)
        _require_plain_int(
            self.consecutive_no_progress,
            "consecutive_no_progress",
            minimum=0,
        )
        if self.consecutive_no_progress > self.evaluation_count:
            raise EscalationContractError(
                "consecutive_no_progress must not exceed evaluation_count"
            )
        _require_tuple(self.unresolved, "unresolved")
        _require_tuple(self.credited_progress, "credited_progress")

        unresolved_ids: list[str] = []
        for history in self.unresolved:
            if not isinstance(history, FindingHistory):
                raise EscalationContractError("unresolved history is invalid")
            if history.failure_count > self.evaluation_count:
                raise EscalationContractError(
                    "finding history cannot exceed evaluation_count"
                )
            unresolved_ids.append(history.finding.fingerprint_sha256)
        if unresolved_ids != sorted(set(unresolved_ids)):
            raise EscalationContractError(
                "unresolved histories must be unique and fingerprint-sorted"
            )

        credit_keys: list[tuple[str, str, str]] = []
        for item in self.credited_progress:
            if not isinstance(item, ProgressEvidence):
                raise EscalationContractError("credited progress item is invalid")
            credit_keys.append(
                (item.kind.value, item.subject_sha256, item.evidence_sha256)
            )
        if credit_keys != sorted(set(credit_keys)):
            raise EscalationContractError(
                "credited progress must be unique and canonically sorted"
            )

        _require_optional_sha256(self.policy_digest, "policy_digest")
        if self.evaluation_count == 0 and self.policy_digest is not None:
            raise EscalationContractError(
                "policy_digest must be absent before any evaluation"
            )
        if self.evaluation_count > 0 and self.policy_digest is None:
            raise EscalationContractError(
                "policy_digest must be bound after the first evaluation"
            )

        if (
            self.terminal_action is not None
            and self.terminal_action is not EscalationAction.BLOCKED
        ):
            raise EscalationContractError(
                "terminal_action must be BLOCKED or absent"
            )
        if self.terminal_action is None:
            has_terminal_metadata = (
                self.terminal_evidence is not None
                or self.terminal_meaningful_progress is not None
            )
            if has_terminal_metadata:
                raise EscalationContractError(
                    "terminal_action and terminal_evidence must be set together"
                )
        else:
            if self.terminal_evidence is None:
                raise EscalationContractError(
                    "terminal_action and terminal_evidence must be set together"
                )
            if not isinstance(self.terminal_evidence, EvaluationEvidence):
                raise EscalationContractError("terminal_evidence is invalid")
            if not isinstance(self.terminal_meaningful_progress, bool):
                raise EscalationContractError(
                    "terminal_meaningful_progress must be set together with "
                    "terminal_action"
                )
        if self.terminal_action is not None and self.policy_digest is None:
            raise EscalationContractError(
                "a terminal state must carry a bound policy_digest"
            )


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    """Finite thresholds for STANDARD -> STRONG -> CRITICAL -> BLOCKED."""

    strong_after: int = 2
    critical_after: int = 3
    blocked_after: int = 4
    max_evaluations: int = 8

    def __post_init__(self) -> None:
        _require_plain_int(self.strong_after, "strong_after", minimum=2)
        _require_plain_int(self.critical_after, "critical_after", minimum=3)
        _require_plain_int(self.blocked_after, "blocked_after", minimum=4)
        _require_plain_int(self.max_evaluations, "max_evaluations", minimum=4)
        if not (
            self.strong_after
            < self.critical_after
            < self.blocked_after
            <= self.max_evaluations
        ):
            raise EscalationContractError(
                "policy thresholds must satisfy "
                "strong_after < critical_after < blocked_after <= max_evaluations"
            )


@dataclass(frozen=True, slots=True)
class EscalationRequest:
    """Versioned input boundary for an escalation decision."""

    state: EscalationState
    evidence: EvaluationEvidence
    policy: EscalationPolicy
    api_version: int = API_VERSION

    def __post_init__(self) -> None:
        _require_api_version(self.api_version)
        if not isinstance(self.state, EscalationState):
            raise EscalationContractError("escalation state is invalid")
        if not isinstance(self.evidence, EvaluationEvidence):
            raise EscalationContractError("evaluation evidence is invalid")
        if not isinstance(self.policy, EscalationPolicy):
            raise EscalationContractError("escalation policy is invalid")


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """Versioned deterministic result with the next immutable state."""

    action: EscalationAction
    tier: EscalationTier | None
    state: EscalationState
    meaningful_progress: bool
    pressure: int
    api_version: int = API_VERSION

    def __post_init__(self) -> None:
        _require_api_version(self.api_version)
        if not isinstance(self.action, EscalationAction):
            raise EscalationContractError("escalation action is invalid")
        if self.tier is not None and not isinstance(self.tier, EscalationTier):
            raise EscalationContractError("escalation tier is invalid")
        if self.action is EscalationAction.ROUTE and self.tier is None:
            raise EscalationContractError("ROUTE decisions require a tier")
        if self.action is EscalationAction.BLOCKED and self.tier is not None:
            raise EscalationContractError("BLOCKED decisions must not carry a tier")
        if not isinstance(self.state, EscalationState):
            raise EscalationContractError("decision state is invalid")
        if not isinstance(self.meaningful_progress, bool):
            raise EscalationContractError("meaningful_progress must be a boolean")
        _require_plain_int(self.pressure, "pressure", minimum=0)


class LackOfProgressPort(Protocol):
    """Vendor-neutral application seam for an R1-G v1 decision maker."""

    def decide(self, request: EscalationRequest, /) -> EscalationDecision: ...


def _credit_key(item: ProgressEvidence) -> tuple[str, str, str]:
    return (item.kind.value, item.subject_sha256, item.evidence_sha256)


def _policy_digest(policy: EscalationPolicy) -> str:
    """Stable digest over every decision-affecting policy field.

    Domain-separated and API-versioned so a future field addition or version
    bump cannot collide with an existing digest.
    """

    canonical = (
        f"escalation-policy:v{API_VERSION}:"
        f"{policy.strong_after}:{policy.critical_after}:"
        f"{policy.blocked_after}:{policy.max_evaluations}"
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _state_pressure(state: EscalationState) -> int:
    finding_pressure = max(
        (history.failure_count for history in state.unresolved), default=0
    )
    return max(state.consecutive_no_progress, finding_pressure)


def _terminal_decision(
    state: EscalationState, *, meaningful_progress: bool
) -> EscalationDecision:
    return EscalationDecision(
        action=EscalationAction.BLOCKED,
        tier=None,
        state=state,
        meaningful_progress=meaningful_progress,
        pressure=_state_pressure(state),
    )


def decide_escalation(request: EscalationRequest, /) -> EscalationDecision:
    """Apply one evaluation and choose the next tier deterministically.

    State is bound to the exact policy that produced it: a mismatched policy
    is rejected before anything else, with no state or evaluation mutation.
    Once ``state.terminal_action`` is ``BLOCKED`` it is monotonic — an exact
    repeat of the evidence that reached it replays the same decision without
    incrementing or crediting anything new; any other evidence is rejected
    explicitly rather than silently discarded.  The reducer is otherwise
    append-only except for an explicit, novel ``FINDING_CLOSED`` credit, which
    removes exactly the matching finding history.
    """

    state = request.state
    policy = request.policy
    evidence = request.evidence
    digest = _policy_digest(policy)

    if state.policy_digest is not None and state.policy_digest != digest:
        raise EscalationContractError(
            "escalation policy does not match the policy bound to this state"
        )

    if state.terminal_action is not None:
        if evidence == state.terminal_evidence:
            terminal_meaningful_progress = state.terminal_meaningful_progress
            assert terminal_meaningful_progress is not None
            return _terminal_decision(
                state, meaningful_progress=terminal_meaningful_progress
            )
        raise EscalationContractError(
            "a terminal escalation state cannot accept new evidence"
        )

    # A state that already crosses the bound threshold but was never marked
    # terminal cannot have come out of this reducer, which always marks the
    # crossing evaluation.  Treat it as forged/inconsistent input and fail
    # closed rather than let it be silently routed (or, worse, downgraded by
    # a finding closure) into looking active again.
    if (
        state.evaluation_count >= policy.max_evaluations
        or _state_pressure(state) >= policy.blocked_after
    ):
        raise EscalationContractError(
            "escalation state exceeds its policy bound without a terminal disposition"
        )

    histories = {
        history.finding.fingerprint_sha256: history for history in state.unresolved
    }
    credited = {_credit_key(item): item for item in state.credited_progress}
    newly_credited: list[ProgressEvidence] = []

    credited_acceptance = {
        item.subject_sha256
        for item in state.credited_progress
        if item.kind is ProgressKind.ACCEPTANCE_GAIN
    }
    for item in request.evidence.progress:
        key = _credit_key(item)
        if item.kind is ProgressKind.ACCEPTANCE_GAIN:
            # Re-passing an already credited condition is evidence, but it is
            # not a newly gained acceptance condition and cannot reset pressure.
            if item.subject_sha256 in credited_acceptance or key in credited:
                continue
            credited_acceptance.add(item.subject_sha256)
        else:
            if item.subject_sha256 not in histories:
                raise EscalationContractError(
                    "finding closure must reference an unresolved finding"
                )
            if key in credited:
                raise EscalationContractError(
                    "finding closure evidence must not be reused"
                )
            # Closure resets this identity only.  Other histories remain
            # untouched and still contribute their full pressure.
            del histories[item.subject_sha256]
        credited[key] = item
        newly_credited.append(item)

    for observation in request.evidence.findings:
        fingerprint = observation.finding.fingerprint_sha256
        existing = histories.get(fingerprint)
        if existing is None:
            histories[fingerprint] = FindingHistory(
                finding=observation.finding,
                observations=(observation,),
            )
            continue
        if any(
            prior.candidate_sha == observation.candidate_sha
            and prior.evidence_sha256 == observation.evidence_sha256
            for prior in existing.observations
        ):
            # Reprocessing identical review evidence does not grow finding
            # history, but the evaluation still counts as no progress below.
            continue
        histories[fingerprint] = FindingHistory(
            finding=existing.finding,
            observations=(*existing.observations, observation),
        )

    meaningful_progress = bool(newly_credited)
    next_state = EscalationState(
        evaluation_count=state.evaluation_count + 1,
        consecutive_no_progress=(
            0 if meaningful_progress else state.consecutive_no_progress + 1
        ),
        unresolved=tuple(histories[key] for key in sorted(histories)),
        credited_progress=tuple(credited[key] for key in sorted(credited)),
        policy_digest=digest,
    )
    pressure = _state_pressure(next_state)

    if (
        next_state.evaluation_count >= policy.max_evaluations
        or pressure >= policy.blocked_after
    ):
        next_state = replace(
            next_state,
            terminal_action=EscalationAction.BLOCKED,
            terminal_evidence=evidence,
            terminal_meaningful_progress=meaningful_progress,
        )
        return _terminal_decision(
            next_state, meaningful_progress=meaningful_progress
        )
    if pressure >= policy.critical_after:
        tier = EscalationTier.CRITICAL
    elif pressure >= policy.strong_after:
        tier = EscalationTier.STRONG
    else:
        tier = EscalationTier.STANDARD
    return EscalationDecision(
        action=EscalationAction.ROUTE,
        tier=tier,
        state=next_state,
        meaningful_progress=meaningful_progress,
        pressure=pressure,
    )
