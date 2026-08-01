import pytest

from guildmind.domain import BudgetLimits, BudgetUsage
from guildmind.runtime import BudgetAuthority, BudgetExceededError, ReservationExceededError


def test_budget_reserves_before_dispatch_and_reconciles_actual_usage() -> None:
    authority = BudgetAuthority(BudgetLimits(max_model_calls=2, max_total_tokens=100))
    maximum = BudgetUsage(uncached_input_tokens=50, output_tokens=25, model_calls=1)

    authority.reserve("request-1", maximum)

    assert authority.reserved == maximum
    authority.reconcile(
        "request-1",
        BudgetUsage(uncached_input_tokens=30, output_tokens=10, model_calls=1),
    )
    assert authority.reservation_ids == ()
    assert authority.used.total_tokens == 40
    assert authority.used.model_calls == 1


def test_budget_refuses_work_that_would_exceed_aggregate_cap() -> None:
    authority = BudgetAuthority(BudgetLimits(max_model_calls=1))
    authority.reserve("request-1", BudgetUsage(model_calls=1))

    with pytest.raises(BudgetExceededError) as captured:
        authority.reserve("request-2", BudgetUsage(model_calls=1))

    assert captured.value.dimensions == ("model_calls",)


def test_budget_rejects_usage_larger_than_its_reservation() -> None:
    authority = BudgetAuthority(BudgetLimits(max_total_tokens=100))
    authority.reserve("request-1", BudgetUsage(output_tokens=10))

    with pytest.raises(ReservationExceededError):
        authority.reconcile("request-1", BudgetUsage(output_tokens=11))

    assert authority.reservation_ids == ("request-1",)
    authority.release("request-1")
    assert authority.reserved == BudgetUsage()
