from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildmind.domain import BudgetLimits, BudgetUsage, EventRecord, RunManifest
from guildmind.evaluation import LocalEvaluationResult, LocalEvaluationSpec, LocalEvaluator
from guildmind.models import ModelResponse, ScriptedPatchModel
from guildmind.runtime.clock import DeterministicClock
from guildmind.runtime.recovery import (
    FixtureRecoveryResult,
    RecoveryDenialReason,
    RecoveryDeniedError,
    recover_existing_fixture_run,
    terminalize_existing_fixture_budget_refusal,
)
from guildmind.runtime.runner import FixtureRunner, FixtureRunPostCommitMaintenanceError
from guildmind.storage.events import EventStore
from guildmind.storage.maintenance import (
    MAINTENANCE_LOCK_FILENAME,
    QUARANTINE_ACTIVE_RELATIVE_PATH,
    MaintenanceBusyError,
    MaintenanceBusyReason,
    MaintenanceIntegrityError,
    MaintenanceIntegrityReason,
    MaintenanceLease,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_PATCH = _FIXTURE / "solution.patch"
_START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _require_exclusive_is_busy(state: Path) -> None:
    with pytest.raises(MaintenanceBusyError) as raised:
        MaintenanceLease.acquire_exclusive(state)
    assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD


class _LeaseCheckingModel:
    def __init__(self, state: Path, delegate: ScriptedPatchModel) -> None:
        self._state = state
        self._delegate = delegate
        self.checked = False

    @property
    def model_id(self) -> str:
        return self._delegate.model_id

    @property
    def maximum_usage(self) -> BudgetUsage:
        return self._delegate.maximum_usage

    def propose_patch(self, problem_statement: str) -> ModelResponse:
        _require_exclusive_is_busy(self._state)
        self.checked = True
        return self._delegate.propose_patch(problem_statement)


class _LeaseCheckingEvaluator(LocalEvaluator):
    def __init__(self, state: Path) -> None:
        self._state = state
        self.checked = False

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
        *,
        expected_patch_sha256: str | None = None,
    ) -> LocalEvaluationResult:
        _require_exclusive_is_busy(self._state)
        self.checked = True
        return super().evaluate(
            spec,
            patch_path,
            expected_patch_sha256=expected_patch_sha256,
        )


def test_fixture_runner_holds_shared_lease_across_model_and_evaluator_work(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    model = _LeaseCheckingModel(
        state,
        ScriptedPatchModel(
            _PATCH,
            maximum_usage=BudgetUsage(
                uncached_input_tokens=32,
                output_tokens=32,
                model_calls=1,
            ),
            actual_usage=BudgetUsage(
                uncached_input_tokens=12,
                output_tokens=8,
                model_calls=1,
            ),
        ),
    )
    evaluator = _LeaseCheckingEvaluator(state)

    result = FixtureRunner(
        state_directory=state,
        clock=DeterministicClock(started_at=_START),
        evaluator=evaluator,
    ).run(
        fixture_root=_FIXTURE,
        model=model,
        run_id="maintenance-lease-runner",
        code_revision="maintenance-lease-test",
        budget_limits=BudgetLimits(max_total_tokens=64, max_model_calls=1),
    )

    assert model.checked
    assert evaluator.checked
    assert result.manifest.status.is_terminal
    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_fixture_runner_classifies_lease_release_failure_as_post_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    real_close = MaintenanceLease.close

    def close_then_report_integrity_failure(lease: MaintenanceLease) -> None:
        real_close(lease)
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.LOCK_CHANGED,
            state_directory=state,
            detail="injected release-time identity failure",
        )

    monkeypatch.setattr(MaintenanceLease, "close", close_then_report_integrity_failure)
    with pytest.raises(FixtureRunPostCommitMaintenanceError) as captured:
        FixtureRunner(
            state_directory=state,
            clock=DeterministicClock(started_at=_START),
        ).run(
            fixture_root=_FIXTURE,
            model=ScriptedPatchModel(_PATCH),
            run_id="maintenance-postcommit-runner",
            code_revision="maintenance-lease-test",
        )

    assert captured.value.result.manifest.status.is_terminal
    with EventStore.open_existing_read_only(
        state / "runs.db",
        trusted_base=state.parent,
    ) as event_store:
        assert (
            event_store.load_manifest("maintenance-postcommit-runner")
            == captured.value.result.manifest
        )


def test_active_quarantine_fence_blocks_runner_before_any_publication_or_ledger_write(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    runner = FixtureRunner(
        state_directory=state,
        clock=DeterministicClock(started_at=_START),
    )
    marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"active")
    model = ScriptedPatchModel(_PATCH)

    with pytest.raises(MaintenanceBusyError) as raised:
        runner.run(
            fixture_root=_FIXTURE,
            model=model,
            run_id="maintenance-active-runner",
            code_revision="maintenance-lease-test",
        )

    assert raised.value.reason is MaintenanceBusyReason.QUARANTINE_ACTIVE
    assert not (state / "artifacts").exists()
    assert not (state / "runs.db").exists()


def _populate_running_run(state: Path, run_id: str) -> tuple[EventRecord, ...]:
    pending = RunManifest(
        run_id=run_id,
        experiment_id="maintenance-lease-test",
        task_id="maintenance-lease-task",
        candidate_id="candidate-a",
        requested_model="scripted-model",
        seed=0,
        environment_digest=f"sha256:{'a' * 64}",
        code_revision="maintenance-lease-test",
        budget_limits=BudgetLimits(max_model_calls=1),
        created_at=_START,
    )
    with EventStore(
        state / "runs.db",
        clock=DeterministicClock(started_at=_START),
    ) as event_store:
        event_store.create_run(pending)
        event_store.start_run(
            run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model=pending.requested_model,
        )
        return tuple(event_store.list_events(run_id))


@pytest.mark.parametrize(
    "terminalize",
    [recover_existing_fixture_run, terminalize_existing_fixture_budget_refusal],
    ids=["recovery", "budget-refusal"],
)
def test_guarded_terminalization_is_nonblocking_while_exclusive_maintenance_is_held(
    tmp_path: Path,
    terminalize: Callable[..., FixtureRecoveryResult],
) -> None:
    state = tmp_path / "state"
    run_id = "maintenance-lease-recovery"
    events_before = _populate_running_run(state, run_id)

    with (
        MaintenanceLease.acquire_exclusive(state),
        pytest.raises(RecoveryDeniedError) as raised,
    ):
        terminalize(state_directory=state, run_id=run_id)

    assert raised.value.reason is RecoveryDenialReason.MAINTENANCE_BUSY
    with EventStore.open_existing_read_only(
        state / "runs.db",
        trusted_base=state.parent,
    ) as event_store:
        assert tuple(event_store.list_events(run_id)) == events_before


def test_active_quarantine_fence_blocks_recovery_without_ledger_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    run_id = "maintenance-active-recovery"
    events_before = _populate_running_run(state, run_id)
    marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"active")

    with pytest.raises(RecoveryDeniedError) as raised:
        recover_existing_fixture_run(state_directory=state, run_id=run_id)

    assert raised.value.reason is RecoveryDenialReason.MAINTENANCE_BUSY
    assert (state / MAINTENANCE_LOCK_FILENAME).read_bytes() == b""
    assert marker.read_bytes() == b"active"
    with EventStore.open_existing_read_only(
        state / "runs.db",
        trusted_base=state.parent,
    ) as event_store:
        assert tuple(event_store.list_events(run_id)) == events_before


@pytest.mark.parametrize(
    ("shape", "expected_storage_state"),
    [("empty", "uninitialized"), ("corrupt-database", "database_invalid")],
)
def test_existing_denied_state_may_gain_only_the_coordination_lock(
    tmp_path: Path,
    shape: str,
    expected_storage_state: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    original_files: dict[str, bytes] = {}
    if shape == "corrupt-database":
        database = state / "runs.db"
        database.write_bytes(b"not a Guildmind SQLite database")
        original_files[database.name] = database.read_bytes()

    with pytest.raises(RecoveryDeniedError) as raised:
        recover_existing_fixture_run(state_directory=state, run_id="missing-run")

    assert raised.value.reason is RecoveryDenialReason.STORAGE_NOT_RECOVERABLE
    assert raised.value.storage_state is not None
    assert raised.value.storage_state.value == expected_storage_state
    assert (state / MAINTENANCE_LOCK_FILENAME).read_bytes() == b""
    assert {
        path.name: path.read_bytes()
        for path in state.iterdir()
        if path.is_file() and path.name != MAINTENANCE_LOCK_FILENAME
    } == original_files
