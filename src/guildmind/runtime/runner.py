"""The first complete deterministic fixture-to-evidence execution path."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from guildmind.domain import (
    BudgetLimits,
    EvaluationResult,
    EventRecord,
    RunManifest,
    RunStatus,
    TaskSpec,
    canonical_json,
)
from guildmind.evaluation import EvaluationStatus, Evaluator, LocalEvaluator
from guildmind.models import ModelClient
from guildmind.runtime.budget import BudgetAuthority, BudgetExceededError
from guildmind.runtime.clock import Clock, SystemClock
from guildmind.runtime.fixture import materialize_fixture_task
from guildmind.runtime.recovery import (
    recover_existing_fixture_run,
    terminalize_existing_fixture_budget_refusal,
)
from guildmind.runtime.replay import ReplayState, replay_events, semantic_digest
from guildmind.storage import (
    ArtifactCorruptionError,
    EventStore,
    FileArtifactStore,
    MaintenanceIntegrityError,
    MaintenanceLease,
)


@dataclass(frozen=True, slots=True)
class FixtureRunResult:
    task: TaskSpec
    manifest: RunManifest
    evaluation: EvaluationResult
    events: tuple[EventRecord, ...]
    replay: ReplayState
    semantic_digest: str
    database_path: Path
    artifact_root: Path


class FixtureRunPostCommitMaintenanceError(RuntimeError):
    """Raised when evidence committed but the outer maintenance lease closed unsafely."""

    def __init__(
        self,
        result: FixtureRunResult,
        release_error: MaintenanceIntegrityError,
    ) -> None:
        self.result = result
        self.release_error = release_error
        super().__init__(
            f"fixture run {result.manifest.run_id!r} committed, "
            "but maintenance lease release failed"
        )


class FixtureRunner:
    """Run a repository fixture through a bounded engineering evaluator."""

    def __init__(
        self,
        *,
        state_directory: Path,
        clock: Clock | None = None,
        evaluator: Evaluator | None = None,
    ) -> None:
        self.state_directory = _prepare_state_directory(state_directory)
        self._state_directory_identity = _directory_identity(self.state_directory)
        self.clock = clock or SystemClock()
        self.evaluator = evaluator or LocalEvaluator()

    def run(
        self,
        *,
        fixture_root: Path,
        model: ModelClient,
        run_id: str,
        code_revision: str,
        experiment_id: str = "experiment-0001",
        candidate_id: str = "scripted-solo-v0",
        seed: int = 0,
        budget_limits: BudgetLimits | None = None,
    ) -> FixtureRunResult:
        """Run one fixture while excluding state-wide maintenance.

        The shared lease spans the first CAS publication through the final SQLite
        binding transaction. Exception recovery nests the same process-local shared
        lease safely, so no gap opens before terminalization.
        """

        self._verify_state_directory_identity()
        lease = MaintenanceLease.acquire_shared(self.state_directory)
        try:
            result = self._run_with_shared_lease(
                fixture_root=fixture_root,
                model=model,
                run_id=run_id,
                code_revision=code_revision,
                experiment_id=experiment_id,
                candidate_id=candidate_id,
                seed=seed,
                budget_limits=budget_limits,
            )
        except BaseException as error:
            try:
                lease.close()
            except BaseException as release_error:
                error.add_note(f"maintenance lease release also failed: {release_error!r}")
            raise
        try:
            lease.close()
        except MaintenanceIntegrityError as error:
            raise FixtureRunPostCommitMaintenanceError(result, error) from error
        return result

    def _run_with_shared_lease(
        self,
        *,
        fixture_root: Path,
        model: ModelClient,
        run_id: str,
        code_revision: str,
        experiment_id: str,
        candidate_id: str,
        seed: int,
        budget_limits: BudgetLimits | None,
    ) -> FixtureRunResult:
        self._verify_state_directory_identity()
        artifact_root = self.state_directory / "artifacts"
        database_path = self.state_directory / "runs.db"
        artifact_store = FileArtifactStore(
            artifact_root,
            trusted_base=self.state_directory.parent,
        )
        task, local_spec, problem_statement = materialize_fixture_task(
            fixture_root.resolve(),
            artifact_store,
            evaluator_version=self.evaluator.evaluator_version,
            environment_digest=self.evaluator.environment_digest,
        )
        task_artifact = artifact_store.put_text(
            canonical_json(task), media_type="application/vnd.guildmind.task+json"
        )
        limits = budget_limits or BudgetLimits(
            max_total_tokens=128,
            max_model_calls=1,
            max_model_retries=0,
            max_tool_calls=0,
        )
        authority = BudgetAuthority(limits)
        created_at = self.clock.stamp().occurred_at
        pending = RunManifest(
            run_id=run_id,
            experiment_id=experiment_id,
            task_id=task.task_id,
            candidate_id=candidate_id,
            requested_model=model.model_id,
            seed=seed,
            environment_digest=task.image_digest,
            code_revision=code_revision,
            budget_limits=limits,
            created_at=created_at,
        )

        run_created = False
        reservation_refused = False
        try:
            with EventStore(database_path, clock=self.clock) as event_store:
                event_store.create_run(pending)
                run_created = True
                started_at = self.clock.stamp().occurred_at
                event_store.start_run(
                    run_id,
                    started_at=started_at,
                    requested_model=model.model_id,
                )
                event_store.record_artifact(run_id, "task_spec", task_artifact)

                reservation_id = "model-request-0001"
                maximum_usage = model.maximum_usage
                try:
                    authority.reserve(reservation_id, maximum_usage)
                except BudgetExceededError:
                    reservation_refused = True
                    raise
                event_store.start_model_request(
                    run_id=run_id,
                    request_id=reservation_id,
                    maximum=maximum_usage,
                    budget_used=authority.used,
                    budget_reserved=authority.reserved,
                )
                response = model.propose_patch(problem_statement)
                patch_artifact = artifact_store.put_bytes(
                    response.patch,
                    media_type="text/x-diff; charset=utf-8",
                )
                authority.reconcile(reservation_id, response.usage)
                event_store.complete_model_response(
                    run_id=run_id,
                    request_id=reservation_id,
                    returned_model=response.returned_model,
                    actual_usage=response.usage,
                    patch=patch_artifact,
                    budget_used=authority.used,
                    budget_reserved=authority.reserved,
                )

                artifact_store.verify(patch_artifact)
                local_result = self.evaluator.evaluate(
                    local_spec,
                    artifact_store.path_for(patch_artifact),
                    expected_patch_sha256=patch_artifact.sha256,
                )
                terminal_status = _terminal_status(local_result.status)
                evaluated_at = self.clock.stamp().occurred_at
                evaluation = local_result.to_domain_result(
                    task,
                    evaluation_id=f"{run_id}:evaluation:0001",
                    run_id=run_id,
                    run_status=terminal_status,
                    evaluator_version=self.evaluator.evaluator_version,
                    patch_hash=patch_artifact.sha256,
                    evaluated_at=evaluated_at,
                )
                stdout_artifact = artifact_store.put_text(local_result.stdout)
                stderr_artifact = artifact_store.put_text(local_result.stderr)
                evidence = [stdout_artifact, stderr_artifact]
                evaluation_artifacts = {
                    "evaluation_stderr": stderr_artifact,
                    "evaluation_stdout": stdout_artifact,
                }
                if local_result.raw_candidate_stdout is not None:
                    candidate_stdout_artifact = artifact_store.put_bytes(
                        local_result.raw_candidate_stdout,
                        media_type="application/octet-stream",
                    )
                    evidence.append(candidate_stdout_artifact)
                    evaluation_artifacts["evaluation_candidate_stdout"] = candidate_stdout_artifact
                if local_result.raw_scorer_stdout is not None:
                    scorer_stdout_artifact = artifact_store.put_bytes(
                        local_result.raw_scorer_stdout,
                        media_type="application/octet-stream",
                    )
                    evidence.append(scorer_stdout_artifact)
                    evaluation_artifacts["evaluation_scorer_stdout"] = scorer_stdout_artifact
                evaluation = EvaluationResult.model_validate(
                    {
                        **evaluation.model_dump(),
                        "evidence": tuple(evidence),
                    }
                )
                evaluation_artifact = artifact_store.put_text(
                    canonical_json(evaluation),
                    media_type="application/vnd.guildmind.evaluation+json",
                )
                evaluation_artifacts["evaluation"] = evaluation_artifact

                finished_at = self.clock.stamp().occurred_at
                terminal_reason = None
                if terminal_status is not RunStatus.SUCCEEDED:
                    terminal_reason = local_result.status.value
                terminal = event_store.complete_evaluation_with_events(
                    run_id=run_id,
                    artifacts=evaluation_artifacts,
                    evaluation_payload={
                        "outcome": evaluation.outcome,
                        "result_sha256": evaluation.result_sha256,
                        "score": evaluation.score,
                        "status": local_result.status.value,
                    },
                    status=terminal_status,
                    finished_at=finished_at,
                    terminal_reason=terminal_reason,
                    budget_used=authority.used,
                    budget_reserved=authority.reserved,
                )
                final_manifest = terminal.manifest
                events = list(terminal.events)
        except BudgetExceededError as error:
            try:
                if reservation_refused:
                    terminalize_existing_fixture_budget_refusal(
                        state_directory=self.state_directory,
                        run_id=run_id,
                        clock=self.clock,
                    )
                elif run_created:
                    recover_existing_fixture_run(
                        state_directory=self.state_directory,
                        run_id=run_id,
                        clock=self.clock,
                        terminal_reason="runner_exception",
                    )
            except BaseException as terminalization_error:
                action = "budget terminalization" if reservation_refused else "run recovery"
                error.add_note(f"{action} also failed: {terminalization_error!r}")
            raise
        except BaseException as error:
            if run_created:
                try:
                    recover_existing_fixture_run(
                        state_directory=self.state_directory,
                        run_id=run_id,
                        clock=self.clock,
                        terminal_reason="runner_exception",
                    )
                except BaseException as recovery_error:
                    error.add_note(f"run recovery also failed: {recovery_error!r}")
            raise

        replay = replay_events(events, require_terminal=True)
        return FixtureRunResult(
            task=task,
            manifest=final_manifest,
            evaluation=evaluation,
            events=tuple(events),
            replay=replay,
            semantic_digest=semantic_digest(events),
            database_path=database_path,
            artifact_root=artifact_root,
        )

    def _verify_state_directory_identity(self) -> None:
        if _directory_identity(self.state_directory) != self._state_directory_identity:
            raise ArtifactCorruptionError(
                f"state directory {self.state_directory} changed after runner initialization"
            )


def _terminal_status(status: EvaluationStatus) -> RunStatus:
    if status is EvaluationStatus.PASSED:
        return RunStatus.SUCCEEDED
    if status is EvaluationStatus.TIMED_OUT:
        return RunStatus.TIMED_OUT
    if status is EvaluationStatus.INFRASTRUCTURE_ERROR:
        return RunStatus.INFRASTRUCTURE_ERROR
    return RunStatus.FAILED


def _prepare_state_directory(configured: Path) -> Path:
    """Create the configured directory below its trusted immediate parent."""

    lexical = Path(os.path.abspath(configured))
    if lexical == Path(lexical.anchor):
        raise ValueError("state directory cannot be a filesystem root")
    physical = lexical.parent.resolve(strict=False) / lexical.name
    try:
        physical.mkdir(parents=True)
    except FileExistsError:
        pass
    except OSError as error:
        raise ArtifactCorruptionError(
            f"state directory {physical} could not be created safely"
        ) from error
    try:
        metadata = os.lstat(physical)
    except OSError as error:
        raise ArtifactCorruptionError(
            f"state directory {physical} could not be inspected"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactCorruptionError(f"state directory {physical} is not a real directory")
    return physical


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as error:
        raise ArtifactCorruptionError(f"state directory {path} could not be inspected") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ArtifactCorruptionError(f"state directory {path} is not a real directory")
    return metadata.st_dev, metadata.st_ino
