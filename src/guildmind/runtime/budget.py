"""Aggregate budget reservation and reconciliation."""

from __future__ import annotations

from collections.abc import Iterable

from guildmind.domain import BudgetLimits, BudgetUsage

_INT_FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "model_calls",
    "model_retries",
    "tool_calls",
)
_FLOAT_FIELDS = (
    "tool_cpu_seconds",
    "container_wall_seconds",
    "elapsed_seconds",
    "estimated_cost_usd",
)
_USAGE_FIELDS = _INT_FIELDS + _FLOAT_FIELDS


class BudgetExceededError(RuntimeError):
    def __init__(self, dimensions: tuple[str, ...]) -> None:
        self.dimensions = dimensions
        super().__init__(f"budget would exceed: {', '.join(dimensions)}")


class ReservationExceededError(RuntimeError):
    pass


def sum_usage(values: Iterable[BudgetUsage]) -> BudgetUsage:
    totals: dict[str, int | float] = {field: 0 for field in _USAGE_FIELDS}
    for usage in values:
        for field in _USAGE_FIELDS:
            totals[field] += getattr(usage, field)
    return BudgetUsage.model_validate(totals)


def _fits_within(actual: BudgetUsage, ceiling: BudgetUsage) -> bool:
    return all(getattr(actual, field) <= getattr(ceiling, field) for field in _USAGE_FIELDS)


class BudgetAuthority:
    """Authorize bounded work before dispatch and reconcile reported usage afterward."""

    def __init__(self, limits: BudgetLimits) -> None:
        self.limits = limits
        self._used = BudgetUsage()
        self._reservations: dict[str, BudgetUsage] = {}

    @property
    def used(self) -> BudgetUsage:
        return self._used

    @property
    def reserved(self) -> BudgetUsage:
        return sum_usage(self._reservations.values())

    @property
    def reservation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._reservations))

    def reserve(self, reservation_id: str, maximum: BudgetUsage) -> None:
        if not reservation_id:
            raise ValueError("reservation_id cannot be empty")
        if reservation_id in self._reservations:
            raise ValueError(f"reservation already exists: {reservation_id}")
        projected = sum_usage((self._used, self.reserved, maximum))
        exceeded = projected.exceeded_limits(self.limits)
        if exceeded:
            raise BudgetExceededError(exceeded)
        self._reservations[reservation_id] = maximum

    def reconcile(self, reservation_id: str, actual: BudgetUsage) -> None:
        try:
            maximum = self._reservations[reservation_id]
        except KeyError as error:
            raise KeyError(f"unknown reservation: {reservation_id}") from error
        if not _fits_within(actual, maximum):
            raise ReservationExceededError(
                f"actual usage exceeds conservative reservation {reservation_id}"
            )
        self._used = sum_usage((self._used, actual))
        del self._reservations[reservation_id]

    def release(self, reservation_id: str) -> None:
        try:
            del self._reservations[reservation_id]
        except KeyError as error:
            raise KeyError(f"unknown reservation: {reservation_id}") from error
