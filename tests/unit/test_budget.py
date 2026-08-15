"""BudgetLedger: multidimensional reserve → commit/release with hard admission,
plus the commit postcondition — honest actuals may never complete a run above caps.
"""

from __future__ import annotations

import threading

import pytest

from dagvane.application.council import BudgetBreach, BudgetLedger, dispatch_cost_microusd
from dagvane.domain.models import Budget, BudgetRejectedError, Pricing, Usage


def _budget(**overrides: int) -> Budget:
    values = {"max_calls": 10, "max_total_tokens": 10_000, "max_cost_microusd": 1_000_000}
    values.update(overrides)
    return Budget(**values)


def test_reserve_commit_totals_use_actuals() -> None:
    ledger = BudgetLedger(_budget())
    reservation = ledger.reserve(tokens=1_000, cost_microusd=500)
    assert ledger.commit(reservation, Usage(input_tokens=100, output_tokens=20), 60) is None
    totals = ledger.totals()
    assert totals.calls == 1
    assert totals.input_tokens == 100
    assert totals.output_tokens == 20
    assert totals.cost_microusd == 60


def test_commit_reports_token_breach_but_records_honestly() -> None:
    ledger = BudgetLedger(_budget(max_total_tokens=100))
    reservation = ledger.reserve(tokens=50, cost_microusd=1)
    breach = ledger.commit(reservation, Usage(input_tokens=90, output_tokens=20), 1)
    assert breach == BudgetBreach(dimension="total_tokens", committed=110, cap=100)
    totals = ledger.totals()  # actuals are recorded even though the cap is broken
    assert totals.input_tokens == 90
    assert totals.output_tokens == 20


def test_commit_reports_cost_breach_but_records_honestly() -> None:
    ledger = BudgetLedger(_budget(max_cost_microusd=100))
    reservation = ledger.reserve(tokens=1, cost_microusd=50)
    breach = ledger.commit(reservation, Usage(input_tokens=1, output_tokens=1), 250)
    assert breach == BudgetBreach(dimension="cost_microusd", committed=250, cap=100)
    assert ledger.totals().cost_microusd == 250


def test_commit_breach_persists_for_later_commits() -> None:
    # Once actuals exceed a cap, every following commit also reports the breach:
    # the run can never be driven back under its hard caps by more spending.
    ledger = BudgetLedger(_budget(max_total_tokens=100))
    first = ledger.reserve(tokens=10, cost_microusd=1)
    second = ledger.reserve(tokens=10, cost_microusd=1)
    assert ledger.commit(first, Usage(input_tokens=200, output_tokens=0), 1) is not None
    later = ledger.commit(second, Usage(input_tokens=1, output_tokens=0), 1)
    assert later is not None
    assert later.dimension == "total_tokens"


def test_concurrent_reservations_never_exceed_caps() -> None:
    ledger = BudgetLedger(_budget(max_calls=4))
    barrier = threading.Barrier(16)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait()
        try:
            ledger.reserve(tokens=1, cost_microusd=1)
        except BudgetRejectedError:
            with lock:
                outcomes.append("rejected")
        else:
            with lock:
                outcomes.append("admitted")

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("admitted") == 4
    assert outcomes.count("rejected") == 12


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
