"""BudgetLedger: multidimensional reserve → commit/release with hard admission."""

from __future__ import annotations

import pytest

from dagvane.application.council import BudgetLedger, dispatch_cost_microusd
from dagvane.domain.models import Budget, BudgetRejectedError, Pricing, Usage


def _budget(**overrides: int) -> Budget:
    values = {"max_calls": 10, "max_total_tokens": 10_000, "max_cost_microusd": 1_000_000}
    values.update(overrides)
    return Budget(**values)


def test_reserve_commit_totals_use_actuals() -> None:
    ledger = BudgetLedger(_budget())
    reservation = ledger.reserve(tokens=1_000, cost_microusd=500)
    ledger.commit(reservation, Usage(input_tokens=100, output_tokens=20), 60)
    totals = ledger.totals()
    assert totals.calls == 1
    assert totals.input_tokens == 100
    assert totals.output_tokens == 20
    assert totals.cost_microusd == 60


def test_calls_dimension_rejects() -> None:
    ledger = BudgetLedger(_budget(max_calls=1))
    ledger.commit(ledger.reserve(tokens=1, cost_microusd=1), Usage(1, 1), 1)
    with pytest.raises(BudgetRejectedError) as excinfo:
        ledger.reserve(tokens=1, cost_microusd=1)
    assert excinfo.value.dimension == "calls"
    assert excinfo.value.cap == 1


def test_tokens_dimension_rejects() -> None:
    ledger = BudgetLedger(_budget(max_total_tokens=100))
    with pytest.raises(BudgetRejectedError) as excinfo:
        ledger.reserve(tokens=101, cost_microusd=1)
    assert excinfo.value.dimension == "total_tokens"


def test_cost_dimension_rejects() -> None:
    ledger = BudgetLedger(_budget(max_cost_microusd=99))
    with pytest.raises(BudgetRejectedError) as excinfo:
        ledger.reserve(tokens=1, cost_microusd=100)
    assert excinfo.value.dimension == "cost_microusd"


def test_inflight_reservations_count_toward_admission() -> None:
    ledger = BudgetLedger(_budget(max_calls=2))
    ledger.reserve(tokens=1, cost_microusd=1)
    ledger.reserve(tokens=1, cost_microusd=1)
    with pytest.raises(BudgetRejectedError):
        ledger.reserve(tokens=1, cost_microusd=1)


def test_release_returns_reservation_to_the_pool() -> None:
    ledger = BudgetLedger(_budget(max_calls=1))
    reservation = ledger.reserve(tokens=1, cost_microusd=1)
    ledger.release(reservation)
    assert ledger.totals().calls == 0
    ledger.reserve(tokens=1, cost_microusd=1)  # admissible again


def test_dispatch_cost_is_integer_ceiling_math() -> None:
    pricing = Pricing(input_microusd_per_mtok=3_000_000, output_microusd_per_mtok=15_000_000)
    assert dispatch_cost_microusd(1, 0, pricing) == 3
    assert dispatch_cost_microusd(0, 1, pricing) == 15
    assert dispatch_cost_microusd(1_000_000, 1_000_000, pricing) == 18_000_000
    tiny = Pricing(input_microusd_per_mtok=1, output_microusd_per_mtok=1)
    assert dispatch_cost_microusd(1, 1, tiny) == 2  # ceilings, never silent zero
