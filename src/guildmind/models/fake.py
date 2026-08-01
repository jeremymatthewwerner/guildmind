"""Zero-cost scripted model used by deterministic integration tests."""

from __future__ import annotations

from pathlib import Path

from guildmind.domain import BudgetUsage
from guildmind.models.base import ModelResponse


class ScriptedPatchModel:
    def __init__(
        self,
        patch_path: Path,
        *,
        model_id: str = "guildmind/fake-scripted-patch-v1",
        maximum_usage: BudgetUsage | None = None,
        actual_usage: BudgetUsage | None = None,
    ) -> None:
        self.patch_path = patch_path
        self._model_id = model_id
        self._maximum_usage = maximum_usage or BudgetUsage(
            uncached_input_tokens=64,
            output_tokens=64,
            model_calls=1,
        )
        self._actual_usage = actual_usage or BudgetUsage(
            uncached_input_tokens=24,
            output_tokens=16,
            model_calls=1,
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def maximum_usage(self) -> BudgetUsage:
        return self._maximum_usage

    def propose_patch(self, problem_statement: str) -> ModelResponse:
        if not problem_statement.strip():
            raise ValueError("problem statement cannot be empty")
        return ModelResponse(
            patch=self.patch_path.read_bytes(),
            usage=self._actual_usage,
            returned_model=self.model_id,
        )
