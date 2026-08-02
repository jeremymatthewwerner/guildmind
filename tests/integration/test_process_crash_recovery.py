"""Real-process crash recovery around public durable lifecycle boundaries.

Each child uses the spawn start method, commits one public ``EventStore`` phase,
signals its parent over a pipe, and blocks on that pipe.  The parent sends SIGKILL
only after receiving the phase barrier, so no wall-clock sleep is used to guess when
the durable prefix exists.

The post-commit cases prove the five material externally observable prefixes used by
the runner.  A second
matrix wraps the already-initialized SQLite connection *inside this test process*
and blocks its selected ``commit()`` call.  Those cases send SIGKILL after all phase
SQL has executed but before COMMIT, without adding a production hook.  Restart must
observe the exact previous phase and recovery must never infer a result from an
unreferenced content-addressed blob.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import cast

import pytest

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EventRecord,
    RunManifest,
    RunStatus,
    sha256_bytes,
)
from guildmind.runtime import DeterministicClock, replay_events
from guildmind.storage import EventStore, FileArtifactStore

_START = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
_MAXIMUM = BudgetUsage(output_tokens=20, model_calls=1)
_ACTUAL = BudgetUsage(output_tokens=8, model_calls=1)
_EMPTY = BudgetUsage()
_REQUEST_ID = "model-request-0001"
_TASK_BYTES = b'{"schema_version":"guildmind.crash-task/v1"}\n'
_PATCH_BYTES = b"diff --git a/example.py b/example.py\n"
_EVALUATION_STDOUT_BYTES = b"evaluation stdout\n"
_EVALUATION_STDERR_BYTES = b""
_EVALUATION_BYTES = b'{"outcome":"passed","schema_version":"guildmind.crash-evaluation/v1"}\n'


class _CrashPoint(StrEnum):
    RUN_CREATED = "run_created"
    TASK_BOUND = "task_bound"
    MODEL_REQUEST_OUTSTANDING = "model_request_outstanding"
    MODEL_RESPONSE_COMPLETED = "model_response_completed"
    EVALUATION_COMPLETED = "evaluation_completed"


class _PreCommitPoint(StrEnum):
    MODEL_RESPONSE = "model_response"
    EVALUATION = "evaluation"
    RECOVERY_OUTSTANDING = "recovery_outstanding"
    RECOVERY_RESPONSE = "recovery_response"


_PREFIX_EVENT_TYPES: dict[_CrashPoint, tuple[str, ...]] = {
    _CrashPoint.RUN_CREATED: ("run.created",),
    _CrashPoint.TASK_BOUND: (
        "run.created",
        "run.started",
        "artifact.recorded",
    ),
    _CrashPoint.MODEL_REQUEST_OUTSTANDING: (
        "run.created",
        "run.started",
        "artifact.recorded",
        "model.request_started",
        "budget.snapshot",
    ),
    _CrashPoint.MODEL_RESPONSE_COMPLETED: (
        "run.created",
        "run.started",
        "artifact.recorded",
        "model.request_started",
        "budget.snapshot",
        "model.response_completed",
        "budget.snapshot",
        "artifact.recorded",
    ),
    _CrashPoint.EVALUATION_COMPLETED: (
        "run.created",
        "run.started",
        "artifact.recorded",
        "model.request_started",
        "budget.snapshot",
        "model.response_completed",
        "budget.snapshot",
        "artifact.recorded",
        "artifact.recorded",
        "artifact.recorded",
        "artifact.recorded",
        "evaluation.completed",
        "budget.snapshot",
        "run.terminal",
    ),
}

_EXPECTED_BOUND_ARTIFACTS: dict[_CrashPoint, tuple[str, ...]] = {
    _CrashPoint.RUN_CREATED: (),
    _CrashPoint.TASK_BOUND: ("task_spec",),
    _CrashPoint.MODEL_REQUEST_OUTSTANDING: ("task_spec",),
    _CrashPoint.MODEL_RESPONSE_COMPLETED: ("patch", "task_spec"),
    _CrashPoint.EVALUATION_COMPLETED: (
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
        "task_spec",
    ),
}

_PRECOMMIT_PREFIX: dict[_PreCommitPoint, _CrashPoint] = {
    _PreCommitPoint.MODEL_RESPONSE: _CrashPoint.MODEL_REQUEST_OUTSTANDING,
    _PreCommitPoint.EVALUATION: _CrashPoint.MODEL_RESPONSE_COMPLETED,
    _PreCommitPoint.RECOVERY_OUTSTANDING: _CrashPoint.MODEL_REQUEST_OUTSTANDING,
    _PreCommitPoint.RECOVERY_RESPONSE: _CrashPoint.MODEL_RESPONSE_COMPLETED,
}

_PRECOMMIT_ORPHANS: dict[_PreCommitPoint, tuple[str, ...]] = {
    _PreCommitPoint.MODEL_RESPONSE: (sha256_bytes(_PATCH_BYTES),),
    _PreCommitPoint.EVALUATION: tuple(
        sorted(
            {
                sha256_bytes(_EVALUATION_BYTES),
                sha256_bytes(_EVALUATION_STDERR_BYTES),
                sha256_bytes(_EVALUATION_STDOUT_BYTES),
            }
        )
    ),
    _PreCommitPoint.RECOVERY_OUTSTANDING: (),
    _PreCommitPoint.RECOVERY_RESPONSE: (),
}


class _CommitBarrierConnection:
    """Delegate a live connection while stopping one armed commit in test code."""

    def __init__(self, connection: sqlite3.Connection, barrier: Connection) -> None:
        self._connection = connection
        self._barrier = barrier
        self._point: _PreCommitPoint | None = None

    def arm(self, point: _PreCommitPoint) -> None:
        if self._point is not None:
            raise AssertionError("commit barrier is already armed")
        self._point = point

    def commit(self) -> None:
        point = self._point
        if point is None:
            self._connection.commit()
            return
        self._point = None
        self._barrier.send(("precommit", point.value))
        self._barrier.recv()
        raise AssertionError("pre-COMMIT barrier was unexpectedly released")

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _pending_manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        experiment_id="experiment-crash-recovery",
        task_id="fixture-process-crash",
        candidate_id="scripted-crash-probe",
        requested_model="fake-model-v1",
        seed=0,
        environment_digest=f"sha256:{'a' * 64}",
        code_revision="process-crash-test",
        budget_limits=BudgetLimits(
            max_total_tokens=100,
            max_model_calls=1,
            max_model_retries=0,
            max_tool_calls=0,
        ),
        created_at=_START,
    )


def _child_run_to_barrier(
    state_directory_text: str,
    stage_text: str,
    barrier: Connection,
) -> None:
    """Commit one selected prefix, report readiness, then await the parent's kill."""
    stage = _CrashPoint(stage_text)
    state_directory = Path(state_directory_text)
    run_id = f"crash-{stage.value.replace('_', '-')}"
    artifacts = FileArtifactStore(state_directory / "artifacts")
    store = EventStore(
        state_directory / "runs.db",
        clock=DeterministicClock(started_at=_START),
    )
    try:
        store.create_run(_pending_manifest(run_id))
        if stage is _CrashPoint.RUN_CREATED:
            _signal_and_wait(barrier, stage)

        store.start_run(
            run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model="fake-model-v1",
        )
        task = artifacts.put_bytes(
            _TASK_BYTES,
            media_type="application/vnd.guildmind.task+json",
        )
        store.record_artifact(run_id, "task_spec", task)
        if stage is _CrashPoint.TASK_BOUND:
            _signal_and_wait(barrier, stage)

        store.start_model_request(
            run_id=run_id,
            request_id=_REQUEST_ID,
            maximum=_MAXIMUM,
            budget_used=_EMPTY,
            budget_reserved=_MAXIMUM,
        )
        if stage is _CrashPoint.MODEL_REQUEST_OUTSTANDING:
            _signal_and_wait(barrier, stage)

        patch = artifacts.put_bytes(
            _PATCH_BYTES,
            media_type="text/x-diff; charset=utf-8",
        )
        store.complete_model_response(
            run_id=run_id,
            request_id=_REQUEST_ID,
            returned_model="fake-model-v1",
            actual_usage=_ACTUAL,
            patch=patch,
            budget_used=_ACTUAL,
            budget_reserved=_EMPTY,
        )
        if stage is _CrashPoint.MODEL_RESPONSE_COMPLETED:
            _signal_and_wait(barrier, stage)

        evaluation_stdout = artifacts.put_bytes(
            _EVALUATION_STDOUT_BYTES,
            media_type="text/plain; charset=utf-8",
        )
        evaluation_stderr = artifacts.put_bytes(
            _EVALUATION_STDERR_BYTES,
            media_type="text/plain; charset=utf-8",
        )
        evaluation = artifacts.put_bytes(
            _EVALUATION_BYTES,
            media_type="application/vnd.guildmind.evaluation+json",
        )
        store.complete_evaluation(
            run_id=run_id,
            artifacts={
                "evaluation": evaluation,
                "evaluation_stderr": evaluation_stderr,
                "evaluation_stdout": evaluation_stdout,
            },
            evaluation_payload={
                "outcome": "passed",
                "result_sha256": sha256_bytes(b"process-crash-evaluation-result"),
            },
            status=RunStatus.SUCCEEDED,
            finished_at=_START + timedelta(seconds=2),
            terminal_reason=None,
            budget_used=_ACTUAL,
            budget_reserved=_EMPTY,
        )
        _signal_and_wait(barrier, stage)
    except BaseException as error:
        try:
            barrier.send(("error", stage.value, repr(error)))
        finally:
            raise
    finally:
        store.close()
        barrier.close()


def _child_run_to_precommit_barrier(
    state_directory_text: str,
    point_text: str,
    barrier: Connection,
) -> None:
    """Execute a selected phase body, then block its COMMIT until SIGKILL."""
    point = _PreCommitPoint(point_text)
    state_directory = Path(state_directory_text)
    run_id = f"precommit-{point.value.replace('_', '-')}"
    artifacts = FileArtifactStore(state_directory / "artifacts")
    store = EventStore(
        state_directory / "runs.db",
        clock=DeterministicClock(started_at=_START),
    )
    connection = _CommitBarrierConnection(store._connection, barrier)
    store._connection = cast(sqlite3.Connection, connection)
    try:
        store.create_run(_pending_manifest(run_id))
        store.start_run(
            run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model="fake-model-v1",
        )
        task = artifacts.put_bytes(
            _TASK_BYTES,
            media_type="application/vnd.guildmind.task+json",
        )
        store.record_artifact(run_id, "task_spec", task)
        store.start_model_request(
            run_id=run_id,
            request_id=_REQUEST_ID,
            maximum=_MAXIMUM,
            budget_used=_EMPTY,
            budget_reserved=_MAXIMUM,
        )

        patch: ArtifactRef | None = None
        if point in {
            _PreCommitPoint.MODEL_RESPONSE,
            _PreCommitPoint.EVALUATION,
            _PreCommitPoint.RECOVERY_RESPONSE,
        }:
            patch = artifacts.put_bytes(
                _PATCH_BYTES,
                media_type="text/x-diff; charset=utf-8",
            )

        if point is _PreCommitPoint.MODEL_RESPONSE:
            connection.arm(point)
            store.complete_model_response(
                run_id=run_id,
                request_id=_REQUEST_ID,
                returned_model="fake-model-v1",
                actual_usage=_ACTUAL,
                patch=_require_patch(patch),
                budget_used=_ACTUAL,
                budget_reserved=_EMPTY,
            )
        elif point in {
            _PreCommitPoint.EVALUATION,
            _PreCommitPoint.RECOVERY_RESPONSE,
        }:
            store.complete_model_response(
                run_id=run_id,
                request_id=_REQUEST_ID,
                returned_model="fake-model-v1",
                actual_usage=_ACTUAL,
                patch=_require_patch(patch),
                budget_used=_ACTUAL,
                budget_reserved=_EMPTY,
            )
            if point is _PreCommitPoint.EVALUATION:
                evaluation_stdout = artifacts.put_bytes(
                    _EVALUATION_STDOUT_BYTES,
                    media_type="text/plain; charset=utf-8",
                )
                evaluation_stderr = artifacts.put_bytes(
                    _EVALUATION_STDERR_BYTES,
                    media_type="text/plain; charset=utf-8",
                )
                evaluation = artifacts.put_bytes(
                    _EVALUATION_BYTES,
                    media_type="application/vnd.guildmind.evaluation+json",
                )
                connection.arm(point)
                store.complete_evaluation(
                    run_id=run_id,
                    artifacts={
                        "evaluation": evaluation,
                        "evaluation_stderr": evaluation_stderr,
                        "evaluation_stdout": evaluation_stdout,
                    },
                    evaluation_payload={
                        "outcome": "passed",
                        "result_sha256": sha256_bytes(b"process-crash-evaluation-result"),
                    },
                    status=RunStatus.SUCCEEDED,
                    finished_at=_START + timedelta(seconds=2),
                    terminal_reason=None,
                    budget_used=_ACTUAL,
                    budget_reserved=_EMPTY,
                )
            else:
                connection.arm(point)
                store.recover_run(
                    run_id,
                    finished_at=_START + timedelta(seconds=2),
                )
        else:
            connection.arm(point)
            store.recover_run(
                run_id,
                finished_at=_START + timedelta(seconds=2),
            )
        raise AssertionError("selected phase unexpectedly passed its commit barrier")
    except BaseException as error:
        try:
            barrier.send(("error", point.value, repr(error)))
        finally:
            raise
    finally:
        store.close()
        barrier.close()


def _require_patch(patch: ArtifactRef | None) -> ArtifactRef:
    if patch is None:
        raise AssertionError("test setup did not materialize the patch")
    return patch


def _signal_and_wait(barrier: Connection, stage: _CrashPoint) -> None:
    barrier.send(("ready", stage.value))
    barrier.recv()
    raise AssertionError("crash barrier was unexpectedly released")


def _kill_after_barrier(state_directory: Path, stage: _CrashPoint) -> None:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_run_to_barrier,
        args=(str(state_directory), stage.value, child_barrier),
        name=f"guildmind-crash-{stage.value}",
    )
    process.start()
    child_barrier.close()
    try:
        ready = wait((parent_barrier, process.sentinel), timeout=20)
        if parent_barrier not in ready:
            process.join(timeout=1)
            pytest.fail(f"child exited before crash barrier: exitcode={process.exitcode}")
        message = parent_barrier.recv()
        if message[0] != "ready":
            process.join(timeout=5)
            pytest.fail(f"child failed before crash barrier: {message!r}")
        assert message == ("ready", stage.value)
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        parent_barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _kill_before_commit(state_directory: Path, point: _PreCommitPoint) -> None:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_run_to_precommit_barrier,
        args=(str(state_directory), point.value, child_barrier),
        name=f"guildmind-precommit-crash-{point.value}",
    )
    process.start()
    child_barrier.close()
    try:
        ready = wait((parent_barrier, process.sentinel), timeout=20)
        if parent_barrier not in ready:
            process.join(timeout=1)
            pytest.fail(f"child exited before pre-COMMIT barrier: exitcode={process.exitcode}")
        message = parent_barrier.recv()
        if message != ("precommit", point.value):
            process.join(timeout=5)
            pytest.fail(f"child failed before pre-COMMIT barrier: {message!r}")
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
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
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _canonical_artifact_digests(artifact_root: Path) -> tuple[str, ...]:
    sha_root = artifact_root / "sha256"
    if not sha_root.exists():
        return ()
    digests: list[str] = []
    for path in sha_root.glob("*/*"):
        if not path.is_file():
            continue
        digest = path.name
        assert len(digest) == 64
        assert path.parent.name == digest[:2]
        assert sha256_bytes(path.read_bytes()) == digest
        digests.append(digest)
    return tuple(sorted(digests))


def _artifact_reference(event: EventRecord) -> ArtifactRef:
    media_type = event.payload["media_type"]
    size_bytes = event.payload["size_bytes"]
    digest = event.payload["sha256"]
    storage_ref = event.payload["storage_ref"]
    if (
        not isinstance(media_type, str)
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not isinstance(digest, str)
        or not isinstance(storage_ref, str)
    ):
        raise AssertionError("artifact event has a malformed reference")
    return ArtifactRef(
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=digest,
        storage_ref=storage_ref,
    )


def _expected_missing(stage: _CrashPoint) -> tuple[str, ...]:
    expected = {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
        "task_spec",
    }
    return tuple(sorted(expected - set(_EXPECTED_BOUND_ARTIFACTS[stage])))


def _expected_final_event_types(stage: _CrashPoint) -> tuple[str, ...]:
    prefix = _PREFIX_EVENT_TYPES[stage]
    if stage is _CrashPoint.EVALUATION_COMPLETED:
        return prefix
    ambiguous = (
        ("model.request_ambiguous",) if stage is _CrashPoint.MODEL_REQUEST_OUTSTANDING else ()
    )
    return (
        *prefix,
        *("artifact.not_produced",) * len(_expected_missing(stage)),
        *ambiguous,
        "budget.snapshot",
        "run.terminal",
    )


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX SIGKILL")
@pytest.mark.parametrize("stage", tuple(_CrashPoint), ids=lambda stage: stage.value)
def test_sigkill_prefixes_recover_atomically_and_idempotently(
    tmp_path: Path,
    stage: _CrashPoint,
) -> None:
    state_directory = tmp_path / stage.value
    database = state_directory / "runs.db"
    artifact_root = state_directory / "artifacts"
    run_id = f"crash-{stage.value.replace('_', '-')}"

    _kill_after_barrier(state_directory, stage)
    _assert_sqlite_integrity(database)

    recovery_clock = DeterministicClock(started_at=_START + timedelta(minutes=1))
    with EventStore(database, clock=recovery_clock) as store:
        prefix = store.list_events(run_id)
        prefix_types = tuple(event.event_type for event in prefix)
        assert prefix_types == _PREFIX_EVENT_TYPES[stage]
        assert [event.sequence for event in prefix] == list(range(len(prefix)))
        prefix_state = replay_events(prefix)
        used_before, reserved_before = store.load_budget_state(run_id)

        recovered = store.recover_run(
            run_id,
            finished_at=_START + timedelta(minutes=2),
        )
        events_after_first_recovery = store.list_events(run_id)
        recovered_again = store.recover_run(
            run_id,
            finished_at=_START + timedelta(minutes=3),
        )
        final_events = store.list_events(run_id)
        used_after, reserved_after = store.load_budget_state(run_id)

        assert recovered_again == recovered
        assert final_events == events_after_first_recovery

    assert tuple(event.event_type for event in final_events) == _expected_final_event_types(stage)
    assert sum(event.event_type == "run.terminal" for event in final_events) == 1
    final_state = replay_events(final_events, require_terminal=True)
    assert final_state.artifacts == {
        name: artifact.sha256 for name, artifact in recovered.artifacts.items()
    }
    assert tuple(sorted(final_state.artifacts)) == _EXPECTED_BOUND_ARTIFACTS[stage]

    if stage is _CrashPoint.MODEL_REQUEST_OUTSTANDING:
        assert used_before == _EMPTY
        assert reserved_before == _MAXIMUM
        assert used_after == _MAXIMUM
        assert recovered.terminal_reason == "ambiguous_model_request"
        assert final_state.model_request_state == "ambiguous"
    elif stage in {
        _CrashPoint.MODEL_RESPONSE_COMPLETED,
        _CrashPoint.EVALUATION_COMPLETED,
    }:
        assert used_before == _ACTUAL
        assert reserved_before == _EMPTY
        assert used_after == _ACTUAL
    else:
        assert used_before == _EMPTY
        assert reserved_before == _EMPTY
        assert used_after == _EMPTY
    assert reserved_after == _EMPTY

    missing = _expected_missing(stage)
    if stage is _CrashPoint.EVALUATION_COMPLETED:
        assert recovered.status is RunStatus.SUCCEEDED
        assert recovered.terminal_reason is None
        assert prefix_state.status is RunStatus.SUCCEEDED
        assert final_state.evaluation_outcome == "passed"
        assert final_state.absent_artifacts == {}
    else:
        assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
        if stage is not _CrashPoint.MODEL_REQUEST_OUTSTANDING:
            assert recovered.terminal_reason == "interrupted_run_recovered"
        assert tuple(sorted(final_state.absent_artifacts)) == missing
        assert set(final_state.absent_artifacts.values()) == {"interrupted"}

    artifact_store = FileArtifactStore(artifact_root)
    for event in final_events:
        if event.event_type != "artifact.recorded":
            continue
        reference = _artifact_reference(event)
        artifact_store.verify(reference)
        assert len(artifact_store.get_bytes(reference)) == reference.size_bytes
    assert not tuple(artifact_root.rglob(".artifact-*"))


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX SIGKILL")
@pytest.mark.parametrize("point", tuple(_PreCommitPoint), ids=lambda point: point.value)
def test_sigkill_before_commit_restores_previous_phase_without_inference(
    tmp_path: Path,
    point: _PreCommitPoint,
) -> None:
    state_directory = tmp_path / point.value
    database = state_directory / "runs.db"
    artifact_root = state_directory / "artifacts"
    run_id = f"precommit-{point.value.replace('_', '-')}"
    prefix_point = _PRECOMMIT_PREFIX[point]

    _kill_before_commit(state_directory, point)
    _assert_sqlite_integrity(database)

    recovery_clock = DeterministicClock(started_at=_START + timedelta(minutes=10))
    with EventStore(database, clock=recovery_clock) as store:
        prefix = store.list_events(run_id)
        manifest_before = store.load_manifest(run_id)
        used_before, reserved_before = store.load_budget_state(run_id)
        assert tuple(event.event_type for event in prefix) == _PREFIX_EVENT_TYPES[prefix_point]
        assert [event.sequence for event in prefix] == list(range(len(prefix)))
        prefix_state = replay_events(prefix)
        assert manifest_before.status is RunStatus.RUNNING
        assert prefix_state.status is RunStatus.RUNNING

        recovered = store.recover_run(
            run_id,
            finished_at=_START + timedelta(minutes=11),
        )
        first_recovery_events = store.list_events(run_id)
        used_after, reserved_after = store.load_budget_state(run_id)
        recovered_again = store.recover_run(
            run_id,
            finished_at=_START + timedelta(minutes=12),
        )
        final_events = store.list_events(run_id)

    assert recovered_again == recovered
    assert final_events == first_recovery_events
    assert tuple(event.event_type for event in final_events) == _expected_final_event_types(
        prefix_point
    )
    assert sum(event.event_type == "run.terminal" for event in final_events) == 1
    final_state = replay_events(final_events, require_terminal=True)
    assert final_state.artifacts == {
        name: artifact.sha256 for name, artifact in recovered.artifacts.items()
    }
    assert tuple(sorted(final_state.artifacts)) == _EXPECTED_BOUND_ARTIFACTS[prefix_point]
    assert reserved_after == _EMPTY

    if prefix_point is _CrashPoint.MODEL_REQUEST_OUTSTANDING:
        assert used_before == _EMPTY
        assert reserved_before == _MAXIMUM
        assert used_after == _MAXIMUM
        assert recovered.terminal_reason == "ambiguous_model_request"
        assert final_state.model_request_state == "ambiguous"
    else:
        assert used_before == _ACTUAL
        assert reserved_before == _EMPTY
        assert used_after == _ACTUAL
        assert recovered.terminal_reason == "interrupted_run_recovered"
        assert final_state.model_request_state == "completed"

    referenced = tuple(sorted(artifact.sha256 for artifact in recovered.artifacts.values()))
    finalized = _canonical_artifact_digests(artifact_root)
    orphans = tuple(sorted(set(finalized) - set(referenced)))
    assert orphans == _PRECOMMIT_ORPHANS[point]
    assert not tuple(artifact_root.rglob(".artifact-*"))
    artifact_store = FileArtifactStore(artifact_root)
    for reference in recovered.artifacts.values():
        artifact_store.verify(reference)
    _assert_sqlite_integrity(database)
