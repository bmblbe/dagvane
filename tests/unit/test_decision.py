"""Judge-decision parsing: strict boundary matrix (Round 4 §fail-loud).

The winner universe passed in comes from the proposals the judge actually saw;
these tests pin every rejection class for malformed or forged decisions.
"""

from __future__ import annotations

import pytest

from dagvane.domain.models import SpecError
from dagvane.protocol.documents import parse_decision

ALLOWED = frozenset({"candidate-1", "candidate-2"})


def test_valid_decision_parses() -> None:
    decision = parse_decision(
        '{"decision_version": 1, "winner": "candidate-2", "rationale": "solid"}',
        ALLOWED,
    )
    assert decision.winner == "candidate-2"
    assert decision.rationale == "solid"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("not json at all", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('"just a string"', "must be a JSON object"),
        ('{"decision_version": 2, "winner": "candidate-1", "rationale": "r"}',
         "unsupported decision_version"),
        ('{"decision_version": "1", "winner": "candidate-1", "rationale": "r"}',
         "decision_version"),
        ('{"winner": "candidate-1", "rationale": "r"}', "missing required key"),
        ('{"decision_version": 1, "rationale": "r"}', "missing required key"),
        ('{"decision_version": 1, "winner": "candidate-1"}', "missing required key"),
        ('{"decision_version": 1, "winner": "candidate-1", "rationale": "r", '
         '"extra": true}', "unknown keys"),
        ('{"decision_version": 1, "winner": 7, "rationale": "r"}', "non-empty string"),
        ('{"decision_version": 1, "winner": "candidate-1", "rationale": ""}',
         "non-empty string"),
        ('{"decision_version": 1, "winner": "candidate-1", "rationale": 42}',
         "non-empty string"),
        ('{"decision_version": 1, "winner": "candidate-ghost", "rationale": "r"}',
         "not one of"),
        ('{"decision_version": 1, "winner": "proposer-a", "rationale": "r"}',
         "not one of"),
        ('{"decision_version": 1, "winner": "candidate-1", "rationale": "a", '
         '"rationale": "b"}', "duplicate key"),
        ('{"decision_version": 1, "decision_version": 1, "winner": "candidate-1", '
         '"rationale": "r"}', "duplicate key"),
    ],
)
def test_invalid_decisions_rejected(text: str, match: str) -> None:
    with pytest.raises(SpecError, match=match):
        parse_decision(text, ALLOWED)


def test_empty_winner_universe_rejects_everything() -> None:
    with pytest.raises(SpecError, match="not one of"):
        parse_decision(
            '{"decision_version": 1, "winner": "candidate-1", "rationale": "r"}',
            frozenset(),
        )
