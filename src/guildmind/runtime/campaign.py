"""Strict loading, execution, reconciliation, and publication for reliability campaigns."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from guildmind.domain import (
    CampaignAttemptDisposition,
    CampaignEvaluatorKind,
    CampaignEvidenceTier,
    ReliabilityCampaignAttempt,
    ReliabilityCampaignAttemptEvidence,
    ReliabilityCampaignFixture,
    ReliabilityCampaignManifest,
    ReliabilityCampaignReport,
    ReliabilityCampaignReportBody,
    ReliabilityCampaignTerminalEvidence,
    RunStatus,
    canonical_json,
    canonical_sha256,
    sha256_bytes,
)
from guildmind.evaluation import Evaluator, LocalEvaluator, load_fixture
from guildmind.models import ScriptedPatchModel
from guildmind.runtime.budget import BudgetExceededError
from guildmind.runtime.clock import Clock, SystemClock
from guildmind.runtime.recovery import recover_existing_fixture_run
from guildmind.runtime.replay import ReplayIntegrityError, replay_events, semantic_digest
from guildmind.runtime.runner import FixtureRunner, FixtureRunPostCommitMaintenanceError
from guildmind.storage import EventStore, StoreIntegrityError, audit_storage
from guildmind.storage._fsops import rename_noreplace_at

_MANIFEST_MAX_BYTES = 1_048_576
_REPORT_MAX_BYTES = 16_777_216
_TREE_MAX_FILES = 20_000
_TREE_MAX_BYTES = 268_435_456
_STATE_MANIFEST_NAME = "campaign-manifest.json"
_ATTEMPTS_DIRECTORY_NAME = "attempts"
_CODE_IDENTITY_SCHEMA = "guildmind.campaign-code-source/v1"
_FIXTURE_IDENTITY_SCHEMA = "guildmind.campaign-fixture-tree/v1"


class CampaignConfigurationError(ValueError):
    """Raised before dispatch when a campaign contract or local input is invalid."""


class CampaignEvidenceError(RuntimeError):
    """Raised when canonical campaign evidence cannot be safely published or loaded."""


@dataclass(frozen=True, slots=True)
class LoadedReliabilityCampaignFixture:
    """Verified fixture declaration bound to its real local paths."""

    specification: ReliabilityCampaignFixture
    root: Path
    solution_patch: Path


@dataclass(frozen=True, slots=True)
class LoadedReliabilityCampaign:
    """Side-effect-free result of strict campaign and fixture verification."""

    manifest_path: Path
    repository_root: Path
    source_manifest_sha256: str
    manifest: ReliabilityCampaignManifest
    fixtures: tuple[LoadedReliabilityCampaignFixture, ...]

    def fixture(self, fixture_id: str) -> LoadedReliabilityCampaignFixture:
        for fixture in self.fixtures:
            if fixture.specification.fixture_id == fixture_id:
                return fixture
        raise KeyError(f"unknown campaign fixture: {fixture_id}")


@dataclass(frozen=True, slots=True)
class _AttemptExecutionNote:
    error_type: str | None = None
    post_commit_maintenance_error: bool = False
    recovery_attempted: bool = False
    recovery_succeeded: bool = False
    diagnostic: str | None = None


def load_reliability_campaign(
    manifest_path: Path,
    *,
    repository_root: Path,
) -> LoadedReliabilityCampaign:
    """Load one closed campaign contract without creating campaign state."""

    repository = _canonical_real_directory(repository_root, label="repository root")
    manifest = _canonical_existing_path(manifest_path, label="campaign manifest")
    if not manifest.is_relative_to(repository):
        raise CampaignConfigurationError("campaign manifest must be inside the repository root")
    manifest_bytes = _read_regular_file(
        manifest,
        label="campaign manifest",
        max_bytes=_MANIFEST_MAX_BYTES,
    )
    raw_manifest = _strict_json_object(manifest_bytes, label="campaign manifest")
    try:
        specification = ReliabilityCampaignManifest.model_validate(raw_manifest)
    except ValidationError as error:
        raise CampaignConfigurationError(f"campaign manifest is invalid: {error}") from error

    observed_code_sha256 = campaign_code_source_sha256(repository)
    if observed_code_sha256 != specification.code_source_sha256:
        raise CampaignConfigurationError(
            "campaign code source digest mismatch: "
            f"expected {specification.code_source_sha256}, observed {observed_code_sha256}"
        )

    loaded_fixtures: list[LoadedReliabilityCampaignFixture] = []
    for fixture in specification.fixtures:
        fixture_root = _plain_repository_descendant(repository, fixture.fixture_path)
        observed_tree_sha256 = campaign_fixture_tree_sha256(fixture_root)
        if observed_tree_sha256 != fixture.fixture_tree_sha256:
            raise CampaignConfigurationError(
                f"campaign fixture tree digest mismatch for {fixture.fixture_id}: "
                f"expected {fixture.fixture_tree_sha256}, observed {observed_tree_sha256}"
            )
        task_manifest = _read_regular_file(
            fixture_root / "task.json",
            label=f"campaign fixture {fixture.fixture_id} task manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
        raw_task = _strict_json_object(
            task_manifest,
            label=f"campaign fixture {fixture.fixture_id} task manifest",
        )
        if raw_task.get("task_id") != fixture.fixture_id:
            raise CampaignConfigurationError(
                f"campaign fixture ID does not match task.json for {fixture.fixture_id}"
            )
        solution_patch = fixture_root / "solution.patch"
        solution_bytes = _read_regular_file(
            solution_patch,
            label=f"campaign fixture {fixture.fixture_id} solution patch",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
        if sha256_bytes(solution_bytes) != fixture.solution_patch_sha256:
            raise CampaignConfigurationError(
                f"campaign solution patch digest mismatch for {fixture.fixture_id}"
            )
        try:
            loaded_spec = load_fixture(fixture_root)
        except (OSError, ValueError) as error:
            raise CampaignConfigurationError(
                f"campaign fixture {fixture.fixture_id} cannot be loaded: {error}"
            ) from error
        if loaded_spec.task_id != fixture.fixture_id:
            raise CampaignConfigurationError(
                f"loaded campaign fixture ID disagrees for {fixture.fixture_id}"
            )
        if campaign_fixture_tree_sha256(fixture_root) != fixture.fixture_tree_sha256:
            raise CampaignConfigurationError(
                f"campaign fixture {fixture.fixture_id} changed while it was loaded"
            )
        loaded_fixtures.append(
            LoadedReliabilityCampaignFixture(
                specification=fixture,
                root=fixture_root,
                solution_patch=solution_patch,
            )
        )

    return LoadedReliabilityCampaign(
        manifest_path=manifest,
        repository_root=repository,
        source_manifest_sha256=sha256_bytes(manifest_bytes),
        manifest=specification,
        fixtures=tuple(loaded_fixtures),
    )


def campaign_code_source_sha256(repository_root: Path) -> str:
    """Hash the package source and locked Python build/runtime inputs."""

    repository = _canonical_real_directory(repository_root, label="repository root")
    package_root = repository / "src" / "guildmind"
    entries = list(
        _tree_entries(
            package_root,
            relative_to=repository,
            label="Guildmind package source",
            ignore_generated_python=True,
        )
    )
    for relative_name in ("pyproject.toml", "uv.lock"):
        path = repository / relative_name
        data, mode = _identity_file(path, label=f"code identity input {relative_name}")
        entries.append(_file_entry(relative_name, data, mode))
    entries.sort(key=lambda entry: str(entry["path"]))
    return canonical_sha256(
        {
            "files": entries,
            "schema_version": _CODE_IDENTITY_SCHEMA,
        }
    )


def campaign_fixture_tree_sha256(fixture_root: Path) -> str:
    """Hash every regular entry in one closed repository-owned fixture tree."""

    root = _canonical_real_directory(fixture_root, label="campaign fixture root")
    entries = _tree_entries(
        root,
        relative_to=root,
        label="campaign fixture tree",
        ignore_generated_python=False,
    )
    return canonical_sha256(
        {
            "files": entries,
            "schema_version": _FIXTURE_IDENTITY_SCHEMA,
        }
    )


def run_reliability_campaign(
    campaign: LoadedReliabilityCampaign,
    *,
    state_directory: Path,
    evaluator: Evaluator | None = None,
    clock_factory: Callable[[int], Clock] | None = None,
    git_revision: str,
    recorded_at: datetime | None = None,
) -> ReliabilityCampaignReport:
    """Execute every declared attempt exactly once, then reconcile all evidence."""

    selected_evaluator = evaluator or LocalEvaluator()
    _require_supported_execution(campaign, selected_evaluator)
    state = _prepare_new_campaign_state(state_directory)
    write_campaign_evidence_file(
        canonical_json(campaign.manifest).encode("utf-8"),
        state / _STATE_MANIFEST_NAME,
    )
    attempts_directory = state / _ATTEMPTS_DIRECTORY_NAME
    notes: dict[str, _AttemptExecutionNote] = {}
    make_clock = clock_factory or (lambda _: SystemClock())
    code_revision = f"source-sha256:{campaign.manifest.code_source_sha256}"

    for index, attempt in enumerate(campaign.manifest.attempts):
        fixture = campaign.fixture(attempt.fixture_id)
        attempt_state = attempts_directory / attempt.attempt_id
        observed_fixture_sha256 = campaign_fixture_tree_sha256(fixture.root)
        if observed_fixture_sha256 != fixture.specification.fixture_tree_sha256:
            notes[attempt.attempt_id] = _AttemptExecutionNote(
                error_type="CampaignConfigurationError",
                diagnostic="fixture_identity_changed_before_dispatch",
            )
            continue

        clock = make_clock(index)
        try:
            FixtureRunner(
                state_directory=attempt_state,
                clock=clock,
                evaluator=selected_evaluator,
            ).run(
                fixture_root=fixture.root,
                model=ScriptedPatchModel(
                    fixture.solution_patch,
                    model_id=campaign.manifest.model.model_id,
                ),
                run_id=attempt.attempt_id,
                code_revision=code_revision,
                experiment_id=campaign.manifest.experiment_id,
                candidate_id=campaign.manifest.candidate_id,
                seed=attempt.seed,
                budget_limits=campaign.manifest.budget_limits,
            )
        except FixtureRunPostCommitMaintenanceError:
            notes[attempt.attempt_id] = _AttemptExecutionNote(
                error_type="FixtureRunPostCommitMaintenanceError",
                post_commit_maintenance_error=True,
                diagnostic="post_commit_maintenance_release_failed",
            )
        except BudgetExceededError:
            _recover_after_execution_error(attempt_state, attempt, clock)
        except Exception as error:
            recovery_attempted, recovery_succeeded = _recover_after_execution_error(
                attempt_state,
                attempt,
                clock,
            )
            notes[attempt.attempt_id] = _AttemptExecutionNote(
                error_type=type(error).__name__,
                recovery_attempted=recovery_attempted,
                recovery_succeeded=recovery_succeeded,
                diagnostic="execution_raised",
            )

        if campaign_fixture_tree_sha256(fixture.root) != fixture.specification.fixture_tree_sha256:
            prior = notes.get(attempt.attempt_id)
            notes[attempt.attempt_id] = _AttemptExecutionNote(
                error_type=(
                    prior.error_type if prior is not None else "CampaignConfigurationError"
                ),
                post_commit_maintenance_error=(
                    prior.post_commit_maintenance_error if prior is not None else False
                ),
                recovery_attempted=prior.recovery_attempted if prior is not None else False,
                recovery_succeeded=prior.recovery_succeeded if prior is not None else False,
                diagnostic="fixture_identity_changed_during_execution",
            )

    return reconcile_reliability_campaign(
        campaign,
        state_directory=state,
        execution_notes=notes,
        git_revision=git_revision,
        recorded_at=recorded_at,
    )


def reconcile_reliability_campaign(
    campaign: LoadedReliabilityCampaign,
    *,
    state_directory: Path,
    execution_notes: dict[str, _AttemptExecutionNote] | None = None,
    git_revision: str,
    recorded_at: datetime | None = None,
) -> ReliabilityCampaignReport:
    """Inspect the full schedule without dispatching or retrying any work."""

    state = Path(os.path.abspath(state_directory))
    expected_manifest_bytes = canonical_json(campaign.manifest).encode("utf-8") + b"\n"
    state_manifest_verified = False
    try:
        state_manifest_bytes = _read_regular_file(
            state / _STATE_MANIFEST_NAME,
            label="campaign state manifest",
            max_bytes=_MANIFEST_MAX_BYTES,
        )
        state_manifest_verified = state_manifest_bytes == expected_manifest_bytes
    except CampaignConfigurationError:
        pass

    notes = execution_notes or {}
    attempts = tuple(
        _reconcile_attempt(
            campaign,
            attempt,
            state / _ATTEMPTS_DIRECTORY_NAME / attempt.attempt_id,
            notes.get(attempt.attempt_id),
        )
        for attempt in campaign.manifest.attempts
    )
    code_identity_verified = (
        campaign_code_source_sha256(campaign.repository_root)
        == campaign.manifest.code_source_sha256
    )
    intended = len(attempts)
    terminal = sum(item.terminal is not None for item in attempts)
    reconciled = sum(item.is_reconciled_terminal for item in attempts)
    expected = sum(item.disposition is CampaignAttemptDisposition.EXPECTED for item in attempts)
    infrastructure_errors = sum(item.is_infrastructure_error for item in attempts)
    rate = infrastructure_errors / intended
    complete = reconciled == intended
    all_expected = expected == intended
    threshold_met = rate <= campaign.manifest.maximum_infrastructure_error_rate
    campaign_passed = (
        state_manifest_verified
        and code_identity_verified
        and complete
        and all_expected
        and threshold_met
    )
    body = ReliabilityCampaignReportBody(
        manifest=campaign.manifest,
        source_manifest_sha256=campaign.source_manifest_sha256,
        campaign_manifest_sha256=campaign.manifest.content_sha256,
        git_revision=git_revision,
        recorded_at=recorded_at or datetime.now(UTC),
        state_manifest_verified=state_manifest_verified,
        code_identity_verified=code_identity_verified,
        attempts=attempts,
        intended_attempt_count=intended,
        terminal_attempt_count=terminal,
        reconciled_attempt_count=reconciled,
        expected_attempt_count=expected,
        infrastructure_error_count=infrastructure_errors,
        infrastructure_error_rate=rate,
        complete=complete,
        all_expected=all_expected,
        threshold_met=threshold_met,
        campaign_passed=campaign_passed,
    )
    return ReliabilityCampaignReport(
        body=body,
        body_sha256=canonical_sha256(body),
    )


def write_reliability_campaign_report(
    report: ReliabilityCampaignReport,
    output_path: Path,
) -> None:
    """Publish one canonical report without replacing any existing path."""

    write_campaign_evidence_file(report.canonical_bytes(), output_path)


def load_reliability_campaign_report(path: Path) -> ReliabilityCampaignReport:
    """Load and validate one bounded canonical report envelope."""

    report_path = _canonical_existing_path(path, label="campaign report")
    report_bytes = _read_regular_file(
        report_path,
        label="campaign report",
        max_bytes=_REPORT_MAX_BYTES,
    )
    raw_report = _strict_json_object(report_bytes, label="campaign report")
    try:
        return ReliabilityCampaignReport.model_validate(raw_report)
    except ValidationError as error:
        raise CampaignEvidenceError(f"campaign report is invalid: {error}") from error


def ensure_campaign_output_available(output_path: Path) -> Path:
    """Resolve an output parent and fail before dispatch if the leaf already exists."""

    lexical = Path(os.path.abspath(output_path))
    if lexical == Path(lexical.anchor):
        raise CampaignEvidenceError("campaign output cannot be a filesystem root")
    parent = _canonical_real_directory(lexical.parent, label="campaign output directory")
    output = parent / lexical.name
    if os.path.lexists(output):
        raise CampaignEvidenceError(f"campaign evidence already exists: {output}")
    return output


def write_campaign_evidence_file(data: bytes, output_path: Path) -> None:
    """Atomically publish bytes through a same-directory no-replace rename."""

    output = ensure_campaign_output_available(output_path)
    payload = data + b"\n"
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(output.parent, parent_flags)
    temporary_name = f".{output.name}.tmp-{uuid.uuid4().hex}"
    temporary_created = False
    try:
        file_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary_name, file_flags, 0o600, dir_fd=parent_descriptor)
        temporary_created = True
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != len(payload)
            ):
                raise CampaignEvidenceError("campaign evidence temporary file is invalid")
        finally:
            os.close(descriptor)
        try:
            rename_noreplace_at(
                parent_descriptor,
                temporary_name,
                parent_descriptor,
                output.name,
            )
        except FileExistsError as error:
            raise CampaignEvidenceError(f"campaign evidence already exists: {output}") from error
        temporary_created = False
        os.fsync(parent_descriptor)
    except CampaignEvidenceError:
        raise
    except OSError as error:
        raise CampaignEvidenceError(f"campaign evidence could not be published: {error}") from error
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        os.close(parent_descriptor)

    observed = _read_regular_file(
        output,
        label="published campaign evidence",
        max_bytes=max(_REPORT_MAX_BYTES, len(data) + 1),
    )
    if observed != payload:
        raise CampaignEvidenceError("published campaign evidence bytes do not match")


def _reconcile_attempt(
    campaign: LoadedReliabilityCampaign,
    attempt: ReliabilityCampaignAttempt,
    state_directory: Path,
    note: _AttemptExecutionNote | None,
) -> ReliabilityCampaignAttemptEvidence:
    fixture = campaign.fixture(attempt.fixture_id).specification
    integrity = audit_storage(state_directory)
    snapshot = integrity.ledger_snapshot
    observed_run_ids = tuple(root.run_id for root in snapshot.roots) if snapshot is not None else ()
    if not integrity.database_present:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_MISSING,
            diagnostic="attempt_database_missing",
        )
    if snapshot is None:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_INVALID,
            diagnostic=integrity.diagnostic or "attempt_ledger_snapshot_missing",
        )
    if observed_run_ids != (attempt.attempt_id,):
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_RUN_SET_INVALID,
            diagnostic="attempt_run_set_does_not_match_schedule",
        )

    database = state_directory / "runs.db"
    try:
        with EventStore.open_existing_read_only(
            database,
            trusted_base=state_directory.parent,
        ) as store:
            manifest = store.load_manifest(attempt.attempt_id)
            events = store.list_events(attempt.attempt_id)
    except (KeyError, OSError, StoreIntegrityError, ValueError) as error:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_INVALID,
            diagnostic=f"attempt_event_store_invalid:{type(error).__name__}",
        )
    if not manifest.status.is_terminal:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_NONTERMINAL,
            diagnostic="attempt_run_is_nonterminal",
        )
    try:
        replay = replay_events(events, require_terminal=True)
    except ReplayIntegrityError as error:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_INVALID,
            diagnostic=f"attempt_replay_invalid:{type(error).__name__}",
        )

    commitment = snapshot.roots[0]
    terminal = ReliabilityCampaignTerminalEvidence(
        run_id=manifest.run_id,
        task_id=manifest.task_id,
        run_status=manifest.status,
        evaluation_outcome=replay.evaluation_outcome,
        manifest_revision=commitment.manifest_revision,
        manifest_sha256=commitment.manifest_sha256,
        event_count=commitment.event_count,
        head_event_sha256=commitment.head_event_sha256,
        semantic_digest=semantic_digest(events),
        ledger_snapshot_sha256=snapshot.snapshot_sha256,
        storage_state=integrity.state.value,
        references_verified=integrity.references_verified,
        storage_clean=integrity.clean,
    )
    expected_code_revision = f"source-sha256:{campaign.manifest.code_source_sha256}"
    identity_matches = (
        manifest.experiment_id == campaign.manifest.experiment_id
        and manifest.task_id == fixture.fixture_id
        and manifest.candidate_id == campaign.manifest.candidate_id
        and manifest.requested_model == campaign.manifest.model.model_id
        and manifest.seed == attempt.seed
        and manifest.environment_digest == campaign.manifest.evaluator.environment_digest
        and manifest.code_revision == expected_code_revision
        and manifest.budget_limits == campaign.manifest.budget_limits
    )
    if not identity_matches or not integrity.references_verified or not integrity.clean:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.EVIDENCE_INVALID,
            terminal=terminal,
            diagnostic=(
                "attempt_identity_mismatch"
                if not identity_matches
                else "attempt_storage_is_not_clean_and_verified"
            ),
        )
    if note is not None and note.post_commit_maintenance_error:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.POST_COMMIT_MAINTENANCE_ERROR,
            terminal=terminal,
            diagnostic=note.diagnostic or "post_commit_maintenance_release_failed",
        )
    if note is not None and note.error_type is not None:
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.INFRASTRUCTURE_ERROR,
            terminal=terminal,
            diagnostic=note.diagnostic or "execution_raised",
        )
    if manifest.status is RunStatus.INFRASTRUCTURE_ERROR or replay.evaluation_outcome == "error":
        return _attempt_evidence(
            attempt,
            observed_run_ids,
            note,
            disposition=CampaignAttemptDisposition.INFRASTRUCTURE_ERROR,
            terminal=terminal,
            diagnostic="attempt_terminalized_as_infrastructure_error",
        )
    expected = (
        manifest.status is fixture.expected_run_status
        and replay.evaluation_outcome == fixture.expected_evaluation_outcome
    )
    return _attempt_evidence(
        attempt,
        observed_run_ids,
        note,
        disposition=(
            CampaignAttemptDisposition.EXPECTED
            if expected
            else CampaignAttemptDisposition.UNEXPECTED_RESULT
        ),
        terminal=terminal,
        diagnostic=None if expected else "attempt_terminal_result_did_not_match_expectation",
    )


def _attempt_evidence(
    attempt: ReliabilityCampaignAttempt,
    observed_run_ids: tuple[str, ...],
    note: _AttemptExecutionNote | None,
    *,
    disposition: CampaignAttemptDisposition,
    diagnostic: str | None,
    terminal: ReliabilityCampaignTerminalEvidence | None = None,
) -> ReliabilityCampaignAttemptEvidence:
    return ReliabilityCampaignAttemptEvidence(
        attempt=attempt,
        disposition=disposition,
        observed_run_ids=observed_run_ids,
        terminal=terminal,
        recovery_attempted=note.recovery_attempted if note is not None else False,
        recovery_succeeded=note.recovery_succeeded if note is not None else False,
        execution_error_type=note.error_type if note is not None else None,
        diagnostic=diagnostic,
    )


def _recover_after_execution_error(
    state_directory: Path,
    attempt: ReliabilityCampaignAttempt,
    clock: Clock,
) -> tuple[bool, bool]:
    try:
        recover_existing_fixture_run(
            state_directory=state_directory,
            run_id=attempt.attempt_id,
            clock=clock,
            terminal_reason="campaign_execution_exception",
        )
    except Exception:
        return True, False
    return True, True


def _require_supported_execution(
    campaign: LoadedReliabilityCampaign,
    evaluator: Evaluator,
) -> None:
    manifest = campaign.manifest
    if manifest.evidence_tier is not CampaignEvidenceTier.DEVELOPMENT:
        raise CampaignConfigurationError(
            "the initial campaign executor supports development evidence only"
        )
    if manifest.evaluator.kind is not CampaignEvaluatorKind.LOCAL:
        raise CampaignConfigurationError(
            "the initial campaign executor supports the local evaluator only"
        )
    if evaluator.evaluator_version != manifest.evaluator.evaluator_version:
        raise CampaignConfigurationError("campaign evaluator version mismatch")
    if evaluator.environment_digest != manifest.evaluator.environment_digest:
        raise CampaignConfigurationError("campaign evaluator environment mismatch")
    if campaign_code_source_sha256(campaign.repository_root) != manifest.code_source_sha256:
        raise CampaignConfigurationError("campaign code identity changed before execution")


def _prepare_new_campaign_state(configured: Path) -> Path:
    lexical = Path(os.path.abspath(configured))
    if lexical == Path(lexical.anchor):
        raise CampaignConfigurationError("campaign state directory cannot be a filesystem root")
    parent = _canonical_real_directory(lexical.parent, label="campaign state parent")
    state = parent / lexical.name
    if os.path.lexists(state):
        raise CampaignConfigurationError(f"campaign state already exists: {state}")
    try:
        os.mkdir(state, mode=0o700)
        _fsync_directory(parent)
        os.mkdir(state / _ATTEMPTS_DIRECTORY_NAME, mode=0o700)
        _fsync_directory(state)
    except OSError as error:
        raise CampaignConfigurationError(f"campaign state could not be created: {error}") from error
    return state


def _strict_json_object(data: bytes, *, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CampaignConfigurationError(f"{label} must be UTF-8 JSON") from error

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise CampaignConfigurationError(f"{label} contains duplicate key {key!r}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise CampaignConfigurationError(f"{label} contains non-finite number {value}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except CampaignConfigurationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise CampaignConfigurationError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise CampaignConfigurationError(f"{label} must be a JSON object")
    return value


def _plain_repository_descendant(repository: Path, value: str) -> Path:
    relative = PurePosixPath(value)
    path = repository.joinpath(*relative.parts)
    root = _canonical_real_directory(path, label=f"campaign fixture {value}")
    if not root.is_relative_to(repository):
        raise CampaignConfigurationError("campaign fixture escapes the repository root")
    return root


def _canonical_existing_path(path: Path, *, label: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CampaignConfigurationError(f"{label} is unavailable: {lexical}") from error
    if resolved != lexical:
        raise CampaignConfigurationError(f"{label} path may not traverse symbolic links")
    return resolved


def _canonical_real_directory(path: Path, *, label: str) -> Path:
    resolved = _canonical_existing_path(path, label=label)
    try:
        metadata = os.lstat(resolved)
    except OSError as error:
        raise CampaignConfigurationError(f"{label} is unavailable: {resolved}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise CampaignConfigurationError(f"{label} must be a real directory")
    return resolved


def _read_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CampaignConfigurationError(f"{label} could not be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
            raise CampaignConfigurationError(f"{label} must be a bounded single-link regular file")
        chunks: list[bytes] = []
        observed = 0
        while chunk := os.read(descriptor, min(1_048_576, max_bytes + 1 - observed)):
            chunks.append(chunk)
            observed += len(chunk)
            if observed > max_bytes:
                raise CampaignConfigurationError(f"{label} exceeds its byte limit")
        after = os.fstat(descriptor)
    except OSError as error:
        raise CampaignConfigurationError(f"{label} could not be read safely") from error
    finally:
        os.close(descriptor)
    try:
        path_after = os.lstat(path)
    except OSError as error:
        raise CampaignConfigurationError(f"{label} changed while it was read") from error
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    identity_path = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_mode,
        path_after.st_nlink,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if identity_before != identity_after or identity_before != identity_path:
        raise CampaignConfigurationError(f"{label} changed while it was read")
    return b"".join(chunks)


def _identity_file(path: Path, *, label: str) -> tuple[bytes, int]:
    data = _read_regular_file(path, label=label, max_bytes=_TREE_MAX_BYTES)
    metadata = os.lstat(path)
    return data, stat.S_IMODE(metadata.st_mode)


def _file_entry(relative_path: str, data: bytes, mode: int) -> dict[str, str | int]:
    try:
        relative_path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CampaignConfigurationError("campaign identity paths must be valid UTF-8") from error
    return {
        "mode": mode,
        "path": relative_path,
        "sha256": sha256_bytes(data),
        "size_bytes": len(data),
    }


def _tree_entries(
    root: Path,
    *,
    relative_to: Path,
    label: str,
    ignore_generated_python: bool,
) -> tuple[dict[str, str | int], ...]:
    root = _canonical_real_directory(root, label=label)
    directory_snapshots: dict[Path, tuple[int, int, int, int]] = {}
    entries: list[dict[str, str | int]] = []
    total_bytes = 0

    def walk_error(error: OSError) -> None:
        raise CampaignConfigurationError(f"{label} could not be enumerated") from error

    for current_text, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=walk_error,
    ):
        current = Path(current_text)
        metadata = os.lstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise CampaignConfigurationError(f"{label} contains a replaced directory")
        directory_snapshots[current] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        directory_names.sort()
        file_names.sort()
        retained_directories: list[str] = []
        for name in directory_names:
            child = current / name
            child_metadata = os.lstat(child)
            if not stat.S_ISDIR(child_metadata.st_mode) or stat.S_ISLNK(child_metadata.st_mode):
                raise CampaignConfigurationError(f"{label} contains a linked or special directory")
            if ignore_generated_python and name == "__pycache__":
                continue
            retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            if ignore_generated_python and name.endswith((".pyc", ".pyo")):
                continue
            path = current / name
            data, mode = _identity_file(path, label=f"{label} file {path}")
            total_bytes += len(data)
            if len(entries) >= _TREE_MAX_FILES or total_bytes > _TREE_MAX_BYTES:
                raise CampaignConfigurationError(f"{label} exceeds its bounded inventory")
            relative_path = path.relative_to(relative_to).as_posix()
            entries.append(_file_entry(relative_path, data, mode))

    for directory, expected in directory_snapshots.items():
        metadata = os.lstat(directory)
        observed = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        if not stat.S_ISDIR(metadata.st_mode) or observed != expected:
            raise CampaignConfigurationError(f"{label} changed while it was enumerated")
    entries.sort(key=lambda entry: str(entry["path"]))
    return tuple(entries)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
