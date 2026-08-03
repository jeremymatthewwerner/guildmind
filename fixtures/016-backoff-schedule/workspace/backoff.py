"""Deterministic retry-delay schedules."""

Number = int | float


def backoff_schedule(base: Number, factor: Number, cap: Number, attempts: int) -> list[Number]:
    """Return the uncapped geometric delays for ``attempts`` entries."""

    return [base * factor**index for index in range(attempts)]
