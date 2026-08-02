"""SIGKILL recovery at FixtureRunner's two external-work boundaries.

The child announces entry from inside the model or evaluator test double and then
blocks on the same multiprocessing connection.  The parent therefore kills a real
process only after the preceding SQLite lifecycle phase is durably committed,
without timing guesses or production-only hooks.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sqlite3
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import NoReturn

import pytest

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    RunStatus,
    TaskSpec,
    sha256_bytes,
)
from guildmind.evaluation import LocalEvaluationResult, LocalEvaluationSpec
from guildmind.models import ModelClient, ModelResponse, ScriptedPatchModel
from guildmind.runtime import DeterministicClock, replay_events
from guildmind.runtime.runner import FixtureRunner
from guildmind.storage import EventStore, FileArtifactStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_PATCH = _FIXTURE / "solution.patch"
_START = datetime(2026, 8, 2, 15, 0, tzinfo=UTC)
_MODEL_ID = "guildmind/process-crash-model-v1"
_EVALUATOR_VERSION = "guildmind/process-crash-evaluator-v1"
_ENVIRONMENT_DIGEST = f"sha256:{sha256_bytes(b'process-crash-evaluator-v1')}"
_MAXIMUM = BudgetUsage(
    uncached_input_tokens=64,
    output_tokens=64,
    model_calls=1,
)
_ACTUAL = BudgetUsage(
    uncached_input_tokens=24,
    output_tokens=16,
    model_calls=1,
)
_EMPTY = BudgetUsage()


class _Boundary(StrEnum):
    MODEL_ENTERED = "model_entered"
    EVALUATOR_ENTERED = "evaluator_entered"


_PREFIX_EVENT_TYPES: dict[_Boundary, tuple[str, ...]] = {
    _Boundary.MODEL_ENTERED: (
        "run.created",
        "run.started",
        "artifact.recorded",
        "model.request_started",
        "budget.snapshot",
    ),
    _Boundary.EVALUATOR_ENTERED: (
        "run.created",
        "run.started",
        "artifact.recorded",
        "model.request_started",
        "budget.snapshot",
        "model.response_completed",
        "budget.snapshot",
        "artifact.recorded",
    ),
}


class _BlockingModel:
    def __init__(self, barrier: Connection) -> None:
        self._barrier = barrier

    @property
    def model_id(self) -> str:
        return _MODEL_ID

    @property
    def maximum_usage(self) -> BudgetUsage:
        return _MAXIMUM

    def propose_patch(self, problem_statement: str) -> ModelResponse:
        if not problem_statement.strip():
            raise AssertionError("runner dispatched an empty problem statement")
        _enter_and_block(self._barrier, _Boundary.MODEL_ENTERED)


class _BlockingEvaluator:
    def __init__(self, barrier: Connection) -> None:
        self._barrier = barrier

    @property
    def evaluator_version(self) -> str:
        return _EVALUATOR_VERSION

    @property
    def environment_digest(self) -> str:
        return _ENVIRONMENT_DIGEST

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
        *,
        expected_patch_sha256: str | None = None,
    ) -> LocalEvaluationResult:
        if spec.task_id != "fixture-001-python-addition":
            raise AssertionError(f"unexpected fixture task: {spec.task_id}")
        if expected_patch_sha256 is None:
            raise AssertionError("runner did not bind the patch identity")
        if sha256_bytes(patch_path.read_bytes()) != expected_patch_sha256:
            raise AssertionError("evaluator received bytes that disagree with the bound patch")
        _enter_and_block(self._barrier, _Boundary.EVALUATOR_ENTERED)


def _enter_and_block(barrier: Connection, boundary: _Boundary) -> NoReturn:
    barrier.send(("entered", boundary.value))
    barrier.recv()
    raise AssertionError("the process-crash barrier was unexpectedly released")


def _child_run_to_external_boundary(
    state_directory_text: str,
    boundary_text: str,
    barrier: Connection,
) -> None:
    boundary = _Boundary(boundary_text)
    state_directory = Path(state_directory_text)
    run_id = f"fixture-crash-{boundary.value.replace('_', '-')}"
    evaluator = _BlockingEvaluator(barrier)
    model: ModelClient
    if boundary is _Boundary.MODEL_ENTERED:
        model = _BlockingModel(barrier)
    else:
        model = ScriptedPatchModel(
            _PATCH,
            model_id=_MODEL_ID,
            maximum_usage=_MAXIMUM,
            actual_usage=_ACTUAL,
        )

    try:
        FixtureRunner(
            state_directory=state_directory,
            clock=DeterministicClock(started_at=_START),
            evaluator=evaluator,
        ).run(
            fixture_root=_FIXTURE,
            model=model,
            run_id=run_id,
            code_revision="process-crash-test",
            budget_limits=BudgetLimits(
                max_total_tokens=_MAXIMUM.total_tokens,
                max_model_calls=1,
                max_model_retries=0,
                max_tool_calls=0,
            ),
        )
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(
                (
                    "error",
                    boundary.value,
                    type(error).__name__,
                    str(error),
                )
            )
        raise
    else:
        barrier.send(("error", boundary.value, "UnexpectedReturn", "runner completed"))
    finally:
        barrier.close()


def _kill_after_external_entry(state_directory: Path, boundary: _Boundary) -> None:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_run_to_external_boundary,
        args=(str(state_directory), boundary.value, child_barrier),
        name=f"guildmind-fixture-crash-{boundary.value}",
    )
    process.start()
    child_barrier.close()
    try:
        ready = wait((parent_barrier, process.sentinel), timeout=30)
        if parent_barrier not in ready:
            process.join(timeout=1)
            pytest.fail(
                "child exited before the external-work barrier: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        try:
            message = parent_barrier.recv()
        except EOFError:
            process.join(timeout=1)
            pytest.fail(
                "child closed the barrier before announcing external work: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        if message != ("entered", boundary.value):
            process.join(timeout=5)
            pytest.fail(f"child failed before the expected barrier: {message!r}")

        pid = process.pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        parent_barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _assert_sqlite_integrity(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_all_bound_bytes(
    artifact_root: Path,
    manifest_artifacts: Mapping[str, ArtifactRef],
) -> None:
    # Kept as a separate helper so the test verifies both manifest-bound evidence and
    # the worker-visible inputs transitively bound by the persisted TaskSpec.
    store = FileArtifactStore(artifact_root)
    for reference in manifest_artifacts.values():
        store.verify(reference)
        data = store.get_bytes(reference)
        assert len(data) == reference.size_bytes

    task_reference = manifest_artifacts["task_spec"]
    task = TaskSpec.model_validate_json(store.get_bytes(task_reference))
    task_inputs = (task.problem_statement, task.repository_snapshot, *task.visible_tests)
    for reference in task_inputs:
        store.verify(reference)
        assert len(store.get_bytes(reference)) == reference.size_bytes


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX SIGKILL")
@pytest.mark.parametrize(
    "boundary",
    (_Boundary.MODEL_ENTERED, _Boundary.EVALUATOR_ENTERED),
    ids=("model-entered", "evaluator-entered"),
)
def test_fixture_runner_recovers_real_process_crashes_at_external_boundaries(
    tmp_path: Path,
    boundary: _Boundary,
) -> None:
    state_directory = tmp_path / boundary.value
    database = state_directory / "runs.db"
    artifact_root = state_directory / "artifacts"
    run_id = f"fixture-crash-{boundary.value.replace('_', '-')}"

    _kill_after_external_entry(state_directory, boundary)
    _assert_sqlite_integrity(database)

    with EventStore(database) as store:
        prefix = store.list_events(run_id)
        manifest_before = store.load_manifest(run_id)
        used_before, reserved_before = store.load_budget_state(run_id)

    assert tuple(event.event_type for event in prefix) == _PREFIX_EVENT_TYPES[boundary]
    assert [event.sequence for event in prefix] == list(range(len(prefix)))
    prefix_state = replay_events(prefix)
    assert manifest_before.status is RunStatus.RUNNING
    assert sum(event.event_type == "run.terminal" for event in prefix) == 0

    recovery_runner = FixtureRunner(
        state_directory=state_directory,
        clock=DeterministicClock(started_at=_START + timedelta(hours=1)),
    )
    recovered = recovery_runner.recover(run_id)
    with EventStore(database) as store:
        events_after_first_recovery = store.list_events(run_id)
        used_after, reserved_after = store.load_budget_state(run_id)

    recovered_again = recovery_runner.recover(run_id)
    with EventStore(database) as store:
        final_events = store.list_events(run_id)
        final_manifest = store.load_manifest(run_id)

    assert recovered_again == recovered == final_manifest
    assert final_events == events_after_first_recovery
    assert sum(event.event_type == "run.terminal" for event in final_events) == 1
    final_state = replay_events(final_events, require_terminal=True)
    assert replay_events(final_events, require_terminal=True) == final_state
    assert final_state.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
    assert reserved_after == _EMPTY

    absent_names = [
        event.payload["name"]
        for event in final_events
        if event.event_type == "artifact.not_produced"
    ]
    if boundary is _Boundary.MODEL_ENTERED:
        assert prefix_state.model_request_state == "started"
        assert prefix_state.artifacts.keys() == {"task_spec"}
        assert used_before == _EMPTY
        assert reserved_before == _MAXIMUM
        assert used_after == _MAXIMUM
        assert recovered.terminal_reason == "ambiguous_model_request"
        assert final_state.model_request_state == "ambiguous"
        assert absent_names == [
            "evaluation",
            "evaluation_stderr",
            "evaluation_stdout",
            "patch",
        ]
        assert tuple(final_state.artifacts) == ("task_spec",)
    else:
        assert prefix_state.model_request_state == "completed"
        assert prefix_state.artifacts.keys() == {"patch", "task_spec"}
        assert used_before == _ACTUAL
        assert reserved_before == _EMPTY
        assert used_after == _ACTUAL
        assert recovered.terminal_reason == "interrupted_run_recovered"
        assert final_state.model_request_state == "completed"
        assert absent_names == [
            "evaluation",
            "evaluation_stderr",
            "evaluation_stdout",
        ]
        assert set(final_state.artifacts) == {"patch", "task_spec"}
        patch_reference = recovered.artifacts["patch"]
        assert FileArtifactStore(artifact_root).get_bytes(patch_reference) == _PATCH.read_bytes()

    assert set(final_state.absent_artifacts.values()) == {"interrupted"}
    assert final_state.evaluation_outcome is None
    assert final_state.budget_used == used_after
    assert final_state.budget_reserved == _EMPTY
    assert final_state.artifacts == {
        name: reference.sha256 for name, reference in recovered.artifacts.items()
    }
    _assert_all_bound_bytes(artifact_root, recovered.artifacts)
    _assert_sqlite_integrity(database)
