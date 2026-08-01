"""Evaluator boundary shared by trusted local and isolated container adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from guildmind.evaluation.local import LocalEvaluationResult, LocalEvaluationSpec


class Evaluator(Protocol):
    """Turn one validated fixture patch into objective evaluation evidence."""

    @property
    def evaluator_version(self) -> str:
        """Return the versioned evaluator protocol/implementation identity."""
        ...

    @property
    def environment_digest(self) -> str:
        """Return the immutable execution environment as ``sha256:<digest>``."""
        ...

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
    ) -> LocalEvaluationResult:
        """Evaluate a patch without exposing grader inputs to the worker."""
        ...
