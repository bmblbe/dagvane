"""Cancellation Port V1: construction validation, transition graph, CAS, terminal matrix.

Uses a tiny in-memory fake implementing ``CancellationPortV1`` purely to
exercise the compare-and-swap contract -- atomic ``request_cancellation``
fenced on the exact prior request and the exact current observation,
monotonic ``advance``, ``finalize`` only ``CANCELLED`` from ``QUIESCENT``,
and ``load_snapshot`` as the single consistent read; it is not a candidate
durable implementation. ``validate_cancellation_request_linearization`` is
also tested directly, without the fake.
"""

from __future__ import annotations

import pytest

from dagvane.domain.models import SpecError
from dagvane.ports.cancellation import (
    CANCELLATION_PHASE_TRANSITIONS,
    CANCELLATION_PORT_API_VERSION,
    CancellationObservationV1,
    CancellationOutcomeV1,
    CancellationPhaseV1,
    CancellationPortV1,
    CancellationReasonV1,
    CancellationRequestV1,
    CancellationResultV1,
    CancellationScopeV1,
    CancellationSnapshotV1,
    advance_cancellation_observation,
    validate_api_version,
    validate_cancellation_finalization,
    validate_cancellation_request_linearization,
)

VALID_SCOPE_KWARGS = {
    "scope_id": "run-01",
    "run_id": "run-01",
    "generation": 1,
    "owner_key": "owner-abc",
}


def make_scope(**overrides: object) -> CancellationScopeV1:
    kwargs = dict(VALID_SCOPE_KWARGS)
    kwargs.update(overrides)
    return CancellationScopeV1(**kwargs)  # type: ignore[arg-type]


def observation(
    scope: CancellationScopeV1,
    phase: CancellationPhaseV1,
    version: int | None,
) -> CancellationObservationV1:
    return CancellationObservationV1(
        scope=scope, observed_request_version=version, phase=phase
    )


def make_request(
    scope: CancellationScopeV1,
    version: int = 1,
    *,
    requested_at: str = "2026-08-16T00:00:00.000Z",
    reason: CancellationReasonV1 = CancellationReasonV1.USER,
) -> CancellationRequestV1:
    return CancellationRequestV1(
        scope=scope, request_version=version, requested_at=requested_at, reason=reason
    )


def make_result(
    scope: CancellationScopeV1,
    version: int = 1,
    *,
    quiescence_evidence_ref: str = "evidence-01",
) -> CancellationResultV1:
    return CancellationResultV1(
        scope=scope,
        request_version=version,
        outcome=CancellationOutcomeV1.CANCELLED,
        quiescent=True,
        quiescence_evidence_ref=quiescence_evidence_ref,
    )


# ---------------------------------------------------------------------------
# Valid construction
# ---------------------------------------------------------------------------


def test_valid_scope_construction() -> None:
    scope = make_scope()
    assert scope.scope_id == "run-01"
    assert scope.generation == 1


def test_valid_request_construction() -> None:
    scope = make_scope()
    request = make_request(scope)
    assert request.reason is CancellationReasonV1.USER


def test_valid_observation_construction() -> None:
    scope = make_scope()
    obs = observation(scope, CancellationPhaseV1.NOT_REQUESTED, None)
    assert obs.phase is CancellationPhaseV1.NOT_REQUESTED
    obs2 = observation(scope, CancellationPhaseV1.REQUESTED, 1)
    assert obs2.observed_request_version == 1


def test_valid_result_construction() -> None:
    scope = make_scope()
    cancelled = make_result(scope)
    assert cancelled.quiescent is True
    assert cancelled.quiescence_evidence_ref == "evidence-01"


def test_outcome_has_only_cancelled_member() -> None:
    assert list(CancellationOutcomeV1) == [CancellationOutcomeV1.CANCELLED]


# ---------------------------------------------------------------------------
# Invalid construction matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["scope_id", "run_id"])
@pytest.mark.parametrize(
    "value",
    [None, 123, True, [], "", ".", "-leading", "../escape", "a/b", "café", "a" * 65],
)
def test_scope_invalid_ids_rejected(field: str, value: object) -> None:
    with pytest.raises(SpecError):
        make_scope(**{field: value})


@pytest.mark.parametrize("value", [True, False, 1.5, "1", None, 0, -1])
def test_scope_invalid_generation_rejected(value: object) -> None:
    with pytest.raises(SpecError):
        make_scope(generation=value)


@pytest.mark.parametrize(
    "value", [None, 123, True, "", "../escape", "a/b", "café", "a" * 65]
)
def test_scope_invalid_owner_key_rejected(value: object) -> None:
    with pytest.raises(SpecError):
        make_scope(owner_key=value)


def test_request_invalid_scope_rejected() -> None:
    with pytest.raises(SpecError):
        CancellationRequestV1(
            scope="not-a-scope",  # type: ignore[arg-type]
            request_version=1,
            requested_at="2026-08-16T00:00:00.000Z",
            reason=CancellationReasonV1.USER,
        )


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1"])
def test_request_invalid_version_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationRequestV1(
            scope=scope,
            request_version=value,  # type: ignore[arg-type]
            requested_at="2026-08-16T00:00:00.000Z",
            reason=CancellationReasonV1.USER,
        )


@pytest.mark.parametrize("value", [None, 123, "not-a-timestamp", "2026-08-16"])
def test_request_invalid_timestamp_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationRequestV1(
            scope=scope,
            request_version=1,
            requested_at=value,  # type: ignore[arg-type]
            reason=CancellationReasonV1.USER,
        )


def test_request_naive_timestamp_rejected() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationRequestV1(
            scope=scope,
            request_version=1,
            requested_at="2026-08-16T00:00:00.000",
            reason=CancellationReasonV1.USER,
        )


@pytest.mark.parametrize("value", ["user", None, 1, object()])
def test_request_invalid_reason_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationRequestV1(
            scope=scope,
            request_version=1,
            requested_at="2026-08-16T00:00:00.000Z",
            reason=value,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", ["requested", None, 1, object()])
def test_observation_invalid_phase_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationObservationV1(
            scope=scope,
            observed_request_version=None,
            phase=value,  # type: ignore[arg-type]
        )


def test_observation_not_requested_with_version_rejected() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        observation(scope, CancellationPhaseV1.NOT_REQUESTED, 1)


def test_observation_requested_without_version_rejected() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        observation(scope, CancellationPhaseV1.REQUESTED, None)


@pytest.mark.parametrize("value", [True, False, 0, -1])
def test_observation_invalid_version_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        observation(scope, CancellationPhaseV1.REQUESTED, value)  # type: ignore[arg-type]


def test_result_invalid_matrix_not_quiescent_rejected() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationResultV1(
            scope=scope,
            request_version=1,
            outcome=CancellationOutcomeV1.CANCELLED,
            quiescent=False,
            quiescence_evidence_ref="evidence-01",
        )


def test_result_invalid_matrix_missing_evidence_ref_rejected() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationResultV1(
            scope=scope,
            request_version=1,
            outcome=CancellationOutcomeV1.CANCELLED,
            quiescent=True,
            quiescence_evidence_ref=None,
        )


@pytest.mark.parametrize("value", ["cancelled", None, 1, object()])
def test_result_invalid_outcome_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationResultV1(
            scope=scope,
            request_version=1,
            outcome=value,  # type: ignore[arg-type]
            quiescent=True,
            quiescence_evidence_ref="evidence-01",
        )


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_result_invalid_quiescent_type_rejected(value: object) -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationResultV1(
            scope=scope,
            request_version=1,
            outcome=CancellationOutcomeV1.CANCELLED,
            quiescent=value,  # type: ignore[arg-type]
            quiescence_evidence_ref="evidence-01",
        )


# ---------------------------------------------------------------------------
# Snapshot consistency matrix
# ---------------------------------------------------------------------------


def test_valid_snapshot_not_requested() -> None:
    scope = make_scope()
    snapshot = CancellationSnapshotV1(
        scope=scope,
        request=None,
        observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
        terminal_result=None,
    )
    assert snapshot.request is None
    assert snapshot.terminal_result is None


def test_valid_snapshot_finalized() -> None:
    scope = make_scope()
    result = make_result(scope)
    snapshot = CancellationSnapshotV1(
        scope=scope,
        request=make_request(scope),
        observation=observation(scope, CancellationPhaseV1.QUIESCENT, 1),
        terminal_result=result,
    )
    assert snapshot.terminal_result == result


def test_snapshot_rejects_request_at_not_requested() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationSnapshotV1(
            scope=scope,
            request=make_request(scope),
            observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
            terminal_result=None,
        )


def test_snapshot_rejects_missing_request_at_later_phase() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationSnapshotV1(
            scope=scope,
            request=None,
            observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            terminal_result=None,
        )


def test_snapshot_rejects_request_observation_version_mismatch() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationSnapshotV1(
            scope=scope,
            request=make_request(scope, 2),
            observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            terminal_result=None,
        )


def test_snapshot_rejects_observation_owner_key_substitution() -> None:
    scope = make_scope()
    imposter = make_scope(owner_key="owner-imposter")
    with pytest.raises(SpecError):
        CancellationSnapshotV1(
            scope=scope,
            request=None,
            observation=observation(imposter, CancellationPhaseV1.NOT_REQUESTED, None),
            terminal_result=None,
        )


def test_snapshot_rejects_terminal_result_from_non_quiescent() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        CancellationSnapshotV1(
            scope=scope,
            request=make_request(scope),
            observation=observation(scope, CancellationPhaseV1.CLEANUP_INCOMPLETE, 1),
            terminal_result=make_result(scope),
        )


# ---------------------------------------------------------------------------
# Transition graph
# ---------------------------------------------------------------------------


def test_transition_graph_legal_paths() -> None:
    scope = make_scope()
    requested = observation(scope, CancellationPhaseV1.REQUESTED, 1)
    stopping = observation(scope, CancellationPhaseV1.STOPPING, 1)
    quiescent = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    cleanup_incomplete = observation(scope, CancellationPhaseV1.CLEANUP_INCOMPLETE, 1)

    assert advance_cancellation_observation(requested, stopping) is stopping
    assert advance_cancellation_observation(stopping, quiescent) is quiescent
    assert advance_cancellation_observation(stopping, cleanup_incomplete) is cleanup_incomplete
    assert advance_cancellation_observation(cleanup_incomplete, quiescent) is quiescent


def test_transition_graph_matches_declared_map() -> None:
    scope = make_scope()
    for current_phase, allowed in CANCELLATION_PHASE_TRANSITIONS.items():
        current = observation(
            scope,
            current_phase,
            None if current_phase is CancellationPhaseV1.NOT_REQUESTED else 1,
        )
        for target_phase in CancellationPhaseV1:
            if target_phase == current_phase:
                continue
            target = observation(
                scope,
                target_phase,
                None if target_phase is CancellationPhaseV1.NOT_REQUESTED else 1,
            )
            if target_phase in allowed:
                assert advance_cancellation_observation(current, target) is target
            else:
                with pytest.raises(SpecError):
                    advance_cancellation_observation(current, target)


@pytest.mark.parametrize(
    ("current_phase", "target_phase"),
    [
        (CancellationPhaseV1.REQUESTED, CancellationPhaseV1.NOT_REQUESTED),
        (CancellationPhaseV1.STOPPING, CancellationPhaseV1.REQUESTED),
        (CancellationPhaseV1.QUIESCENT, CancellationPhaseV1.STOPPING),
        (CancellationPhaseV1.NOT_REQUESTED, CancellationPhaseV1.STOPPING),
        (CancellationPhaseV1.CLEANUP_INCOMPLETE, CancellationPhaseV1.STOPPING),
    ],
)
def test_transition_graph_illegal(
    current_phase: CancellationPhaseV1, target_phase: CancellationPhaseV1
) -> None:
    scope = make_scope()
    current = observation(
        scope, current_phase, None if current_phase is CancellationPhaseV1.NOT_REQUESTED else 1
    )
    target = observation(
        scope, target_phase, None if target_phase is CancellationPhaseV1.NOT_REQUESTED else 1
    )
    with pytest.raises(SpecError):
        advance_cancellation_observation(current, target)


def test_transition_rejects_stale_version() -> None:
    scope = make_scope()
    stopping_v2 = observation(scope, CancellationPhaseV1.STOPPING, 2)
    quiescent_v1 = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        advance_cancellation_observation(stopping_v2, quiescent_v1)


def test_transition_rejects_unlinearized_higher_version() -> None:
    scope = make_scope()
    requested_v1 = observation(scope, CancellationPhaseV1.REQUESTED, 1)
    stopping_v999 = observation(scope, CancellationPhaseV1.STOPPING, 999)
    with pytest.raises(SpecError):
        advance_cancellation_observation(requested_v1, stopping_v999)


def test_transition_rejects_generation_change() -> None:
    scope = make_scope(generation=1)
    other_generation_scope = make_scope(generation=2)
    current = observation(scope, CancellationPhaseV1.STOPPING, 1)
    target = observation(other_generation_scope, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        advance_cancellation_observation(current, target)


def test_transition_rejects_scope_identity_mismatch() -> None:
    scope_a = make_scope(scope_id="run-01", run_id="run-01")
    scope_b = make_scope(scope_id="run-02", run_id="run-02")
    current = observation(scope_a, CancellationPhaseV1.STOPPING, 1)
    target = observation(scope_b, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        advance_cancellation_observation(current, target)


def test_transition_rejects_owner_key_substitution() -> None:
    scope = make_scope()
    imposter = make_scope(owner_key="owner-imposter")
    current = observation(scope, CancellationPhaseV1.STOPPING, 1)
    target = observation(imposter, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        advance_cancellation_observation(current, target)


def test_transition_idempotent_retry() -> None:
    scope = make_scope()
    current = observation(scope, CancellationPhaseV1.STOPPING, 1)
    retry = observation(scope, CancellationPhaseV1.STOPPING, 1)
    assert advance_cancellation_observation(current, retry) is retry


# ---------------------------------------------------------------------------
# Request linearization (direct, no fake)
# ---------------------------------------------------------------------------


def test_linearization_first_request_returns_requested_observation() -> None:
    scope = make_scope()
    recorded = validate_cancellation_request_linearization(
        None,
        make_request(scope),
        current_observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
        scope_finalized=False,
    )
    assert recorded == observation(scope, CancellationPhaseV1.REQUESTED, 1)


def test_linearization_first_request_version_must_be_one() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            None,
            make_request(scope, 2),
            current_observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
            scope_finalized=False,
        )


def test_linearization_first_request_requires_not_requested_observation() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            None,
            make_request(scope),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            scope_finalized=False,
        )


def test_linearization_rejects_request_owner_key_substitution() -> None:
    scope = make_scope()
    imposter = make_scope(owner_key="owner-imposter")
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            None,
            make_request(imposter),
            current_observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
            scope_finalized=False,
        )


def test_linearization_rejects_prior_request_scope_mismatch() -> None:
    scope = make_scope()
    imposter = make_scope(owner_key="owner-imposter")
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(imposter),
            make_request(scope, 2),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            scope_finalized=False,
        )


def test_linearization_finalized_scope_rejects_even_identical_retry() -> None:
    scope = make_scope()
    prior = make_request(scope)
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            prior,
            make_request(scope),
            current_observation=observation(scope, CancellationPhaseV1.QUIESCENT, 1),
            scope_finalized=True,
        )


def test_linearization_rejects_terminal_phase_even_unfinalized() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope),
            make_request(scope, 2),
            current_observation=observation(scope, CancellationPhaseV1.QUIESCENT, 1),
            scope_finalized=False,
        )


def test_linearization_identical_retry_is_idempotent() -> None:
    scope = make_scope()
    prior = make_request(scope)
    retry = make_request(scope)
    current = observation(scope, CancellationPhaseV1.STOPPING, 1)
    recorded = validate_cancellation_request_linearization(
        prior, retry, current_observation=current, scope_finalized=False
    )
    assert recorded is current


def test_linearization_rejects_conflicting_request_at_same_version() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope),
            make_request(scope, reason=CancellationReasonV1.BUDGET),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            scope_finalized=False,
        )


def test_linearization_rejects_version_rollback() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope, 2),
            make_request(scope, 1),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 2),
            scope_finalized=False,
        )


def test_linearization_rejects_version_skip() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope),
            make_request(scope, 3),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            scope_finalized=False,
        )


def test_linearization_next_version_bumps_version_and_preserves_phase() -> None:
    scope = make_scope()
    recorded = validate_cancellation_request_linearization(
        make_request(scope),
        make_request(
            scope,
            2,
            requested_at="2026-08-16T00:00:01.000Z",
            reason=CancellationReasonV1.BUDGET,
        ),
        current_observation=observation(scope, CancellationPhaseV1.STOPPING, 1),
        scope_finalized=False,
    )
    assert recorded == observation(scope, CancellationPhaseV1.STOPPING, 2)


def test_linearization_requires_observation_to_carry_prior_request() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope),
            make_request(scope, 2),
            current_observation=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
            scope_finalized=False,
        )


def test_linearization_rejects_observation_prior_version_mismatch() -> None:
    scope = make_scope()
    with pytest.raises(SpecError):
        validate_cancellation_request_linearization(
            make_request(scope, 2),
            make_request(scope, 3),
            current_observation=observation(scope, CancellationPhaseV1.REQUESTED, 1),
            scope_finalized=False,
        )


# ---------------------------------------------------------------------------
# CAS / stale examples via a tiny fake implementing CancellationPortV1
# ---------------------------------------------------------------------------


class FakeCancellationPort:
    """Minimal in-memory CAS-fenced fake; not a candidate durable implementation."""

    def __init__(self) -> None:
        self._scopes: dict[tuple[str, str], CancellationScopeV1] = {}
        self._requests: dict[tuple[str, str], CancellationRequestV1] = {}
        self._observations: dict[tuple[str, str], CancellationObservationV1] = {}
        self._results: dict[tuple[str, str], CancellationResultV1] = {}

    def seed(self, scope: CancellationScopeV1) -> None:
        key = (scope.scope_id, scope.run_id)
        self._scopes[key] = scope
        self._observations[key] = CancellationObservationV1(
            scope=scope,
            observed_request_version=None,
            phase=CancellationPhaseV1.NOT_REQUESTED,
        )

    def load_scope(self, *, scope_id: str, run_id: str) -> CancellationScopeV1 | None:
        return self._scopes.get((scope_id, run_id))

    def load_request(self, scope: CancellationScopeV1) -> CancellationRequestV1 | None:
        return self._requests.get((scope.scope_id, scope.run_id))

    def observe(self, scope: CancellationScopeV1) -> CancellationObservationV1:
        return self._observations[(scope.scope_id, scope.run_id)]

    def request_cancellation(
        self,
        request: CancellationRequestV1,
        *,
        expected_prior_request: CancellationRequestV1 | None,
        expected_current_observation: CancellationObservationV1,
    ) -> tuple[CancellationRequestV1, CancellationObservationV1]:
        key = (request.scope.scope_id, request.scope.run_id)
        if self._requests.get(key) != expected_prior_request:
            raise SpecError("request_cancellation: stale expected_prior_request")
        current = self._observations[key]
        if current != expected_current_observation:
            raise SpecError("request_cancellation: stale expected_current_observation")
        # The linearization validator returns the observation to record in the
        # same atomic write; a version bump at an unchanged phase is legal here
        # and must NOT be routed through advance_cancellation_observation.
        recorded = validate_cancellation_request_linearization(
            expected_prior_request,
            request,
            current_observation=current,
            scope_finalized=key in self._results,
        )
        self._requests[key] = request
        self._observations[key] = recorded
        return request, recorded

    def advance(
        self,
        observation: CancellationObservationV1,
        *,
        expected_current: CancellationObservationV1,
    ) -> CancellationObservationV1:
        key = (observation.scope.scope_id, observation.scope.run_id)
        if key in self._results:
            raise SpecError("advance: scope already finalized cancellation")
        current = self._observations[key]
        if current != expected_current:
            raise SpecError("advance: stale expected_current")
        if observation.phase is CancellationPhaseV1.REQUESTED and observation != current:
            raise SpecError("advance: REQUESTED is only entered via request_cancellation")
        validated = advance_cancellation_observation(current, observation)
        self._observations[key] = validated
        return validated

    def finalize(
        self,
        result: CancellationResultV1,
        *,
        expected_observation: CancellationObservationV1,
    ) -> CancellationResultV1:
        key = (result.scope.scope_id, result.scope.run_id)
        current = self._observations[key]
        if current != expected_observation:
            raise SpecError("finalize: stale expected_observation")
        existing = self._results.get(key)
        if existing is not None:
            if existing == result:
                return result
            raise SpecError(
                "finalize: conflicting result for already-finalized request_version"
            )
        validate_cancellation_finalization(current, result)
        self._results[key] = result
        return result

    def load_snapshot(self, scope: CancellationScopeV1) -> CancellationSnapshotV1:
        key = (scope.scope_id, scope.run_id)
        return CancellationSnapshotV1(
            scope=self._scopes[key],
            request=self._requests.get(key),
            observation=self._observations[key],
            terminal_result=self._results.get(key),
        )


def seeded_port(scope: CancellationScopeV1) -> FakeCancellationPort:
    port = FakeCancellationPort()
    port.seed(scope)
    return port


def request_first(
    port: FakeCancellationPort, scope: CancellationScopeV1
) -> tuple[CancellationRequestV1, CancellationObservationV1]:
    return port.request_cancellation(
        make_request(scope),
        expected_prior_request=None,
        expected_current_observation=observation(
            scope, CancellationPhaseV1.NOT_REQUESTED, None
        ),
    )


def drive_to_quiescent(
    port: FakeCancellationPort, scope: CancellationScopeV1
) -> tuple[CancellationRequestV1, CancellationObservationV1]:
    request, requested_obs = request_first(port, scope)
    stopping = observation(scope, CancellationPhaseV1.STOPPING, 1)
    port.advance(stopping, expected_current=requested_obs)
    quiescent = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    port.advance(quiescent, expected_current=stopping)
    return request, quiescent


def test_fake_conforms_to_protocol() -> None:
    scope = make_scope()
    port: CancellationPortV1 = seeded_port(scope)
    assert port.load_scope(scope_id=scope.scope_id, run_id=scope.run_id) == scope
    assert port.load_request(scope) is None
    assert port.observe(scope).phase is CancellationPhaseV1.NOT_REQUESTED


def test_load_snapshot_not_requested_is_consistent() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    snapshot = port.load_snapshot(scope)
    assert snapshot.scope == scope
    assert snapshot.request is None
    assert snapshot.observation == observation(scope, CancellationPhaseV1.NOT_REQUESTED, None)
    assert snapshot.terminal_result is None


def test_request_cancellation_atomically_records_request_and_requested_state() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, recorded = request_first(port, scope)
    assert recorded == observation(scope, CancellationPhaseV1.REQUESTED, 1)
    assert port.load_request(scope) == request
    assert port.observe(scope) == recorded
    snapshot = port.load_snapshot(scope)
    assert snapshot.request == request
    assert snapshot.observation == recorded
    assert snapshot.terminal_result is None


def test_request_cancellation_rejects_stale_prior_request_without_partial_write() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, recorded = request_first(port, scope)
    second = make_request(scope, 2)
    with pytest.raises(SpecError):
        port.request_cancellation(
            second,
            expected_prior_request=None,
            expected_current_observation=recorded,
        )
    assert port.load_request(scope) == request
    assert port.observe(scope) == recorded


def test_request_cancellation_rejects_stale_observation_without_partial_write() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    stale = observation(scope, CancellationPhaseV1.REQUESTED, 1)
    with pytest.raises(SpecError):
        port.request_cancellation(
            make_request(scope),
            expected_prior_request=None,
            expected_current_observation=stale,
        )
    assert port.load_request(scope) is None
    assert port.observe(scope) == observation(scope, CancellationPhaseV1.NOT_REQUESTED, None)


def test_request_cancellation_rejects_owner_key_substitution_in_expected() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    imposter = make_scope(owner_key="owner-imposter")
    with pytest.raises(SpecError):
        port.request_cancellation(
            make_request(imposter),
            expected_prior_request=None,
            expected_current_observation=observation(
                imposter, CancellationPhaseV1.NOT_REQUESTED, None
            ),
        )
    assert port.load_request(scope) is None


def test_request_cancellation_rejects_owner_key_substitution_in_request() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    imposter = make_scope(owner_key="owner-imposter")
    with pytest.raises(SpecError):
        port.request_cancellation(
            make_request(imposter),
            expected_prior_request=None,
            expected_current_observation=observation(
                scope, CancellationPhaseV1.NOT_REQUESTED, None
            ),
        )
    assert port.load_request(scope) is None


def test_request_cancellation_exact_idempotent_retry() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, recorded = request_first(port, scope)
    retry = make_request(scope)
    assert retry is not request
    returned_request, returned_obs = port.request_cancellation(
        retry,
        expected_prior_request=request,
        expected_current_observation=recorded,
    )
    assert returned_request == request
    assert returned_obs == recorded
    assert port.observe(scope) == recorded


def test_request_cancellation_rejects_conflict_rollback_and_skip() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, recorded = request_first(port, scope)
    conflicting = make_request(scope, reason=CancellationReasonV1.BUDGET)
    with pytest.raises(SpecError):
        port.request_cancellation(
            conflicting,
            expected_prior_request=request,
            expected_current_observation=recorded,
        )
    skipping = make_request(scope, 3)
    with pytest.raises(SpecError):
        port.request_cancellation(
            skipping,
            expected_prior_request=request,
            expected_current_observation=recorded,
        )
    second = make_request(
        scope, 2, requested_at="2026-08-16T00:00:01.000Z", reason=CancellationReasonV1.BUDGET
    )
    _, recorded_v2 = port.request_cancellation(
        second,
        expected_prior_request=request,
        expected_current_observation=recorded,
    )
    assert recorded_v2 == observation(scope, CancellationPhaseV1.REQUESTED, 2)
    rollback = make_request(scope, 1)
    with pytest.raises(SpecError):
        port.request_cancellation(
            rollback,
            expected_prior_request=second,
            expected_current_observation=recorded_v2,
        )


def test_advance_never_enters_requested() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    with pytest.raises(SpecError):
        port.advance(
            observation(scope, CancellationPhaseV1.REQUESTED, 1),
            expected_current=observation(scope, CancellationPhaseV1.NOT_REQUESTED, None),
        )
    assert port.observe(scope) == observation(scope, CancellationPhaseV1.NOT_REQUESTED, None)


def test_cas_advance_rejects_stale_expected_observation() -> None:
    scope = make_scope(generation=1)
    port = seeded_port(scope)
    _, recorded = request_first(port, scope)
    stopping = observation(scope, CancellationPhaseV1.STOPPING, 1)
    stale_expected = observation(make_scope(generation=2), CancellationPhaseV1.REQUESTED, 1)
    with pytest.raises(SpecError):
        port.advance(stopping, expected_current=stale_expected)
    assert port.advance(stopping, expected_current=recorded) == stopping


def test_cas_advance_rejects_unlinearized_higher_version_without_state_change() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, recorded = request_first(port, scope)
    forged = observation(scope, CancellationPhaseV1.STOPPING, 999)
    with pytest.raises(SpecError):
        port.advance(forged, expected_current=recorded)
    snapshot = port.load_snapshot(scope)
    assert snapshot.request == request
    assert snapshot.observation == recorded


def test_cas_advance_idempotent_retry() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    _, recorded = request_first(port, scope)
    stopping = observation(scope, CancellationPhaseV1.STOPPING, 1)
    port.advance(stopping, expected_current=recorded)
    retry = observation(scope, CancellationPhaseV1.STOPPING, 1)
    assert port.advance(retry, expected_current=stopping) == retry


def test_cas_advance_rejects_illegal_transition() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    _, recorded = request_first(port, scope)
    quiescent = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        port.advance(quiescent, expected_current=recorded)


def test_cleanup_incomplete_recovers_to_quiescent_before_finalize() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    _, recorded = request_first(port, scope)
    stopping = observation(scope, CancellationPhaseV1.STOPPING, 1)
    port.advance(stopping, expected_current=recorded)
    cleanup = observation(scope, CancellationPhaseV1.CLEANUP_INCOMPLETE, 1)
    port.advance(cleanup, expected_current=stopping)
    result = make_result(scope)
    with pytest.raises(SpecError):
        port.finalize(result, expected_observation=cleanup)
    quiescent = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    port.advance(quiescent, expected_current=cleanup)
    finalized = port.finalize(result, expected_observation=quiescent)
    assert finalized.outcome is CancellationOutcomeV1.CANCELLED
    assert port.load_snapshot(scope).terminal_result == result


# ---------------------------------------------------------------------------
# finalize / finalization validation
# ---------------------------------------------------------------------------


def test_finalize_happy_path_quiescent_cancelled() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, quiescent = drive_to_quiescent(port, scope)
    result = make_result(scope)
    finalized = port.finalize(result, expected_observation=quiescent)
    assert finalized.outcome is CancellationOutcomeV1.CANCELLED
    assert finalized.request_version == 1
    snapshot = port.load_snapshot(scope)
    assert snapshot.request == request
    assert snapshot.observation == quiescent
    assert snapshot.terminal_result == result


def test_finalize_rejects_stale_expected_observation() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    drive_to_quiescent(port, scope)
    stale = observation(scope, CancellationPhaseV1.STOPPING, 1)
    with pytest.raises(SpecError):
        port.finalize(make_result(scope), expected_observation=stale)
    assert port.load_snapshot(scope).terminal_result is None


def test_finalize_identical_retry_is_idempotent() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    _, quiescent = drive_to_quiescent(port, scope)
    result = make_result(scope, quiescence_evidence_ref="evidence-retry-01")
    retry = make_result(scope, quiescence_evidence_ref="evidence-retry-01")
    finalized_first = port.finalize(result, expected_observation=quiescent)
    finalized_retry = port.finalize(retry, expected_observation=quiescent)
    assert finalized_first == finalized_retry == result


def test_finalize_conflicting_result_raises_specerror() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    _, quiescent = drive_to_quiescent(port, scope)
    first_result = make_result(scope, quiescence_evidence_ref="evidence-first")
    port.finalize(first_result, expected_observation=quiescent)
    conflicting_result = make_result(scope, quiescence_evidence_ref="evidence-different")
    with pytest.raises(SpecError):
        port.finalize(conflicting_result, expected_observation=quiescent)
    assert port.load_snapshot(scope).terminal_result == first_result


def test_terminal_snapshot_preserved_and_scope_never_reopens() -> None:
    scope = make_scope()
    port = seeded_port(scope)
    request, quiescent = drive_to_quiescent(port, scope)
    result = make_result(scope)
    port.finalize(result, expected_observation=quiescent)
    snapshot = port.load_snapshot(scope)
    assert snapshot.request == request
    assert snapshot.observation == quiescent
    assert snapshot.terminal_result == result

    next_request = make_request(
        scope, 2, requested_at="2026-08-16T00:00:01.000Z", reason=CancellationReasonV1.SHUTDOWN
    )
    with pytest.raises(SpecError):
        port.request_cancellation(
            next_request,
            expected_prior_request=request,
            expected_current_observation=quiescent,
        )
    with pytest.raises(SpecError):
        port.advance(
            observation(scope, CancellationPhaseV1.QUIESCENT, 1), expected_current=quiescent
        )
    assert port.load_snapshot(scope) == snapshot


def test_validate_cancellation_finalization_rejects_cancelled_from_non_quiescent() -> None:
    scope = make_scope()
    for phase in [
        CancellationPhaseV1.REQUESTED,
        CancellationPhaseV1.STOPPING,
        CancellationPhaseV1.CLEANUP_INCOMPLETE,
    ]:
        obs = observation(scope, phase, 1)
        result = make_result(scope)
        with pytest.raises(SpecError):
            validate_cancellation_finalization(obs, result)


def test_validate_cancellation_finalization_scope_identity_binding() -> None:
    scope_a = make_scope(scope_id="scope-a", run_id="run-a")
    scope_b = make_scope(scope_id="scope-b", run_id="run-b")
    obs_a = observation(scope_a, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        validate_cancellation_finalization(obs_a, make_result(scope_b))


def test_validate_cancellation_finalization_generation_binding() -> None:
    scope_gen1 = make_scope(generation=1)
    scope_gen2 = make_scope(generation=2)
    obs = observation(scope_gen1, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        validate_cancellation_finalization(obs, make_result(scope_gen2))


def test_validate_cancellation_finalization_owner_key_binding() -> None:
    scope = make_scope()
    imposter = make_scope(owner_key="owner-imposter")
    obs = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        validate_cancellation_finalization(obs, make_result(imposter))


def test_validate_cancellation_finalization_request_version_binding() -> None:
    scope = make_scope()
    obs = observation(scope, CancellationPhaseV1.QUIESCENT, 1)
    with pytest.raises(SpecError):
        validate_cancellation_finalization(obs, make_result(scope, 2))


# ---------------------------------------------------------------------------
# Exception / repr non-reflection
# ---------------------------------------------------------------------------


def test_scope_repr_never_reflects_owner_key() -> None:
    scope = make_scope(owner_key="super-secret-owner-token")
    rendering = repr(scope)
    assert "super-secret-owner-token" not in rendering
    assert "[redacted]" in rendering


def test_request_repr_never_reflects_owner_key_via_nested_scope() -> None:
    scope = make_scope(owner_key="super-secret-owner-token")
    request = make_request(scope)
    assert "super-secret-owner-token" not in repr(request)


def test_snapshot_repr_never_reflects_owner_key_or_evidence_ref() -> None:
    scope = make_scope(owner_key="super-secret-owner-token")
    snapshot = CancellationSnapshotV1(
        scope=scope,
        request=make_request(scope),
        observation=observation(scope, CancellationPhaseV1.QUIESCENT, 1),
        terminal_result=make_result(scope, quiescence_evidence_ref="secret-evidence-ref"),
    )
    rendering = repr(snapshot)
    assert "super-secret-owner-token" not in rendering
    assert "secret-evidence-ref" not in rendering


def test_invalid_owner_key_error_never_reflects_value() -> None:
    secret_attempt = "provider/controlled-leak-value"
    with pytest.raises(SpecError) as excinfo:
        make_scope(owner_key=secret_attempt)
    assert secret_attempt not in str(excinfo.value)


def test_invalid_evidence_ref_error_never_reflects_value() -> None:
    secret_attempt = "provider/controlled-leak-value"
    scope = make_scope()
    with pytest.raises(SpecError) as excinfo:
        make_result(scope, quiescence_evidence_ref=secret_attempt)
    assert secret_attempt not in str(excinfo.value)


def test_invalid_reason_error_never_reflects_arbitrary_value() -> None:
    class _Leaky:
        def __repr__(self) -> str:
            return "PROVIDER_LEAK_MARKER"

    scope = make_scope()
    with pytest.raises(SpecError) as excinfo:
        CancellationRequestV1(
            scope=scope,
            request_version=1,
            requested_at="2026-08-16T00:00:00.000Z",
            reason=_Leaky(),  # type: ignore[arg-type]
        )
    assert "PROVIDER_LEAK_MARKER" not in str(excinfo.value)


def test_invalid_phase_error_never_reflects_arbitrary_value() -> None:
    class _Leaky:
        def __repr__(self) -> str:
            return "PROVIDER_LEAK_MARKER"

    scope = make_scope()
    with pytest.raises(SpecError) as excinfo:
        CancellationObservationV1(
            scope=scope, observed_request_version=None, phase=_Leaky()  # type: ignore[arg-type]
        )
    assert "PROVIDER_LEAK_MARKER" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# API version
# ---------------------------------------------------------------------------


def test_api_version_is_plain_int_one() -> None:
    assert CANCELLATION_PORT_API_VERSION == 1
    assert type(CANCELLATION_PORT_API_VERSION) is int


def test_validate_api_version_accepts_current() -> None:
    assert validate_api_version(1, ctx="v") == 1


@pytest.mark.parametrize("value", [True, False, 2, 0, "1", 1.0, None])
def test_validate_api_version_rejects_invalid(value: object) -> None:
    with pytest.raises(SpecError):
        validate_api_version(value, ctx="v")
