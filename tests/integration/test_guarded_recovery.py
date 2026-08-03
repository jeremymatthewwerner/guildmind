from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import guildmind.runtime.recovery as recovery_module
from guildmind.domain import (
    BudgetLimits,
    EventRecord,
    RunManifest,
    RunStatus,
    TaskSpec,
    canonical_json,
)
from guildmind.runtime.clock import DeterministicClock
from guildmind.runtime.recovery import (
    RecoveryDenialReason,
    RecoveryDeniedError,
    RecoveryPostCommitMaintenanceError,
    recover_existing_fixture_run,
    terminalize_existing_fixture_budget_refusal,
)
from guildmind.runtime.replay import replay_events
from guildmind.storage.artifacts import FileArtifactStore
from guildmind.storage.coordinator import StorageIntegrityReport
from guildmind.storage.coordinator import audit_storage as real_audit_storage
from guildmind.storage.events import EventStore, VerifiedRunRoot
from guildmind.storage.integrity import ArtifactAudit
from guildmind.storage.integrity import audit_artifact_store as real_artifact_audit
from guildmind.storage.maintenance import (
    MaintenanceIntegrityError,
    MaintenanceIntegrityReason,
    MaintenanceLease,
)

_START = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
_IMAGE_DIGEST = f"sha256:{'a' * 64}"


def _populate_running_run(
    state_directory: Path,
    *,
    run_id: str = "run-a",
) -> tuple[FileArtifactStore, dict[str, Path]]:
    artifact_store = FileArtifactStore(
        state_directory / "artifacts",
        trusted_base=state_directory.parent,
    )
    problem = artifact_store.put_text("Repair the fixture.")
    repository = artifact_store.put_bytes(
        b'{"files":[]}',
        media_type="application/vnd.guildmind.tree+json",
    )
    visible_test = artifact_store.put_text(
        "def test_fixture():\n    assert True\n",
        media_type="text/x-python",
    )
    task = TaskSpec(
        task_id="fixture-guarded-recovery",
        source="test",
        split="fixture",
        repository="guildmind/guarded-recovery",
        repository_commit="fixture-v1",
        image_digest=_IMAGE_DIGEST,
        task_content_hash="b" * 64,
        problem_statement=problem,
        repository_snapshot=repository,
        visible_tests=(visible_test,),
    )
    task_reference = artifact_store.put_text(
        canonical_json(task),
        media_type="application/vnd.guildmind.task+json",
    )
    pending = RunManifest(
        run_id=run_id,
        experiment_id="guarded-recovery-test",
        task_id=task.task_id,
        candidate_id="candidate-a",
        requested_model="scripted-model",
        seed=0,
        environment_digest=_IMAGE_DIGEST,
        code_revision="guarded-recovery-test",
        budget_limits=BudgetLimits(max_model_calls=1),
        created_at=_START,
    )
    with EventStore(
        state_directory / "runs.db",
        clock=DeterministicClock(started_at=_START),
    ) as event_store:
        event_store.create_run(pending)
        event_store.start_run(
            run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model=pending.requested_model,
        )
        event_store.record_artifact(run_id, "task_spec", task_reference)
    return artifact_store, {
        "repository": artifact_store.path_for(repository),
        "task_spec": artifact_store.path_for(task_reference),
    }


def _events(state_directory: Path, run_id: str = "run-a") -> tuple[EventRecord, ...]:
    with EventStore.open_existing_read_only(
        state_directory / "runs.db",
        trusted_base=state_directory.parent,
    ) as event_store:
        return tuple(event_store.list_events(run_id))


def test_guarded_recovery_succeeds_idempotently_and_preserves_orphans(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    artifact_store, _ = _populate_running_run(state)
    orphan = artifact_store.put_text("ownerless finalized orphan")
    orphan_path = artifact_store.path_for(orphan)
    orphan_identity = (orphan_path.stat().st_dev, orphan_path.stat().st_ino)
    orphan_bytes = orphan_path.read_bytes()

    first = recover_existing_fixture_run(
        state_directory=state,
        run_id="run-a",
        clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
    )
    second = recover_existing_fixture_run(
        state_directory=state,
        run_id="run-a",
        clock=DeterministicClock(started_at=_START + timedelta(minutes=2)),
    )

    assert first.manifest.status is RunStatus.INFRASTRUCTURE_ERROR
    assert first.manifest.terminal_reason == "interrupted_run_recovered"
    assert second == first
    replay = replay_events(list(first.events), require_terminal=True)
    assert replay.status is RunStatus.INFRASTRUCTURE_ERROR
    assert sum(event.event_type == "run.terminal" for event in first.events) == 1
    assert (orphan_path.stat().st_dev, orphan_path.stat().st_ino) == orphan_identity
    assert orphan_path.read_bytes() == orphan_bytes


def test_guarded_recovery_classifies_lease_release_failure_as_post_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    real_close = MaintenanceLease.close

    def close_then_report_integrity_failure(lease: MaintenanceLease) -> None:
        real_close(lease)
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.LOCK_CHANGED,
            state_directory=state,
            detail="injected release-time identity failure",
        )

    monkeypatch.setattr(MaintenanceLease, "close", close_then_report_integrity_failure)

    with pytest.raises(RecoveryPostCommitMaintenanceError) as captured:
        recover_existing_fixture_run(
            state_directory=state,
            run_id="run-a",
            clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        )

    assert captured.value.result.manifest.status is RunStatus.INFRASTRUCTURE_ERROR
    assert captured.value.result.events[-1].event_type == "run.terminal"
    with EventStore.open_existing_read_only(
        state / "runs.db",
        trusted_base=state.parent,
    ) as event_store:
        assert event_store.load_manifest("run-a") == captured.value.result.manifest
        assert tuple(event_store.list_events("run-a")) == captured.value.result.events


def test_guarded_recovery_accepts_a_validated_terminal_reason(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)

    recovered = recover_existing_fixture_run(
        state_directory=state,
        run_id="run-a",
        clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        terminal_reason="runner_exception",
    )

    assert recovered.manifest.terminal_reason == "runner_exception"
    assert recovered.events[-1].event_type == "run.terminal"
    assert recovered.events[-1].payload["terminal_reason"] == "runner_exception"


def test_guarded_recovery_rejects_an_invalid_terminal_reason_without_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    events_before = _events(state)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(
            state_directory=state,
            run_id="run-a",
            terminal_reason=" ",
        )

    assert captured.value.reason is RecoveryDenialReason.STORAGE_CHANGED
    assert _events(state) == events_before


def test_guarded_recovery_captures_events_before_commit_without_postcommit_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)

    def reject_public_observation(_store: EventStore, _run_id: str) -> list[EventRecord]:
        raise sqlite3.OperationalError("public event observation must not follow recovery commit")

    with monkeypatch.context() as patch:
        patch.setattr(EventStore, "list_events", reject_public_observation)
        recovered = recover_existing_fixture_run(
            state_directory=state,
            run_id="run-a",
            clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        )

    assert recovered.manifest.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.events == _events(state)
    assert sum(event.event_type == "run.terminal" for event in recovered.events) == 1


def test_guarded_recovery_normalizes_sqlite_writer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    events_before = _events(state)

    def reject_writer_open(
        _path: Path,
        *,
        clock: object | None = None,
        trusted_base: Path | None = None,
    ) -> EventStore:
        del clock, trusted_base
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(EventStore, "open_existing_writable", reject_writer_open)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert captured.value.reason is RecoveryDenialReason.STORAGE_CHANGED
    assert _events(state) == events_before


def test_guarded_budget_refusal_returns_transactionally_captured_terminal_events(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)

    terminalized = terminalize_existing_fixture_budget_refusal(
        state_directory=state,
        run_id="run-a",
        clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
    )

    assert terminalized.manifest.status is RunStatus.BUDGET_EXHAUSTED
    assert terminalized.manifest.terminal_reason == "model_reservation_refused"
    assert terminalized.events == _events(state)
    replay = replay_events(list(terminalized.events), require_terminal=True)
    assert replay.status is RunStatus.BUDGET_EXHAUSTED
    assert sum(event.event_type == "run.terminal" for event in terminalized.events) == 1


def test_guarded_budget_refusal_rolls_back_when_recursive_bytes_change_during_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _, paths = _populate_running_run(state)
    events_before = _events(state)
    save_manifest = EventStore._save_manifest_locked
    guard_calls = 0

    def record_guard(
        roots: tuple[VerifiedRunRoot, ...],
        artifact_store: FileArtifactStore,
    ) -> ArtifactAudit:
        nonlocal guard_calls
        guard_calls += 1
        return real_artifact_audit(roots, artifact_store)

    def save_then_corrupt(
        event_store: EventStore,
        run_id: str,
        manifest: RunManifest,
    ) -> None:
        save_manifest(event_store, run_id, manifest)
        if manifest.status is RunStatus.BUDGET_EXHAUSTED:
            paths["repository"].write_bytes(b"changed while budget refusal was being committed")

    monkeypatch.setattr(recovery_module, "audit_artifact_store", record_guard)
    monkeypatch.setattr(EventStore, "_save_manifest_locked", save_then_corrupt)

    with pytest.raises(RecoveryDeniedError) as captured:
        terminalize_existing_fixture_budget_refusal(
            state_directory=state,
            run_id="run-a",
            clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        )

    assert guard_calls == 2
    assert captured.value.reason is RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)


@pytest.mark.parametrize("create_state", [False, True], ids=("absent-state", "empty-state"))
def test_guarded_recovery_denies_without_creating_storage(
    tmp_path: Path,
    create_state: bool,
) -> None:
    state = tmp_path / "state"
    if create_state:
        state.mkdir()

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert captured.value.reason is RecoveryDenialReason.STORAGE_NOT_RECOVERABLE
    assert state.exists() is create_state
    assert not (state / "runs.db").exists()
    assert not (state / "artifacts").exists()


def test_guarded_recovery_rejects_filesystem_root_before_storage_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem_root = Path(Path.cwd().anchor)
    database = filesystem_root / "runs.db"
    database_existed = os.path.lexists(database)

    def unexpected_audit(state_directory: Path) -> StorageIntegrityReport:
        raise AssertionError(f"must not inspect root state: {state_directory}")

    monkeypatch.setattr(recovery_module, "audit_storage", unexpected_audit)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(
            state_directory=filesystem_root,
            run_id="run-a",
        )

    assert captured.value.reason is RecoveryDenialReason.STORAGE_NOT_RECOVERABLE
    assert os.path.lexists(database) is database_existed


@pytest.mark.parametrize("corrupt", [False, True], ids=("missing", "corrupt"))
def test_guarded_recovery_denies_missing_or_corrupt_referenced_bytes(
    tmp_path: Path,
    corrupt: bool,
) -> None:
    state = tmp_path / "state"
    _, paths = _populate_running_run(state)
    events_before = _events(state)
    if corrupt:
        paths["task_spec"].write_bytes(b"tampered task")
    else:
        paths["task_spec"].unlink()

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert captured.value.reason is RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)


def test_guarded_recovery_denies_unknown_run_before_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    events_before = _events(state)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="unknown-run")

    assert captured.value.reason is RecoveryDenialReason.RUN_NOT_FOUND
    assert _events(state) == events_before


def test_guarded_recovery_rejects_a_ledger_commit_after_its_initial_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    target_events_before = _events(state)
    audit_calls = 0

    def audit_then_commit_another_run(state_directory: Path) -> StorageIntegrityReport:
        nonlocal audit_calls
        report = real_audit_storage(state_directory)
        audit_calls += 1
        _populate_running_run(state, run_id="run-b")
        return report

    monkeypatch.setattr(recovery_module, "audit_storage", audit_then_commit_another_run)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert audit_calls == 1
    assert captured.value.reason is RecoveryDenialReason.STORAGE_CHANGED
    assert _events(state, "run-a") == target_events_before
    assert all(event.event_type != "run.terminal" for event in target_events_before)
    assert _events(state, "run-b")


def test_guarded_recovery_rolls_back_when_recursive_bytes_change_inside_writer_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _, paths = _populate_running_run(state)
    events_before = _events(state)
    guard_calls = 0

    def change_then_audit(
        roots: tuple[VerifiedRunRoot, ...],
        artifact_store: FileArtifactStore,
    ) -> ArtifactAudit:
        nonlocal guard_calls
        guard_calls += 1
        paths["repository"].write_bytes(b"changed after the initial audit")
        return real_artifact_audit(roots, artifact_store)

    monkeypatch.setattr(recovery_module, "audit_artifact_store", change_then_audit)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(
            state_directory=state,
            run_id="run-a",
            clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        )

    assert guard_calls == 1
    assert captured.value.reason is RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)


def test_guarded_recovery_rolls_back_when_recursive_bytes_change_during_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _, paths = _populate_running_run(state)
    events_before = _events(state)
    save_manifest = EventStore._save_manifest_locked
    guard_calls = 0
    corruption_injections = 0

    def record_guard(
        roots: tuple[VerifiedRunRoot, ...],
        artifact_store: FileArtifactStore,
    ) -> ArtifactAudit:
        nonlocal guard_calls
        guard_calls += 1
        return real_artifact_audit(roots, artifact_store)

    def save_then_corrupt(
        event_store: EventStore,
        run_id: str,
        manifest: RunManifest,
    ) -> None:
        nonlocal corruption_injections
        save_manifest(event_store, run_id, manifest)
        if manifest.status.is_terminal:
            corruption_injections += 1
            paths["repository"].write_bytes(b"changed while recovery was staging its commit")

    monkeypatch.setattr(recovery_module, "audit_artifact_store", record_guard)
    monkeypatch.setattr(EventStore, "_save_manifest_locked", save_then_corrupt)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(
            state_directory=state,
            run_id="run-a",
            clock=DeterministicClock(started_at=_START + timedelta(minutes=1)),
        )

    assert guard_calls == 2
    assert corruption_injections == 1
    assert captured.value.reason is RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)


@pytest.mark.parametrize(
    "replacement",
    ["state-directory", "database", "artifact-root"],
)
def test_guarded_recovery_rejects_equal_content_path_replacement_after_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    events_before = _events(state)
    replacement_calls = 0

    def audit_then_replace(state_directory: Path) -> StorageIntegrityReport:
        nonlocal replacement_calls
        report = real_audit_storage(state_directory)
        replacement_calls += 1
        if replacement == "state-directory":
            preserved = tmp_path / "preserved-state"
            state.rename(preserved)
            shutil.copytree(preserved, state)
        elif replacement == "database":
            database = state / "runs.db"
            preserved = state / "preserved-runs.db"
            database.rename(preserved)
            shutil.copy2(preserved, database)
        else:
            artifact_root = state / "artifacts"
            preserved = state / "preserved-artifacts"
            artifact_root.rename(preserved)
            shutil.copytree(preserved, artifact_root)
        return report

    monkeypatch.setattr(recovery_module, "audit_storage", audit_then_replace)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert replacement_calls == 1
    assert captured.value.reason is RecoveryDenialReason.STORAGE_CHANGED
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)


def test_guarded_recovery_rejects_trusted_base_replacement_with_same_state_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_base = tmp_path / "trusted-base"
    state = trusted_base / "state"
    _populate_running_run(state)
    events_before = _events(state)
    replacement_calls = 0

    def audit_then_replace_base(state_directory: Path) -> StorageIntegrityReport:
        nonlocal replacement_calls
        report = real_audit_storage(state_directory)
        replacement_calls += 1
        preserved_base = tmp_path / "preserved-base"
        trusted_base.rename(preserved_base)
        trusted_base.mkdir()
        (preserved_base / "state").rename(state)
        return report

    monkeypatch.setattr(recovery_module, "audit_storage", audit_then_replace_base)

    with pytest.raises(RecoveryDeniedError) as captured:
        recover_existing_fixture_run(state_directory=state, run_id="run-a")

    assert replacement_calls == 1
    assert captured.value.reason is RecoveryDenialReason.STORAGE_CHANGED
    assert _events(state) == events_before
    assert all(event.event_type != "run.terminal" for event in events_before)
