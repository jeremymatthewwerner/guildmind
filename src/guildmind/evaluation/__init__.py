"""Evaluation adapters and result types."""

from guildmind.evaluation.base import Evaluator
from guildmind.evaluation.container import ContainerEvaluator, ContainerEvaluatorResources
from guildmind.evaluation.local import (
    EvaluationStatus,
    FixtureConfigurationError,
    LocalEvaluationResult,
    LocalEvaluationSpec,
    LocalEvaluator,
    load_fixture,
)

__all__ = [
    "ContainerEvaluator",
    "ContainerEvaluatorResources",
    "EvaluationStatus",
    "Evaluator",
    "FixtureConfigurationError",
    "LocalEvaluationResult",
    "LocalEvaluationSpec",
    "LocalEvaluator",
    "load_fixture",
]
