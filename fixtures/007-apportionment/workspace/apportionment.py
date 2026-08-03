"""Proportional integer allocation."""


def apportion(total: int, weights: list[int]) -> list[int]:
    """Allocate ``total`` in proportion to non-negative integer weights."""

    weight_sum = sum(weights)
    return [round(total * weight / weight_sum) for weight in weights]
