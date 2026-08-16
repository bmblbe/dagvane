"""Focused offline regressions for the R1-G0 escalation port."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from dagvane.ports.escalation import (
    API_VERSION,
    EscalationAction,
    EscalationContractError,
    EscalationDecision,
    EscalationPolicy,
    EscalationRequest,
    EscalationState,
    EscalationTier,
    EvaluationEvidence,
    FindingHistory,
    FindingIdentity,
    FindingObservation,
    FindingSeverity,
    ProgressEvidence,
    ProgressKind,
    decide_escalation,
)


def _sha(char: str) -> str:
    return char * 40


def _digest(char: str) -> str:
    return char * 64


def _observation(
    finding: FindingIdentity,
    candidate: str,
    evidence: str,
    *,
    severity: FindingSeverity = FindingSeverity.BLOCKER,
) -> FindingObservation:
    return FindingObservation(
        finding=finding,
        severity=severity,
        candidate_sha=candidate,
        evidence_sha256=evidence,
    )


def _advance(
    state: EscalationState,
    evidence: EvaluationEvidence,
    *,
    policy: EscalationPolicy | None = None,
) -> EscalationDecision:
    return decide_escalation(
        EscalationRequest(
            state=state,
            evidence=evidence,
            policy=policy or EscalationPolicy(),
        )
    )


@pytest.mark.parametrize("version", [2, 1.0, True, "1"])
def test_api_is_versioned_and_rejects_unknown_version(version: object) -> None:
    assert API_VERSION == 1
    with pytest.raises(EscalationContractError, match="unsupported.*version"):
        EscalationRequest(
            state=EscalationState(),
            evidence=EvaluationEvidence(candidate_sha=_sha("a")),
            policy=EscalationPolicy(),
            api_version=version,  # type: ignore[arg-type]
        )


def test_repeated_blocker_with_junk_shas_walks_the_bounded_ladder() -> None:
    finding = FindingIdentity(_digest("f"))
    state = EscalationState()
    expected = (
        (EscalationAction.ROUTE, EscalationTier.STANDARD),
        (EscalationAction.ROUTE, EscalationTier.STRONG),
        (EscalationAction.ROUTE, EscalationTier.CRITICAL),
        (EscalationAction.BLOCKED, None),
    )

    # The canonical finding and even its review payload digest stay identical;
    # only irrelevant candidate SHAs change.  That must still be no progress.
    for index, candidate_char in enumerate("abcd"):
        candidate = _sha(candidate_char)
        decision = _advance(
            state,
            EvaluationEvidence(
                candidate_sha=candidate,
                findings=(
                    _observation(finding, candidate, _digest("1")),
                ),
            ),
        )
        assert (decision.action, decision.tier) == expected[index]
        assert decision.meaningful_progress is False
        assert decision.pressure == index + 1
        state = decision.state

    history = state.unresolved[0]
    assert history.finding == finding
    assert history.failure_count == 4
    assert tuple(item.candidate_sha for item in history.observations) == tuple(
        _sha(char) for char in "abcd"
    )


def test_a_new_candidate_sha_alone_is_not_progress() -> None:
    state = EscalationState()
    tiers: list[EscalationTier | None] = []
    for candidate_char in "abc":
        decision = _advance(
            state,
            EvaluationEvidence(candidate_sha=_sha(candidate_char)),
        )
        tiers.append(decision.tier)
        state = decision.state

    assert tiers == [
        EscalationTier.STANDARD,
        EscalationTier.STRONG,
        EscalationTier.CRITICAL,
    ]
    assert state.consecutive_no_progress == 3
    assert state.credited_progress == ()


def test_closure_resets_only_the_corresponding_finding() -> None:
    finding_a = FindingIdentity(_digest("a"))
    finding_b = FindingIdentity(_digest("b"))
    state = EscalationState()

    for candidate_char, evidence_chars in (("1", "cd"), ("2", "ef")):
        candidate = _sha(candidate_char)
        decision = _advance(
            state,
            EvaluationEvidence(
                candidate_sha=candidate,
                findings=(
                    _observation(finding_a, candidate, _digest(evidence_chars[0])),
                    _observation(finding_b, candidate, _digest(evidence_chars[1])),
                ),
            ),
        )
        state = decision.state

    closure_candidate = _sha("3")
    decision = _advance(
        state,
        EvaluationEvidence(
            candidate_sha=closure_candidate,
            findings=(
                _observation(finding_b, closure_candidate, _digest("8")),
            ),
            progress=(
                ProgressEvidence(
                    kind=ProgressKind.FINDING_CLOSED,
                    subject_sha256=finding_a.fingerprint_sha256,
                    candidate_sha=closure_candidate,
                    evidence_sha256=_digest("9"),
                ),
            ),
        ),
    )

    assert decision.meaningful_progress is True
    assert decision.state.consecutive_no_progress == 0
    assert [history.finding for history in decision.state.unresolved] == [finding_b]
    assert decision.state.unresolved[0].failure_count == 3
    assert decision.tier is EscalationTier.CRITICAL

    # If A is observed again later it starts at one; B's history still stays at
    # three and therefore keeps the route CRITICAL.
    next_candidate = _sha("4")
    reopened = _advance(
        decision.state,
        EvaluationEvidence(
            candidate_sha=next_candidate,
            findings=(
                _observation(finding_a, next_candidate, _digest("0")),
            ),
        ),
    )
    counts = {
        history.finding.fingerprint_sha256: history.failure_count
        for history in reopened.state.unresolved
    }
    assert counts == {
        finding_a.fingerprint_sha256: 1,
        finding_b.fingerprint_sha256: 3,
    }
    assert reopened.tier is EscalationTier.CRITICAL


def test_only_a_new_acceptance_subject_is_meaningful_progress() -> None:
    criterion = _digest("c")
    first = _advance(
        EscalationState(),
        EvaluationEvidence(
            candidate_sha=_sha("a"),
            progress=(
                ProgressEvidence(
                    kind=ProgressKind.ACCEPTANCE_GAIN,
                    subject_sha256=criterion,
                    candidate_sha=_sha("a"),
                    evidence_sha256=_digest("1"),
                ),
            ),
        ),
    )
    assert first.meaningful_progress is True
    assert first.state.consecutive_no_progress == 0

    # Re-passing the same already credited condition, even under new evidence
    # and a new SHA, is not a newly gained acceptance result.
    repeated = _advance(
        first.state,
        EvaluationEvidence(
            candidate_sha=_sha("b"),
            progress=(
                ProgressEvidence(
                    kind=ProgressKind.ACCEPTANCE_GAIN,
                    subject_sha256=criterion,
                    candidate_sha=_sha("b"),
                    evidence_sha256=_digest("2"),
                ),
            ),
        ),
    )
    assert repeated.meaningful_progress is False
    assert repeated.state.consecutive_no_progress == 1
    assert repeated.state.credited_progress == first.state.credited_progress


def test_closure_requires_new_evidence_for_a_currently_unresolved_finding() -> None:
    finding = FindingIdentity(_digest("f"))
    closure = ProgressEvidence(
        kind=ProgressKind.FINDING_CLOSED,
        subject_sha256=finding.fingerprint_sha256,
        candidate_sha=_sha("a"),
        evidence_sha256=_digest("e"),
    )
    with pytest.raises(
        EscalationContractError,
        match="closure must reference an unresolved finding",
    ):
        _advance(
            EscalationState(),
            EvaluationEvidence(candidate_sha=_sha("a"), progress=(closure,)),
        )


def test_hard_evaluation_bound_is_terminal_and_does_not_consume_more_evidence() -> None:
    policy = EscalationPolicy(
        strong_after=3,
        critical_after=4,
        blocked_after=5,
        max_evaluations=5,
    )
    state = EscalationState()
    for candidate_char in "abcd":
        state = _advance(
            state,
            EvaluationEvidence(candidate_sha=_sha(candidate_char)),
            policy=policy,
        ).state

    terminal = _advance(
        state,
        EvaluationEvidence(candidate_sha=_sha("e")),
        policy=policy,
    )
    assert terminal.action is EscalationAction.BLOCKED
    assert terminal.state.evaluation_count == 5

    exact_retry = _advance(
        terminal.state,
        EvaluationEvidence(candidate_sha=_sha("e")),
        policy=policy,
    )
    assert exact_retry.action is EscalationAction.BLOCKED
    assert exact_retry.state is terminal.state

    with pytest.raises(EscalationContractError, match="cannot accept new evidence"):
        _advance(
            terminal.state,
            EvaluationEvidence(candidate_sha=_sha("f")),
            policy=policy,
        )


def test_terminal_decision_with_progress_replays_identically_on_restart() -> None:
    # max_evaluations=4 forces the hard bound and the strong-threshold
    # meaningful-progress credit to land on the exact same evaluation: the
    # terminal decision must still report the progress it actually credited,
    # and an exact restart replay must reproduce that decision exactly.
    policy = EscalationPolicy(
        strong_after=2,
        critical_after=3,
        blocked_after=4,
        max_evaluations=4,
    )
    finding = FindingIdentity(_digest("f"))
    criterion = _digest("c")
    state = EscalationState()

    for candidate_char in "abc":
        candidate = _sha(candidate_char)
        state = _advance(
            state,
            EvaluationEvidence(
                candidate_sha=candidate,
                findings=(_observation(finding, candidate, _digest("1")),),
            ),
            policy=policy,
        ).state

    final_candidate = _sha("d")
    final_evidence = EvaluationEvidence(
        candidate_sha=final_candidate,
        findings=(_observation(finding, final_candidate, _digest("1")),),
        progress=(
            ProgressEvidence(
                kind=ProgressKind.ACCEPTANCE_GAIN,
                subject_sha256=criterion,
                candidate_sha=final_candidate,
                evidence_sha256=_digest("9"),
            ),
        ),
    )
    terminal = _advance(state, final_evidence, policy=policy)

    assert terminal.action is EscalationAction.BLOCKED
    assert terminal.state.evaluation_count == 4
    assert terminal.meaningful_progress is True
    assert terminal.state.terminal_meaningful_progress is True

    restart_replay = _advance(terminal.state, final_evidence, policy=policy)
    assert restart_replay == terminal
    assert restart_replay.state is terminal.state


def test_terminal_state_cannot_reopen_under_a_looser_policy_after_restart() -> None:
    original = EscalationPolicy()
    state = EscalationState()
    terminal: EscalationDecision | None = None
    for candidate_char in "abcd":
        terminal = _advance(
            state,
            EvaluationEvidence(candidate_sha=_sha(candidate_char)),
            policy=original,
        )
        state = terminal.state

    assert terminal is not None
    assert terminal.action is EscalationAction.BLOCKED
    before = terminal.state
    looser = EscalationPolicy(
        strong_after=5,
        critical_after=6,
        blocked_after=7,
        max_evaluations=8,
    )
    with pytest.raises(EscalationContractError, match="policy does not match"):
        _advance(
            before,
            EvaluationEvidence(candidate_sha=_sha("e")),
            policy=looser,
        )
    assert terminal.state is before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strong_after": 1},
        {"critical_after": 2},
        {"blocked_after": 3},
        {"max_evaluations": 3},
        {"strong_after": 3, "critical_after": 3},
        {"blocked_after": 9, "max_evaluations": 8},
    ],
)
def test_policy_bounds_are_strict(kwargs: dict[str, int]) -> None:
    with pytest.raises(EscalationContractError):
        EscalationPolicy(**kwargs)


def test_records_are_deeply_immutable_and_reject_mutable_collections() -> None:
    finding = FindingIdentity(_digest("f"))
    observation = _observation(finding, _sha("a"), _digest("e"))
    history = FindingHistory(finding=finding, observations=(observation,))

    with pytest.raises(FrozenInstanceError):
        history.observations = ()  # type: ignore[misc]
    assert not hasattr(history, "__dict__")
    with pytest.raises(EscalationContractError, match="must be a tuple"):
        FindingHistory(
            finding=finding,
            observations=[observation],  # type: ignore[arg-type]
        )


def test_provider_text_is_never_reflected_by_validation_errors() -> None:
    raw_provider_text = "provider-secret: sk-do-not-persist"
    with pytest.raises(EscalationContractError) as captured:
        FindingIdentity(raw_provider_text)

    assert raw_provider_text not in str(captured.value)
    assert "sk-do-not-persist" not in repr(captured.value)


def test_records_reject_strings_in_place_of_closed_enums() -> None:
    finding = FindingIdentity(_digest("f"))
    with pytest.raises(EscalationContractError, match="severity is invalid"):
        FindingObservation(
            finding=finding,
            severity="BLOCKER",  # type: ignore[arg-type]
            candidate_sha=_sha("a"),
            evidence_sha256=_digest("e"),
        )
