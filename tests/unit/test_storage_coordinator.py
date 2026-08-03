from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildmind.domain import (
    BudgetLimits,
    RunManifest,
    TaskSpec,
    canonical_json,
)
from guildmind.storage.artifacts import FileArtifactStore
from guildmind.storage.coordinator import (
    StorageIntegrityReport,
    StorageIntegrityState,
    VerifiedRunRootCommitment,
    audit_storage,
)
from guildmind.storage.events import EventStore, VerifiedRunRoot
from guildmind.storage.integrity import ArtifactFindingKind

_START = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
_IMAGE_DIGEST = f"sha256:{'a' * 64}"


def _initialize_database(state_directory: Path) -> None:
    state_directory.mkdir(parents=True, exist_ok=True)
    with EventStore(state_directory / "runs.db"):
        pass


def _populate_running_run(
    state_directory: Path,
    *,
    run_id: str = "run-a",
) -> tuple[FileArtifactStore, Path]:
    state_directory.mkdir(parents=True, exist_ok=True)
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
        task_id="fixture-storage-audit",
        source="test",
        split="fixture",
        repository="guildmind/storage-audit",
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
        experiment_id="storage-integrity-test",
        task_id=task.task_id,
        candidate_id="candidate-a",
        requested_model="scripted-model",
        seed=0,
        environment_digest=_IMAGE_DIGEST,
        code_revision="storage-integrity-test",
        budget_limits=BudgetLimits(max_model_calls=1),
        created_at=_START,
    )
    with EventStore(state_directory / "runs.db") as event_store:
        event_store.create_run(pending)
        event_store.start_run(
            run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model=pending.requested_model,
        )
        event_store.record_artifact(run_id, "task_spec", task_reference)
    return artifact_store, artifact_store.path_for(task_reference)


def _assert_all_write_gates_closed(report: StorageIntegrityReport) -> None:
    assert not report.initialization_allowed
    assert not report.references_verified
    assert not report.read_allowed
    assert not report.mutation_allowed
    assert not report.quarantine_allowed
    assert not report.clean


@pytest.mark.parametrize("create_state", [False, True], ids=("absent", "empty"))
def test_absent_database_and_empty_cas_is_uninitialized_without_creating_paths(
    tmp_path: Path,
    create_state: bool,
) -> None:
    state = tmp_path / "state"
    if create_state:
        state.mkdir()

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.UNINITIALIZED
    assert report.initialization_allowed
    assert report.clean
    assert not report.database_present
    assert not report.artifact_root_present
    assert not report.references_verified
    assert not report.read_allowed
    assert not report.mutation_allowed
    assert not report.quarantine_allowed
    assert not (state / "runs.db").exists()
    assert not (state / "artifacts").exists()
    assert state.exists() is create_state


def test_absent_database_and_existing_empty_cas_is_still_uninitialized(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    (state / "artifacts" / "sha256" / "ab").mkdir(parents=True)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.UNINITIALIZED
    assert report.initialization_allowed
    assert report.artifact_root_present
    assert report.artifact_audit is not None
    assert report.artifact_audit.findings == ()
    assert not report.quarantine_allowed
    assert not (state / "runs.db").exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_absent_database_with_any_sidecar_is_not_initializable(
    tmp_path: Path,
    suffix: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    sidecar = Path(f"{state / 'runs.db'}{suffix}")
    sidecar.write_bytes(b"ambiguous crash residue")

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == f"database_{suffix[1:]}_regular"
    assert sidecar.read_bytes() == b"ambiguous crash residue"
    assert not (state / "runs.db").exists()


def test_absent_database_with_a_linked_sidecar_does_not_follow_it(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside-wal"
    outside.write_bytes(b"untouched")
    wal = Path(f"{state / 'runs.db'}-wal")
    wal.symlink_to(outside)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "database_wal_symlink"
    assert outside.read_bytes() == b"untouched"
    assert wal.is_symlink()


def test_absent_database_with_artifacts_has_no_authority_or_quarantine_gate(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    artifact_store = FileArtifactStore(
        state / "artifacts",
        trusted_base=state.parent,
    )
    orphan = artifact_store.put_text("unbound evidence")

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
    _assert_all_write_gates_closed(report)
    assert report.ledger_snapshot is None
    assert report.artifact_audit is not None
    assert report.artifact_audit.quarantine_allowed
    assert report.artifact_audit.findings[0].kind is ArtifactFindingKind.VALID_FINALIZED_ORPHAN
    assert report.artifact_audit.findings[0].relative_path == orphan.storage_ref
    assert artifact_store.get_bytes(orphan) == b"unbound evidence"
    assert not (state / "runs.db").exists()


def test_leaf_symlinks_are_rejected_without_following_or_initializing_them(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("untouched", encoding="utf-8")
    (state / "artifacts").symlink_to(outside, target_is_directory=True)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "artifact_root_symlink"
    assert secret.read_text(encoding="utf-8") == "untouched"
    assert not (state / "runs.db").exists()

    (state / "artifacts").unlink()
    database_target = outside / "runs.db"
    database_target.write_bytes(b"not a database")
    (state / "runs.db").symlink_to(database_target)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "database_symlink"
    assert database_target.read_bytes() == b"not a database"


def test_existing_non_database_is_invalid_and_never_initialized(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    database = state / "runs.db"
    original = b"definitely not SQLite"
    database.write_bytes(original)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "database_integrity_validation_failed"
    assert database.read_bytes() == original
    assert not (state / "artifacts").exists()


@pytest.mark.parametrize(
    ("statement", "parameters"),
    [
        (
            "UPDATE events SET event_id = ? WHERE run_id = ? AND sequence = 0",
            ("sql-only-event-id", "run-a"),
        ),
        (
            "UPDATE budget_state SET used_json = used_json || ' ' WHERE run_id = ?",
            ("run-a",),
        ),
    ],
    ids=("event-index-disagreement", "noncanonical-budget-json"),
)
def test_storage_audit_rejects_false_safe_ledger_representations(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    with sqlite3.connect(state / "runs.db") as connection:
        connection.execute(statement, parameters)
        connection.commit()

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.ledger_snapshot is None
    assert report.artifact_audit is None
    assert report.diagnostic == "database_integrity_validation_failed"


def test_validated_empty_database_is_bound_and_clean_without_creating_cas(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _initialize_database(state)

    first = audit_storage(state)
    second = audit_storage(state)

    assert first == second
    assert first.state is StorageIntegrityState.INITIALIZED_EMPTY
    assert first.database_present
    assert not first.artifact_root_present
    assert first.ledger_snapshot is not None
    assert first.ledger_snapshot.roots == ()
    assert first.artifact_audit is not None
    assert first.artifact_audit.snapshot_sha256 == first.ledger_snapshot.snapshot_sha256
    assert first.references_verified
    assert first.read_allowed
    assert first.mutation_allowed
    assert first.quarantine_allowed
    assert first.clean
    assert not first.initialization_allowed
    assert not (state / "artifacts").exists()


@pytest.mark.parametrize("temporary", [False, True], ids=("finalized", "temporary"))
def test_validated_empty_database_distinguishes_unreferenced_findings(
    tmp_path: Path,
    temporary: bool,
) -> None:
    state = tmp_path / "state"
    _initialize_database(state)
    artifact_store = FileArtifactStore(
        state / "artifacts",
        trusted_base=state.parent,
    )
    if temporary:
        shard = artifact_store.root / "sha256" / "ab"
        shard.mkdir(parents=True)
        artifact_path = shard / ".artifact-interrupted"
        artifact_path.write_bytes(b"partial")
        expected_kind = ArtifactFindingKind.TEMP_ORPHAN
    else:
        reference = artifact_store.put_text("finalized orphan")
        artifact_path = artifact_store.path_for(reference)
        expected_kind = ArtifactFindingKind.VALID_FINALIZED_ORPHAN

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.INITIALIZED_EMPTY_WITH_UNREFERENCED_FINDINGS
    assert report.references_verified
    assert report.read_allowed
    assert report.mutation_allowed
    assert report.quarantine_allowed
    assert not report.clean
    assert report.artifact_audit is not None
    assert report.artifact_audit.findings[0].kind is expected_kind
    assert artifact_path.exists()


def test_healthy_state_verifies_recursive_references_and_binds_snapshot(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.HEALTHY
    assert report.ledger_snapshot is not None
    assert tuple(root.run_id for root in report.ledger_snapshot.roots) == ("run-a",)
    assert report.artifact_audit is not None
    assert report.artifact_audit.snapshot_sha256 == report.ledger_snapshot.snapshot_sha256
    assert len(report.artifact_audit.reachable) == 4
    assert all(item.bytes_verified for item in report.artifact_audit.reachable)
    assert report.artifact_audit.findings == ()
    assert report.references_verified
    assert report.read_allowed
    assert report.mutation_allowed
    assert report.quarantine_allowed
    assert report.clean

    serialized = json.loads(report.model_dump_json())
    reparsed = StorageIntegrityReport.model_validate(serialized)
    assert reparsed == report


def test_healthy_state_distinguishes_unreferenced_findings(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    artifact_store, _ = _populate_running_run(state)
    orphan = artifact_store.put_text("orphan created before a rolled-back commit")

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.HEALTHY_WITH_UNREFERENCED_FINDINGS
    assert report.references_verified
    assert report.read_allowed
    assert report.mutation_allowed
    assert report.quarantine_allowed
    assert not report.clean
    assert report.artifact_audit is not None
    finding = next(
        item for item in report.artifact_audit.findings if item.relative_path == orphan.storage_ref
    )
    assert finding.kind is ArtifactFindingKind.VALID_FINALIZED_ORPHAN
    assert artifact_store.get_bytes(orphan) == b"orphan created before a rolled-back commit"


@pytest.mark.parametrize("corrupt", [False, True], ids=("missing", "corrupt"))
def test_missing_or_corrupt_referenced_evidence_closes_every_operation_gate(
    tmp_path: Path,
    corrupt: bool,
) -> None:
    state = tmp_path / "state"
    _, task_path = _populate_running_run(state)
    if corrupt:
        task_path.write_bytes(b"tampered")
        expected = ArtifactFindingKind.CORRUPT_REFERENCED
    else:
        task_path.unlink()
        expected = ArtifactFindingKind.MISSING_REFERENCED

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.REFERENCED_EVIDENCE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.ledger_snapshot is not None
    assert report.artifact_audit is not None
    owned = [finding for finding in report.artifact_audit.findings if finding.owners]
    assert any(finding.kind is expected for finding in owned)
    assert owned[0].owners[0].run_id == "run-a"
    assert owned[0].owners[0].path == ("task_spec",)


def test_missing_artifact_store_classifies_committed_references_as_missing(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    artifact_root = state / "artifacts"
    artifact_root.rename(state / "artifacts-preserved-outside-store")

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.REFERENCED_EVIDENCE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.artifact_audit is not None
    assert report.artifact_audit.complete
    assert report.artifact_audit.reachable
    assert all(not item.bytes_verified for item in report.artifact_audit.reachable)
    assert {finding.kind for finding in report.artifact_audit.findings} == {
        ArtifactFindingKind.MISSING_REFERENCED
    }


def test_incomplete_inventory_closes_gates_without_changing_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _initialize_database(state)
    artifact_store = FileArtifactStore(
        state / "artifacts",
        trusted_base=state.parent,
    )
    orphan = artifact_store.put_text("must remain")
    original_scandir = __import__("os").scandir

    def deny_artifact_scan(path: str | Path) -> object:
        if Path(path) == artifact_store.root:
            raise PermissionError("simulated scan denial")
        return original_scandir(path)

    monkeypatch.setattr("guildmind.storage.integrity.os.scandir", deny_artifact_scan)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.AUDIT_INCOMPLETE
    _assert_all_write_gates_closed(report)
    assert report.artifact_audit is not None
    assert not report.artifact_audit.complete
    assert report.artifact_audit.findings[0].kind is ArtifactFindingKind.SCAN_ERROR
    assert artifact_store.get_bytes(orphan) == b"must remain"


def test_artifact_root_disappearance_after_preflight_does_not_recreate_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _initialize_database(state)
    writer = FileArtifactStore(
        state / "artifacts",
        trusted_base=state.parent,
    )
    orphan = writer.put_text("preserved after disappearance")
    artifact_root = writer.root
    moved_root = state / "artifacts-moved-during-open"
    real_open = FileArtifactStore.open_existing_read_only

    def disappear_then_open(
        root: Path,
        *,
        trusted_base: Path | None = None,
    ) -> FileArtifactStore:
        root.rename(moved_root)
        return real_open(root, trusted_base=trusted_base)

    monkeypatch.setattr(
        FileArtifactStore,
        "open_existing_read_only",
        staticmethod(disappear_then_open),
    )

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert not artifact_root.exists()
    assert moved_root.is_dir()
    assert (moved_root / orphan.storage_ref).read_bytes() == b"preserved after disappearance"
    assert report.artifact_audit is None
    assert report.diagnostic == "database_integrity_validation_failed"


def test_state_directory_symlink_is_rejected_before_descendant_construction(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    state = tmp_path / "state"
    state.symlink_to(outside, target_is_directory=True)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "state_directory_symlink"
    assert list(outside.iterdir()) == []


def test_state_appearing_after_missing_observation_closes_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    from guildmind.storage import coordinator

    real_observe = coordinator._observe_path
    state_observations = 0

    def create_before_final_observation(path: Path) -> object:
        nonlocal state_observations
        if path == state:
            state_observations += 1
            if state_observations == 2:
                state.mkdir()
                (state / "runs.db").write_bytes(b"appeared after preflight")
        return real_observe(path)

    monkeypatch.setattr(coordinator, "_observe_path", create_before_final_observation)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "state_directory_changed_during_audit"
    assert (state / "runs.db").read_bytes() == b"appeared after preflight"


def test_artifact_store_appearing_after_missing_database_audit_closes_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    from guildmind.storage import coordinator

    real_audit = coordinator._audit_without_database

    def create_after_audit(
        state_directory: Path,
        artifact_root: Path,
        observation: coordinator._PathObservation,
    ) -> object:
        result = real_audit(state_directory, artifact_root, observation)
        store = FileArtifactStore(state / "artifacts", trusted_base=state.parent)
        store.put_text("appeared after preflight")
        return result

    monkeypatch.setattr(coordinator, "_audit_without_database", create_after_audit)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "storage_paths_changed_during_audit"
    assert (state / "artifacts").is_dir()


def test_artifact_link_appearing_after_empty_ledger_audit_closes_gates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    _initialize_database(state)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"untouched")
    from guildmind.storage import coordinator

    real_audit = coordinator._audit_for_snapshot

    def link_after_audit(
        roots: tuple[VerifiedRunRoot, ...],
        snapshot: coordinator.VerifiedLedgerSnapshot,
        state_directory: Path,
        artifact_root: Path,
        observation: coordinator._PathObservation,
    ) -> object:
        result = real_audit(
            roots,
            snapshot,
            state_directory,
            artifact_root,
            observation,
        )
        (state / "artifacts").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(coordinator, "_audit_for_snapshot", link_after_audit)

    report = audit_storage(state)

    assert report.state is StorageIntegrityState.DATABASE_INVALID
    _assert_all_write_gates_closed(report)
    assert report.diagnostic == "database_integrity_validation_failed"
    assert sentinel.read_bytes() == b"untouched"
    assert (state / "artifacts").is_symlink()


def test_report_rejects_forged_derived_gates(tmp_path: Path) -> None:
    report = audit_storage(tmp_path / "absent")
    payload = report.model_dump(mode="json")
    payload["mutation_allowed"] = True

    with pytest.raises(ValueError, match="state and gates must be derived"):
        StorageIntegrityReport.model_validate(payload)


def test_report_diagnostic_closes_healthy_operation_gates(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _populate_running_run(state)
    report = audit_storage(state)
    payload = report.model_dump(mode="json")
    payload.update(
        {
            "clean": False,
            "diagnostic": "database_changed_after_audit",
            "mutation_allowed": False,
            "quarantine_allowed": False,
            "read_allowed": False,
            "references_verified": False,
            "state": StorageIntegrityState.AUDIT_INCOMPLETE.value,
        }
    )

    reparsed = StorageIntegrityReport.model_validate(payload)

    assert reparsed.state is StorageIntegrityState.AUDIT_INCOMPLETE
    _assert_all_write_gates_closed(reparsed)


def test_ledger_commitment_rejects_revision_beyond_event_head() -> None:
    with pytest.raises(ValueError, match="revision must be less than event count"):
        VerifiedRunRootCommitment(
            run_id="run-a",
            manifest_revision=1,
            manifest_sha256="a" * 64,
            event_count=1,
            head_event_sha256="b" * 64,
        )
