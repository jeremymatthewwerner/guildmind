from datetime import UTC, datetime
from pathlib import Path

import pytest

from guildmind.domain import (
    BudgetLimits,
    BudgetUsage,
    EvaluationResult,
    RunStatus,
    TaskSpec,
)
from guildmind.evaluation import LocalEvaluator
from guildmind.models import ModelResponse, ScriptedPatchModel
from guildmind.runtime import BudgetExceededError, DeterministicClock, replay_events
from guildmind.runtime.runner import FixtureRunner, FixtureRunResult
from guildmind.storage import EventStore, FileArtifactStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_START = datetime(2026, 7, 31, tzinfo=UTC)


class RaisingModel:
    def __init__(self) -> None:
        self._maximum_usage = BudgetUsage(
            uncached_input_tokens=32,
            output_tokens=48,
            model_calls=1,
        )

    @property
    def model_id(self) -> str:
        return "guildmind/fake-raising-model-v1"

    @property
    def maximum_usage(self) -> BudgetUsage:
        return self._maximum_usage

    def propose_patch(self, problem_statement: str) -> ModelResponse:
        raise RuntimeError("simulated provider failure after dispatch")


class IdentifiedLocalEvaluator(LocalEvaluator):
    @property
    def evaluator_version(self) -> str:
        return "guildmind/test-identified-evaluator-v1"

    @property
    def environment_digest(self) -> str:
        return f"sha256:{'d' * 64}"


def run_fixture(state_directory: Path, run_id: str, *, day_offset: int = 0) -> FixtureRunResult:
    clock = DeterministicClock(
        started_at=_START.replace(day=1 + day_offset),
    )
    return FixtureRunner(state_directory=state_directory, clock=clock).run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id=run_id,
        code_revision="test-revision",
    )


def test_fixture_runner_emits_replayable_content_verified_evidence(tmp_path: Path) -> None:
    result = run_fixture(tmp_path / "state", "run-001")

    assert result.manifest.status is RunStatus.SUCCEEDED
    assert result.evaluation.outcome == "passed"
    assert result.replay.status is RunStatus.SUCCEEDED
    assert result.replay.evaluation_outcome == "passed"
    assert result.replay.budget_used.model_calls == 1
    assert result.replay.budget_reserved.model_calls == 0
    assert result.replay.artifacts["patch"] == result.manifest.artifacts["patch"].sha256

    artifacts = FileArtifactStore(result.artifact_root)
    for reference in result.manifest.artifacts.values():
        artifacts.verify(reference)
    persisted_task = TaskSpec.model_validate_json(
        artifacts.get_bytes(result.manifest.artifacts["task_spec"])
    )
    persisted_evaluation = EvaluationResult.model_validate_json(
        artifacts.get_bytes(result.manifest.artifacts["evaluation"])
    )
    assert persisted_task == result.task
    assert persisted_evaluation == result.evaluation
    assert persisted_evaluation.run_id == result.manifest.run_id
    assert persisted_evaluation.run_status is result.manifest.status
    assert persisted_evaluation.task_hash == result.task.task_content_hash
    assert persisted_evaluation.patch_hash == result.manifest.artifacts["patch"].sha256
    assert persisted_evaluation.evidence == (
        result.manifest.artifacts["evaluation_stdout"],
        result.manifest.artifacts["evaluation_stderr"],
    )
    evaluation_event = next(
        event for event in result.events if event.event_type == "evaluation.completed"
    )
    assert evaluation_event.payload["result_sha256"] == persisted_evaluation.result_sha256

    with EventStore(result.database_path) as store:
        assert store.load_manifest("run-001") == result.manifest
        assert tuple(store.list_events("run-001")) == result.events


def test_fixture_runner_has_stable_semantic_digest_across_identity_and_time(tmp_path: Path) -> None:
    first = run_fixture(tmp_path / "first", "run-a")
    second = run_fixture(tmp_path / "second", "run-b", day_offset=1)

    assert first.semantic_digest == second.semantic_digest
    assert first.events != second.events


def test_fixture_runner_records_the_injected_evaluator_identity(tmp_path: Path) -> None:
    runner = FixtureRunner(
        state_directory=tmp_path / "state",
        clock=DeterministicClock(started_at=_START),
        evaluator=IdentifiedLocalEvaluator(),
    )

    result = runner.run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id="run-identified-evaluator",
        code_revision="test-revision",
    )

    assert result.task.image_digest == f"sha256:{'d' * 64}"
    assert result.task.metadata["evaluator_version"] == ("guildmind/test-identified-evaluator-v1")
    assert result.manifest.environment_digest == result.task.image_digest
    assert result.evaluation.evaluator_version == "guildmind/test-identified-evaluator-v1"


def test_raising_model_is_terminalized_as_ambiguous_and_recovery_is_idempotent(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    clock = DeterministicClock(started_at=_START)
    runner = FixtureRunner(state_directory=state_directory, clock=clock)
    model = RaisingModel()

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        runner.run(
            fixture_root=_FIXTURE,
            model=model,
            run_id="run-ambiguous",
            code_revision="test-revision",
        )

    database = state_directory / "runs.db"
    with EventStore(database) as store:
        manifest = store.load_manifest("run-ambiguous")
        used, reserved = store.load_budget_state("run-ambiguous")
        events_before = store.list_events("run-ambiguous")

    replay = replay_events(events_before, require_terminal=True)
    assert manifest.status is RunStatus.INFRASTRUCTURE_ERROR
    assert manifest.terminal_reason == "ambiguous_model_request"
    assert used == model.maximum_usage
    assert reserved == BudgetUsage()
    assert replay.model_request_state == "ambiguous"
    assert replay.artifacts.keys() == {"task_spec"}
    assert set(replay.absent_artifacts) == {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
    }
    assert "model.response_completed" not in {event.event_type for event in events_before}
    assert "evaluation.completed" not in {event.event_type for event in events_before}

    assert runner.recover("run-ambiguous") == manifest
    assert runner.recover("run-ambiguous") == manifest
    with EventStore(database) as store:
        assert store.list_events("run-ambiguous") == events_before


def test_budget_refusal_is_terminalized_without_dispatch_or_infrastructure_bias(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / "state"
    runner = FixtureRunner(
        state_directory=state_directory,
        clock=DeterministicClock(started_at=_START),
    )

    with pytest.raises(BudgetExceededError):
        runner.run(
            fixture_root=_FIXTURE,
            model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
            run_id="run-budget-refused",
            code_revision="test-revision",
            budget_limits=BudgetLimits(max_model_calls=0, max_total_tokens=1),
        )

    with EventStore(state_directory / "runs.db") as store:
        manifest = store.load_manifest("run-budget-refused")
        used, reserved = store.load_budget_state("run-budget-refused")
        events = store.list_events("run-budget-refused")

    state = replay_events(events, require_terminal=True)
    assert manifest.status is RunStatus.BUDGET_EXHAUSTED
    assert manifest.terminal_reason == "model_reservation_refused"
    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert used == BudgetUsage()
    assert reserved == BudgetUsage()
    assert "model.request_started" not in {event.event_type for event in events}
    assert state.artifacts.keys() == {"task_spec"}
    assert set(state.absent_artifacts) == {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
    }
