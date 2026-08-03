"""Versioned evidence records for bounded fixture-reliability campaigns."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from guildmind.domain.models import (
    BudgetLimits,
    DomainModel,
    NonEmptyStr,
    NonNegativeInt,
    PositiveInt,
    RunStatus,
    Sha256,
    Sha256Digest,
    UtcDatetime,
)
from guildmind.domain.serialization import canonical_json, canonical_sha256, sha256_bytes

CampaignId = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$"),
]
CampaignRate = Annotated[
    float,
    Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False),
]
StrictBool = Annotated[bool, Field(strict=True)]


class CampaignEvidenceTier(StrEnum):
    """Environment class declared before a campaign is executed."""

    DEVELOPMENT = "development"
    REFERENCE = "reference"


class CampaignEvaluatorKind(StrEnum):
    """Evaluator adapter selected by the immutable campaign contract."""

    LOCAL = "local"
    CONTAINER = "container"


class CampaignAttemptDisposition(StrEnum):
    """Exhaustive reconciliation result for one declared attempt."""

    EXPECTED = "expected"
    UNEXPECTED_RESULT = "unexpected_result"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_RUN_SET_INVALID = "evidence_run_set_invalid"
    EVIDENCE_NONTERMINAL = "evidence_nonterminal"
    EVIDENCE_INVALID = "evidence_invalid"
    POST_COMMIT_MAINTENANCE_ERROR = "post_commit_maintenance_error"

    @property
    def is_infrastructure_error(self) -> bool:
        return self not in {
            CampaignAttemptDisposition.EXPECTED,
            CampaignAttemptDisposition.UNEXPECTED_RESULT,
        }


class ReliabilityCampaignEvaluator(DomainModel):
    """Exact evaluator implementation and immutable execution environment."""

    kind: CampaignEvaluatorKind
    evaluator_version: NonEmptyStr
    environment_digest: Sha256Digest


class ReliabilityCampaignModel(DomainModel):
    """The only model adapter authorized by the initial reliability harness."""

    kind: Literal["scripted_patch"] = "scripted_patch"
    model_id: NonEmptyStr


class ReliabilityCampaignFixture(DomainModel):
    """One complete, content-bound known-outcome fixture tree."""

    fixture_id: CampaignId
    fixture_path: NonEmptyStr
    fixture_tree_sha256: Sha256
    solution_patch_sha256: Sha256
    expected_run_status: Literal[RunStatus.SUCCEEDED] = RunStatus.SUCCEEDED
    expected_evaluation_outcome: Literal["passed"] = "passed"

    @field_validator("fixture_path")
    @classmethod
    def _fixture_path_is_plain_relative_posix(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("fixture_path must be a plain POSIX relative path")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != value
        ):
            raise ValueError("fixture_path must be a plain POSIX relative path")
        return value


class ReliabilityCampaignAttempt(DomainModel):
    """One explicit, non-retry schedule entry."""

    attempt_id: CampaignId
    fixture_id: CampaignId
    round_index: NonNegativeInt
    seed: NonNegativeInt


class ReliabilityCampaignManifest(DomainModel):
    """Frozen full-factorial schedule for a normal-fixture reliability campaign."""

    schema_version: Literal["guildmind.reliability-campaign/v1"] = (
        "guildmind.reliability-campaign/v1"
    )
    report_schema_version: Literal["guildmind.reliability-campaign-report/v1"] = (
        "guildmind.reliability-campaign-report/v1"
    )
    campaign_id: CampaignId
    evidence_tier: CampaignEvidenceTier
    experiment_id: CampaignId
    candidate_id: CampaignId
    code_source_sha256: Sha256
    evaluator: ReliabilityCampaignEvaluator
    model: ReliabilityCampaignModel
    budget_limits: BudgetLimits
    rounds: PositiveInt
    retry_limit: Literal[0] = 0
    maximum_infrastructure_error_rate: CampaignRate
    fixtures: Annotated[
        tuple[ReliabilityCampaignFixture, ...],
        Field(min_length=1, max_length=100),
    ]
    attempts: Annotated[
        tuple[ReliabilityCampaignAttempt, ...],
        Field(min_length=1, max_length=10_000),
    ]

    @model_validator(mode="after")
    def _schedule_is_complete_unique_and_canonical(self) -> Self:
        fixture_ids = tuple(fixture.fixture_id for fixture in self.fixtures)
        if fixture_ids != tuple(sorted(set(fixture_ids))):
            raise ValueError("campaign fixtures must be unique and ordered by fixture_id")
        fixture_paths = tuple(fixture.fixture_path for fixture in self.fixtures)
        if len(fixture_paths) != len(set(fixture_paths)):
            raise ValueError("campaign fixture paths must be unique")

        attempt_ids = tuple(attempt.attempt_id for attempt in self.attempts)
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("campaign attempt IDs must be unique")

        expected_bindings = tuple(
            (fixture_id, round_index)
            for round_index in range(self.rounds)
            for fixture_id in fixture_ids
        )
        observed_bindings = tuple(
            (attempt.fixture_id, attempt.round_index) for attempt in self.attempts
        )
        if observed_bindings != expected_bindings:
            raise ValueError("campaign attempts must be the complete round-major fixture schedule")
        if self.evidence_tier is CampaignEvidenceTier.REFERENCE and (
            self.evaluator.kind is not CampaignEvaluatorKind.CONTAINER
        ):
            raise ValueError("reference campaigns require the container evaluator")
        return self

    @property
    def content_sha256(self) -> str:
        return canonical_sha256(self)


class ReliabilityCampaignTerminalEvidence(DomainModel):
    """Verified terminal ledger/replay/storage commitment for one attempt."""

    run_id: CampaignId
    task_id: NonEmptyStr
    run_status: RunStatus
    evaluation_outcome: NonEmptyStr | None
    manifest_revision: NonNegativeInt
    manifest_sha256: Sha256
    event_count: PositiveInt
    head_event_sha256: Sha256
    semantic_digest: Sha256
    ledger_snapshot_sha256: Sha256
    storage_state: NonEmptyStr
    references_verified: StrictBool
    storage_clean: StrictBool

    @model_validator(mode="after")
    def _run_is_terminal(self) -> Self:
        if not self.run_status.is_terminal:
            raise ValueError("campaign terminal evidence requires a terminal run")
        if self.manifest_revision >= self.event_count:
            raise ValueError("manifest revision must be less than event count")
        return self


class ReliabilityCampaignAttemptEvidence(DomainModel):
    """One schedule entry reconciled against its isolated evidence directory."""

    schema_version: Literal["guildmind.reliability-campaign-attempt/v1"] = (
        "guildmind.reliability-campaign-attempt/v1"
    )
    attempt: ReliabilityCampaignAttempt
    disposition: CampaignAttemptDisposition
    observed_run_ids: tuple[CampaignId, ...]
    terminal: ReliabilityCampaignTerminalEvidence | None = None
    recovery_attempted: StrictBool = False
    recovery_succeeded: StrictBool = False
    execution_error_type: NonEmptyStr | None = None
    diagnostic: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _shape_matches_disposition(self) -> Self:
        if self.observed_run_ids != tuple(sorted(set(self.observed_run_ids))):
            raise ValueError("observed run IDs must be unique and ordered")
        if self.recovery_succeeded and not self.recovery_attempted:
            raise ValueError("successful recovery requires a recovery attempt")
        if self.terminal is not None and self.terminal.run_id != self.attempt.attempt_id:
            raise ValueError("terminal run ID must equal the declared attempt ID")
        terminal_required = {
            CampaignAttemptDisposition.EXPECTED,
            CampaignAttemptDisposition.UNEXPECTED_RESULT,
            CampaignAttemptDisposition.POST_COMMIT_MAINTENANCE_ERROR,
        }
        if self.disposition in terminal_required and self.terminal is None:
            raise ValueError("campaign disposition requires terminal evidence")
        if (
            self.disposition
            in {
                CampaignAttemptDisposition.EXPECTED,
                CampaignAttemptDisposition.UNEXPECTED_RESULT,
            }
            and self.execution_error_type is not None
        ):
            raise ValueError("ordinary result dispositions cannot retain an execution error")
        if self.disposition is CampaignAttemptDisposition.EVIDENCE_MISSING and (
            self.observed_run_ids or self.terminal is not None
        ):
            raise ValueError("missing evidence cannot contain observed runs")
        if self.disposition is CampaignAttemptDisposition.EVIDENCE_NONTERMINAL and (
            self.observed_run_ids != (self.attempt.attempt_id,) or self.terminal is not None
        ):
            raise ValueError("nonterminal evidence requires exactly the declared run")
        return self

    @property
    def is_infrastructure_error(self) -> bool:
        return self.disposition.is_infrastructure_error

    @property
    def is_reconciled_terminal(self) -> bool:
        return (
            self.terminal is not None
            and self.observed_run_ids == (self.attempt.attempt_id,)
            and self.terminal.references_verified
            and self.terminal.storage_clean
        )


class ReliabilityCampaignReportBody(DomainModel):
    """Self-contained report body with all aggregate claims strictly derived."""

    schema_version: Literal["guildmind.reliability-campaign-report-body/v1"] = (
        "guildmind.reliability-campaign-report-body/v1"
    )
    manifest: ReliabilityCampaignManifest
    source_manifest_sha256: Sha256
    campaign_manifest_sha256: Sha256
    git_revision: NonEmptyStr
    recorded_at: UtcDatetime
    state_manifest_verified: StrictBool
    code_identity_verified: StrictBool
    attempts: tuple[ReliabilityCampaignAttemptEvidence, ...]
    intended_attempt_count: PositiveInt
    terminal_attempt_count: NonNegativeInt
    reconciled_attempt_count: NonNegativeInt
    expected_attempt_count: NonNegativeInt
    infrastructure_error_count: NonNegativeInt
    infrastructure_error_rate: CampaignRate
    complete: StrictBool
    all_expected: StrictBool
    threshold_met: StrictBool
    campaign_passed: StrictBool

    @model_validator(mode="after")
    def _aggregate_claims_are_exactly_derived(self) -> Self:
        if self.campaign_manifest_sha256 != self.manifest.content_sha256:
            raise ValueError("campaign manifest hash does not match the retained manifest")
        declared_attempts = self.manifest.attempts
        observed_attempts = tuple(evidence.attempt for evidence in self.attempts)
        if observed_attempts != declared_attempts:
            raise ValueError("campaign report attempts must equal the declared schedule")

        intended = len(declared_attempts)
        terminal = sum(evidence.terminal is not None for evidence in self.attempts)
        reconciled = sum(evidence.is_reconciled_terminal for evidence in self.attempts)
        expected = sum(
            evidence.disposition is CampaignAttemptDisposition.EXPECTED
            for evidence in self.attempts
        )
        infrastructure_errors = sum(evidence.is_infrastructure_error for evidence in self.attempts)
        rate = infrastructure_errors / intended
        complete = reconciled == intended
        all_expected = expected == intended
        threshold_met = rate <= self.manifest.maximum_infrastructure_error_rate
        campaign_passed = (
            self.state_manifest_verified
            and self.code_identity_verified
            and complete
            and all_expected
            and threshold_met
        )
        observed = (
            self.intended_attempt_count,
            self.terminal_attempt_count,
            self.reconciled_attempt_count,
            self.expected_attempt_count,
            self.infrastructure_error_count,
            self.infrastructure_error_rate,
            self.complete,
            self.all_expected,
            self.threshold_met,
            self.campaign_passed,
        )
        derived = (
            intended,
            terminal,
            reconciled,
            expected,
            infrastructure_errors,
            rate,
            complete,
            all_expected,
            threshold_met,
            campaign_passed,
        )
        if observed != derived:
            raise ValueError("campaign report aggregate claims must be derived")
        return self


class ReliabilityCampaignReport(DomainModel):
    """Hash-bound canonical envelope for one complete campaign observation."""

    schema_version: Literal["guildmind.reliability-campaign-report/v1"] = (
        "guildmind.reliability-campaign-report/v1"
    )
    body: ReliabilityCampaignReportBody
    body_sha256: Sha256

    @model_validator(mode="after")
    def _body_hash_matches(self) -> Self:
        if self.body_sha256 != canonical_sha256(self.body):
            raise ValueError("campaign report body hash mismatch")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())
