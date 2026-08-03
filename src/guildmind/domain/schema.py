"""Side-effect-free JSON Schema export for public domain records."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from guildmind.domain.campaign import (
    ReliabilityCampaignManifest,
    ReliabilityCampaignReport,
)
from guildmind.domain.models import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EvaluationResult,
    EventRecord,
    ExperimentSpec,
    RunManifest,
    TaskSpec,
)

DOMAIN_MODELS: tuple[type[BaseModel], ...] = (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EvaluationResult,
    EventRecord,
    ExperimentSpec,
    ReliabilityCampaignManifest,
    ReliabilityCampaignReport,
    RunManifest,
    TaskSpec,
)


def export_json_schemas() -> dict[str, dict[str, Any]]:
    """Return validation schemas keyed by stable public model name.

    The helper deliberately performs no file I/O; a CLI or build step can decide where
    and how to persist the returned schemas.
    """
    return {
        model.__name__: model.model_json_schema(mode="validation")
        for model in sorted(DOMAIN_MODELS, key=lambda item: item.__name__)
    }
