"""Injectable clocks for reproducible event streams."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol


@dataclass(frozen=True)
class ClockStamp:
    occurred_at: datetime
    monotonic_ns: int


class Clock(Protocol):
    def stamp(self) -> ClockStamp:
        """Return a mutually consistent wall and monotonic timestamp."""


class SystemClock:
    def stamp(self) -> ClockStamp:
        return ClockStamp(
            occurred_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
        )


class DeterministicClock:
    """A thread-safe fake clock advancing once per observed event."""

    def __init__(
        self,
        *,
        started_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
        step: timedelta = timedelta(milliseconds=1),
    ) -> None:
        if started_at.tzinfo is None or started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        if step <= timedelta(0):
            raise ValueError("step must be positive")
        self._started_at = started_at.astimezone(UTC)
        self._step = step
        self._index = 0
        self._lock = Lock()

    def stamp(self) -> ClockStamp:
        with self._lock:
            index = self._index
            self._index += 1
        elapsed = self._step * index
        return ClockStamp(
            occurred_at=self._started_at + elapsed,
            monotonic_ns=int(elapsed.total_seconds() * 1_000_000_000),
        )
