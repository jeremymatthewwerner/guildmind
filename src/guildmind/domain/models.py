"""Versioned, immutable domain records for the Stage 1 measurement substrate."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

from guildmind.domain.serialization import canonical_sha256

SchemaVersion = Literal["0.1"]
NonEmptyStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
Sha256 = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
Sha256Digest = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeFloat = Annotated[
    float,
    Field(strict=True, ge=0, allow_inf_nan=False),
]


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_normalize_utc)]


class DomainModel(BaseModel):
    """Shared validation policy for versioned evidence records."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ArtifactRef(DomainModel):
    """Reference to immutable bytes in a content-addressed artifact store."""

    schema_version: SchemaVersion = "0.1"
    media_type: NonEmptyStr
    size_bytes: NonNegativeInt
    sha256: Sha256
    storage_ref: NonEmptyStr


class BudgetLimits(DomainModel):
    """Aggregate hard caps for one run; ``None`` means that dimension is uncapped."""

    schema_version: SchemaVersion = "0.1"
    max_input_tokens: NonNegativeInt | None = None
    max_output_tokens: NonNegativeInt | None = None
    max_total_tokens: NonNegativeInt | None = None
    max_model_calls: NonNegativeInt | None = None
    max_model_retries: NonNegativeInt | None = None
    max_tool_calls: NonNegativeInt | None = None
    max_tool_cpu_seconds: NonNegativeFloat | None = None
    max_container_wall_seconds: NonNegativeFloat | None = None
    max_elapsed_seconds: NonNegativeFloat | None = None
    max_estimated_cost_usd: NonNegativeFloat | None = None


class BudgetUsage(DomainModel):
    """Normalized cumulative usage for one run.

    The three input-token fields are disjoint. ``reasoning_tokens`` is a diagnostic
    subset of ``output_tokens`` and is therefore not counted a second time.
    """

    schema_version: SchemaVersion = "0.1"
    uncached_input_tokens: NonNegativeInt = 0
    cache_read_input_tokens: NonNegativeInt = 0
    cache_write_input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    reasoning_tokens: NonNegativeInt = 0
    model_calls: NonNegativeInt = 0
    model_retries: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    tool_cpu_seconds: NonNegativeFloat = 0.0
    container_wall_seconds: NonNegativeFloat = 0.0
    elapsed_seconds: NonNegativeFloat = 0.0
    estimated_cost_usd: NonNegativeFloat = 0.0

    @model_validator(mode="after")
    def _reasoning_is_output_subset(self) -> Self:
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens cannot exceed output_tokens")
        return self

    @property
    def input_tokens(self) -> int:
        return (
            self.uncached_input_tokens
            + self.cache_read_input_tokens
            + self.cache_write_input_tokens
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def exceeded_limits(self, limits: BudgetLimits) -> tuple[str, ...]:
        """Return stable names for every cap exceeded by this usage snapshot."""
        checks = (
            ("input_tokens", self.input_tokens, limits.max_input_tokens),
            ("output_tokens", self.output_tokens, limits.max_output_tokens),
            ("total_tokens", self.total_tokens, limits.max_total_tokens),
            ("model_calls", self.model_calls, limits.max_model_calls),
            ("model_retries", self.model_retries, limits.max_model_retries),
            ("tool_calls", self.tool_calls, limits.max_tool_calls),
            ("tool_cpu_seconds", self.tool_cpu_seconds, limits.max_tool_cpu_seconds),
            (
                "container_wall_seconds",
                self.container_wall_seconds,
                limits.max_container_wall_seconds,
            ),
            ("elapsed_seconds", self.elapsed_seconds, limits.max_elapsed_seconds),
            (
                "estimated_cost_usd",
                self.estimated_cost_usd,
                limits.max_estimated_cost_usd,
            ),
        )
        return tuple(name for name, actual, limit in checks if limit is not None and actual > limit)


class TaskSpec(DomainModel):
    """Immutable benchmark task and the exact worker-visible inputs."""

    schema_version: SchemaVersion = "0.1"
    task_id: NonEmptyStr
    source: NonEmptyStr
    split: NonEmptyStr
    repository: NonEmptyStr
    repository_commit: NonEmptyStr
    image_digest: Sha256Digest
    task_content_hash: Sha256
    problem_statement: ArtifactRef
    repository_snapshot: ArtifactRef
    visible_tests: tuple[ArtifactRef, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ExperimentSpec(DomainModel):
    """Frozen hypothesis and equal-budget comparison contract."""

    schema_version: SchemaVersion = "0.1"
    experiment_id: NonEmptyStr
    hypothesis: NonEmptyStr
    candidate_ids: tuple[NonEmptyStr, ...]
    task_ids: tuple[NonEmptyStr, ...]
    task_split: NonEmptyStr
    repeats: PositiveInt = 1
    budget_limits: BudgetLimits
    metrics: tuple[NonEmptyStr, ...]
    stopping_rule: NonEmptyStr
    retry_limit: NonNegativeInt = 0
    seed: NonNegativeInt = 0

    @field_validator("candidate_ids", "task_ids", "metrics")
    @classmethod
    def _require_nonempty_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("collection must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("collection values must be unique")
        return value


class RunStatus(StrEnum):
    """Lifecycle state for an attempted run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INFRASTRUCTURE_ERROR = "infrastructure_error"

    @property
    def is_terminal(self) -> bool:
        return self not in {RunStatus.PENDING, RunStatus.RUNNING}


class RunManifest(DomainModel):
    """Immutable snapshot of one attempted execution and its lifecycle state."""

    schema_version: SchemaVersion = "0.1"
    run_id: NonEmptyStr
    experiment_id: NonEmptyStr
    task_id: NonEmptyStr
    candidate_id: NonEmptyStr
    genome_hash: Sha256 | None = None
    requested_model: NonEmptyStr
    returned_model: NonEmptyStr | None = None
    model_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    seed: NonNegativeInt
    environment_digest: Sha256Digest
    code_revision: NonEmptyStr
    budget_limits: BudgetLimits
    status: RunStatus = RunStatus.PENDING
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    terminal_reason: NonEmptyStr | None = None
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> Self:
        if self.started_at is not None and self.started_at < self.created_at:
            raise ValueError("started_at cannot precede created_at")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("finished_at cannot precede created_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at cannot precede started_at")

        if self.status is RunStatus.PENDING:
            if self.started_at is not None or self.finished_at is not None:
                raise ValueError("pending runs cannot have start or finish timestamps")
            if self.terminal_reason is not None:
                raise ValueError("pending runs cannot have a terminal reason")
        elif self.status is RunStatus.RUNNING:
            if self.started_at is None:
                raise ValueError("running runs require started_at")
            if self.finished_at is not None or self.terminal_reason is not None:
                raise ValueError("running runs cannot be terminal")
        elif self.status is RunStatus.SUCCEEDED:
            if self.started_at is None or self.finished_at is None:
                raise ValueError("successful runs require start and finish timestamps")
            if self.terminal_reason is not None:
                raise ValueError("successful runs cannot have a terminal reason")
        else:
            if self.finished_at is None:
                raise ValueError("terminal runs require finished_at")
            if self.terminal_reason is None:
                raise ValueError("non-success terminal runs require a terminal reason")
        return self


class EventRecord(DomainModel):
    """One append-only, hash-linked run state transition."""

    schema_version: SchemaVersion = "0.1"
    event_version: SchemaVersion = "0.1"
    event_id: NonEmptyStr
    run_id: NonEmptyStr
    sequence: NonNegativeInt
    causal_parent_ids: tuple[NonEmptyStr, ...] = ()
    event_type: NonEmptyStr
    monotonic_ns: NonNegativeInt
    occurred_at: UtcDatetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    payload_sha256: Sha256
    previous_event_hash: Sha256 | None = None

    @field_validator("causal_parent_ids")
    @classmethod
    def _causal_parents_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("causal parent IDs must be unique")
        return value

    @model_validator(mode="after")
    def _validate_hash_chain(self) -> Self:
        if self.sequence == 0 and self.previous_event_hash is not None:
            raise ValueError("the first event cannot have a previous event hash")
        if self.sequence > 0 and self.previous_event_hash is None:
            raise ValueError("events after sequence zero require a previous event hash")
        if self.payload_sha256 != canonical_sha256(self.payload):
            raise ValueError("payload_sha256 does not match the canonical payload")
        return self

    def content_hash(self) -> str:
        """Return the hash linked by the next event in this run."""
        return canonical_sha256(self)


EvaluationOutcome = Literal["passed", "failed", "error", "not_run"]


class EvaluationResult(DomainModel):
    """Content-verified result from a versioned, isolated evaluator."""

    schema_version: SchemaVersion = "0.1"
    evaluation_id: NonEmptyStr
    run_id: NonEmptyStr
    run_status: RunStatus
    evaluator_version: NonEmptyStr
    task_hash: Sha256
    patch_hash: Sha256 | None = None
    outcome: EvaluationOutcome
    score: NonNegativeFloat | None = None
    result: dict[str, JsonValue] = Field(default_factory=dict)
    result_sha256: Sha256
    evidence: tuple[ArtifactRef, ...] = ()
    evaluated_at: UtcDatetime
    signature: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        if not self.run_status.is_terminal:
            raise ValueError("evaluations require a terminal run state")
        if self.outcome == "not_run" and self.patch_hash is not None:
            raise ValueError("not_run evaluations cannot have a patch hash")
        if self.outcome != "not_run" and self.patch_hash is None:
            raise ValueError("completed evaluations require a patch hash")
        compatible_statuses = {
            "passed": {RunStatus.SUCCEEDED},
            "failed": {RunStatus.FAILED, RunStatus.TIMED_OUT},
            "error": {RunStatus.INFRASTRUCTURE_ERROR},
            "not_run": {
                RunStatus.CANCELLED,
                RunStatus.BUDGET_EXHAUSTED,
                RunStatus.INFRASTRUCTURE_ERROR,
            },
        }
        if self.run_status not in compatible_statuses[self.outcome]:
            raise ValueError("evaluation outcome does not match its terminal run state")
        if self.result_sha256 != canonical_sha256(self.result):
            raise ValueError("result_sha256 does not match the canonical result")
        return self
