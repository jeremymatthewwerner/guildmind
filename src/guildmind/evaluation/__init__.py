"""Evaluation adapters and result types."""

from guildmind.evaluation.base import Evaluator
from guildmind.evaluation.container import ContainerEvaluator, ContainerEvaluatorResources
from guildmind.evaluation.corpus import (
    AdversarialCase,
    AdversarialCorpus,
    AdversarialExpectation,
    load_adversarial_corpus,
)
from guildmind.evaluation.local import (
    EvaluationStatus,
    FixtureConfigurationError,
    LocalEvaluationResult,
    LocalEvaluationSpec,
    LocalEvaluator,
    PythonCallProtocol,
    load_fixture,
)
from guildmind.evaluation.protocol import PythonCallBundle, load_python_call_bundle
from guildmind.evaluation.qualification import (
    require_tracked_clean_revision,
    write_new_report,
)

__all__ = [
    "AdversarialCase",
    "AdversarialCorpus",
    "AdversarialExpectation",
    "ContainerEvaluator",
    "ContainerEvaluatorResources",
    "EvaluationStatus",
    "Evaluator",
    "FixtureConfigurationError",
    "LocalEvaluationResult",
    "LocalEvaluationSpec",
    "LocalEvaluator",
    "PythonCallBundle",
    "PythonCallProtocol",
    "load_adversarial_corpus",
    "load_fixture",
    "load_python_call_bundle",
    "require_tracked_clean_revision",
    "write_new_report",
]
