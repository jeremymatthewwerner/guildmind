"""The first complete deterministic fixture-to-evidence execution path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    EvaluationResult,
    EventRecord,
    RunManifest,
    RunStatus,
    TaskSpec,
    canonical_json,
)
from guildmind.evaluation import EvaluationStatus, LocalEvaluator
from guildmind.models import ModelClient
from guildmind.runtime.budget import BudgetAuthority
from guildmind.runtime.clock import Clock, SystemClock
from guildmind.runtime.fixture import materialize_fixture_task
from guildmind.runtime.replay import ReplayState, replay_events, semantic_digest
from guildmind.storage import EventStore, FileArtifactStore


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


class FixtureRunner:
    """Run a trusted repository fixture through the local engineering adapter."""

    def __init__(
        self,
        *,
        state_directory: Path,
        clock: Clock | None = None,
        evaluator: LocalEvaluator | None = None,
    ) -> None:
        self.state_directory = state_directory.resolve()
        self.state_directory.mkdir(parents=True, exist_ok=True)
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
        artifact_root = self.state_directory / "artifacts"
        database_path = self.state_directory / "runs.db"
        artifact_store = FileArtifactStore(artifact_root)
        task, local_spec, problem_statement = materialize_fixture_task(
            fixture_root.resolve(), artifact_store
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

        with EventStore(database_path, clock=self.clock) as event_store:
            event_store.create_run(pending)
            started_at = self.clock.stamp().occurred_at
            running = _transition_manifest(
                pending,
                status=RunStatus.RUNNING,
                started_at=started_at,
            )
            event_store.append_event(
                run_id=run_id,
                event_type="run.started",
                payload={"requested_model": model.model_id},
                manifest=running,
            )
            _record_artifact(event_store, run_id, "task_spec", task_artifact)

            reservation_id = "model-request-0001"
            authority.reserve(reservation_id, model.maximum_usage)
            _record_budget_snapshot(event_store, run_id, authority)
            response = model.propose_patch(problem_statement)
            patch_artifact = artifact_store.put_bytes(
                response.patch,
                media_type="text/x-diff; charset=utf-8",
            )
            _record_artifact(event_store, run_id, "patch", patch_artifact)
            authority.reconcile(reservation_id, response.usage)
            _record_budget_snapshot(event_store, run_id, authority)

            local_result = self.evaluator.evaluate(
                local_spec,
                artifact_store.path_for(patch_artifact),
            )
            terminal_status = _terminal_status(local_result.status)
            evaluated_at = self.clock.stamp().occurred_at
            evaluation = local_result.to_domain_result(
                task,
                evaluation_id=f"{run_id}:evaluation:0001",
                run_id=run_id,
                run_status=terminal_status,
                evaluator_version="guildmind/local-fixture-v1",
                patch_hash=patch_artifact.sha256,
                evaluated_at=evaluated_at,
            )
            stdout_artifact = artifact_store.put_text(local_result.stdout)
            stderr_artifact = artifact_store.put_text(local_result.stderr)
            evaluation = EvaluationResult.model_validate(
                {
                    **evaluation.model_dump(),
                    "evidence": (stdout_artifact, stderr_artifact),
                }
            )
            evaluation_artifact = artifact_store.put_text(
                canonical_json(evaluation),
                media_type="application/vnd.guildmind.evaluation+json",
            )
            _record_artifact(event_store, run_id, "evaluation_stdout", stdout_artifact)
            _record_artifact(event_store, run_id, "evaluation_stderr", stderr_artifact)
            _record_artifact(event_store, run_id, "evaluation", evaluation_artifact)
            event_store.append_event(
                run_id=run_id,
                event_type="evaluation.completed",
                payload={
                    "outcome": evaluation.outcome,
                    "result_sha256": evaluation.result_sha256,
                    "score": evaluation.score,
                    "status": local_result.status.value,
                },
            )

            finished_at = self.clock.stamp().occurred_at
            terminal_reason = None
            if terminal_status is not RunStatus.SUCCEEDED:
                terminal_reason = local_result.status.value
            final_manifest = _transition_manifest(
                running,
                returned_model=response.returned_model,
                status=terminal_status,
                finished_at=finished_at,
                terminal_reason=terminal_reason,
                artifacts={
                    "evaluation": evaluation_artifact,
                    "evaluation_stderr": stderr_artifact,
                    "evaluation_stdout": stdout_artifact,
                    "patch": patch_artifact,
                    "task_spec": task_artifact,
                },
            )
            event_store.append_event(
                run_id=run_id,
                event_type="run.terminal",
                payload={
                    "evaluation_outcome": evaluation.outcome,
                    "status": terminal_status.value,
                },
                manifest=final_manifest,
                budget_used=authority.used,
                budget_reserved=authority.reserved,
            )
            events = event_store.list_events(run_id)

        replay = replay_events(events)
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


def _transition_manifest(manifest: RunManifest, **changes: object) -> RunManifest:
    return RunManifest.model_validate({**manifest.model_dump(), **changes})


def _record_artifact(
    event_store: EventStore,
    run_id: str,
    name: str,
    artifact: ArtifactRef,
) -> None:
    event_store.append_event(
        run_id=run_id,
        event_type="artifact.recorded",
        payload={
            "media_type": artifact.media_type,
            "name": name,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        },
    )


def _record_budget_snapshot(
    event_store: EventStore,
    run_id: str,
    authority: BudgetAuthority,
) -> None:
    event_store.append_event(
        run_id=run_id,
        event_type="budget.snapshot",
        payload={
            "used": authority.used.model_dump(mode="json"),
            "reserved": authority.reserved.model_dump(mode="json"),
        },
        budget_used=authority.used,
        budget_reserved=authority.reserved,
    )


def _terminal_status(status: EvaluationStatus) -> RunStatus:
    if status is EvaluationStatus.TIMED_OUT:
        return RunStatus.TIMED_OUT
    if status is EvaluationStatus.INFRASTRUCTURE_ERROR:
        return RunStatus.INFRASTRUCTURE_ERROR
    return RunStatus.SUCCEEDED
