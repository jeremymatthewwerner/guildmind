"""Narrow provider-neutral model interface for the first runner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from guildmind.domain import BudgetUsage


@dataclass(frozen=True, slots=True)
class ModelResponse:
    patch: bytes
    usage: BudgetUsage
    returned_model: str


class ModelClient(Protocol):
    @property
    def model_id(self) -> str:
        """Return the requested immutable model identifier."""

    @property
    def maximum_usage(self) -> BudgetUsage:
        """Return the conservative reservation required before dispatch."""

    def propose_patch(self, problem_statement: str) -> ModelResponse:
        """Return one patch without mutating the task workspace."""
