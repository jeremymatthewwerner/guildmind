from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import guildmind.storage.quarantine as quarantine_module
from guildmind.domain import BudgetLimits, RunManifest, TaskSpec, canonical_json, canonical_sha256
from guildmind.storage.artifacts import FileArtifactStore
from guildmind.storage.events import EventStore
from guildmind.storage.maintenance import MaintenanceLease
from guildmind.storage.quarantine import (
    QuarantineActive,
    QuarantineBefore,
    QuarantineDeniedError,
    QuarantineFailureReason,
    QuarantineIncompleteError,
    QuarantineOutcome,
    QuarantinePlan,
    quarantine_orphans,
)

_START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _initialized_state(tmp_path: Path) -> tuple[Path, FileArtifactStore]:
    state = tmp_path / "state"
    state.mkdir()
    with EventStore(state / "runs.db"):
        pass
    return state, FileArtifactStore(state / "artifacts", trusted_base=tmp_path)


def _active_transaction(state: Path) -> tuple[QuarantineActive, QuarantinePlan, Path]:
    active = QuarantineActive.model_validate_json(
        (state / "quarantine" / "v1" / "ACTIVE").read_bytes(),
        strict=True,
    )
    transaction = state / "quarantine" / "v1" / "transactions" / active.transaction_id
    plan = QuarantinePlan.model_validate_json((transaction / "PLAN.json").read_bytes(), strict=True)
    return active, plan, transaction


def _interrupt_after_move(
    monkeypatch: pytest.MonkeyPatch,
    state: Path,
) -> tuple[QuarantineActive, QuarantinePlan, Path]:
    original = quarantine_module._write_or_verify_receipt

    def fail_receipt(*_: object) -> str:
        raise OSError("injected failure after the candidate move")

    monkeypatch.setattr(quarantine_module, "_write_or_verify_receipt", fail_receipt)
    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)
    assert raised.value.reason is QuarantineFailureReason.IO_ERROR
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()
    monkeypatch.setattr(quarantine_module, "_write_or_verify_receipt", original)
    return _active_transaction(state)


def test_quarantines_all_v1_candidate_kinds_into_canonical_immutable_records(
    tmp_path: Path,
) -> None:
    state, store = _initialized_state(tmp_path)
    valid = store.put_text("valid finalized orphan")
    valid_path = store.path_for(valid)

    corrupt_digest = "a" * 64
    corrupt_path = store.root / "sha256" / "aa" / corrupt_digest
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"bytes that do not match the canonical name")
    temporary_path = corrupt_path.parent / ".artifact-interrupted"
    temporary_path.write_bytes(b"partial temporary bytes")

    result = quarantine_orphans(state)

    assert result.outcome is QuarantineOutcome.COMPLETED
    assert result.quarantined_count == 3
    assert result.final_report.clean
    assert result.transaction_id is not None
    assert not valid_path.exists()
    assert not corrupt_path.exists()
    assert not temporary_path.exists()
    assert not (state / "quarantine" / "v1" / "ACTIVE").exists()

    transaction = state / "quarantine" / "v1" / "transactions" / result.transaction_id
    plan = QuarantinePlan.model_validate_json((transaction / "PLAN.json").read_bytes(), strict=True)
    assert plan.transaction_id == canonical_sha256(plan.body)
    assert {candidate.body.finding.kind.value for candidate in plan.body.candidates} == {
        "valid_finalized_orphan",
        "corrupt_finalized_orphan",
        "temp_orphan",
    }
    assert len(tuple((transaction / "payload").iterdir())) == 3
    assert len(tuple((transaction / "receipts").iterdir())) == 3
    assert (transaction / "BEFORE.json").is_file()
    assert (transaction / "AFTER.json").is_file()
    assert (transaction / "COMPLETE.json").is_file()
    for record in (
        transaction / "BEFORE.json",
        transaction / "PLAN.json",
        transaction / "AFTER.json",
        transaction / "COMPLETE.json",
        *(transaction / "receipts").iterdir(),
    ):
        assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_clean_initialized_storage_is_an_exact_no_op(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with EventStore(state / "runs.db"):
        pass

    result = quarantine_orphans(state)

    assert result.outcome is QuarantineOutcome.NO_OP
    assert result.transaction_id is None
    assert result.quarantined_count == 0
    assert result.final_report.clean
    assert not (state / "quarantine").exists()


def test_second_invocation_after_completion_is_a_no_op(tmp_path: Path) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("one orphan")
    first = quarantine_orphans(state)

    second = quarantine_orphans(state)

    assert first.outcome is QuarantineOutcome.COMPLETED
    assert second.outcome is QuarantineOutcome.NO_OP
    assert second.final_report.clean


def test_missing_database_is_denied_without_moving_artifacts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = FileArtifactStore(state / "artifacts", trusted_base=tmp_path)
    orphan = store.put_text("database-less orphan")
    source = store.path_for(orphan)
    original = source.read_bytes()

    with pytest.raises(QuarantineDeniedError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE
    assert source.read_bytes() == original
    assert not (state / "quarantine").exists()


def test_nested_mount_candidate_is_denied_before_fencing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    orphan = store.put_text("simulated nested-mount orphan")
    source = store.path_for(orphan)
    original_bytes = source.read_bytes()
    original_file_identity = quarantine_module._file_identity

    def report_foreign_device(metadata: os.stat_result) -> quarantine_module.FileIdentity:
        identity = original_file_identity(metadata)
        return identity.model_copy(update={"device": identity.device + 1})

    monkeypatch.setattr(quarantine_module, "_file_identity", report_foreign_device)

    with pytest.raises(QuarantineDeniedError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.CROSS_DEVICE
    assert source.read_bytes() == original_bytes
    assert not (state / "quarantine").exists()


@pytest.mark.parametrize("kind", ["noncanonical", "symlink", "hardlink"])
def test_any_unsupported_finding_denies_the_whole_operation_before_fencing(
    tmp_path: Path,
    kind: str,
) -> None:
    state, store = _initialized_state(tmp_path)
    supported = store.put_text("supported orphan remains untouched")
    supported_path = store.path_for(supported)
    if kind == "noncanonical":
        (store.root / "unexpected").write_bytes(b"unrecognized root entry")
    elif kind == "symlink":
        (store.root / "unexpected").symlink_to(tmp_path / "outside")
    else:
        os.link(supported_path, supported_path.parent / ".artifact-linked")

    with pytest.raises(QuarantineDeniedError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.UNSUPPORTED_FINDING
    assert supported_path.exists()
    assert not (state / "quarantine").exists()


def test_referenced_recursive_bytes_are_preserved_while_an_orphan_is_moved(
    tmp_path: Path,
) -> None:
    state, store = _initialized_state(tmp_path)
    problem = store.put_text("Fix the referenced function.")
    repository = store.put_bytes(
        b'{"files":[]}\n',
        media_type="application/vnd.guildmind.tree+json",
    )
    task = TaskSpec(
        task_id="quarantine-fixture",
        source="test",
        split="fixture",
        repository="guildmind/quarantine",
        repository_commit="fixture-v1",
        image_digest=f"sha256:{'b' * 64}",
        task_content_hash="c" * 64,
        problem_statement=problem,
        repository_snapshot=repository,
    )
    task_reference = store.put_text(
        canonical_json(task),
        media_type="application/vnd.guildmind.task+json",
    )
    manifest = RunManifest(
        run_id="referenced-quarantine-run",
        experiment_id="quarantine-test",
        task_id=task.task_id,
        candidate_id="candidate",
        requested_model="fake-model",
        seed=0,
        environment_digest=task.image_digest,
        code_revision="quarantine-test",
        budget_limits=BudgetLimits(max_model_calls=1),
        created_at=_START,
    )
    with EventStore(state / "runs.db") as events:
        events.create_run(manifest)
        events.start_run(
            manifest.run_id,
            started_at=_START + timedelta(seconds=1),
            requested_model=manifest.requested_model,
        )
        events.record_artifact(manifest.run_id, "task_spec", task_reference)
    orphan = store.put_text("only this entry is ownerless")

    result = quarantine_orphans(state)

    assert result.quarantined_count == 1
    assert store.get_bytes(problem) == b"Fix the referenced function."
    assert store.get_bytes(repository) == b'{"files":[]}\n'
    assert store.get_bytes(task_reference) == canonical_json(task).encode()
    assert not store.path_for(orphan).exists()


def test_restart_after_move_before_receipt_reconciles_forward_and_synthesizes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    orphan = store.put_text("resume after atomic move")
    active, plan, transaction = _interrupt_after_move(monkeypatch, state)
    candidate = plan.body.candidates[0]

    assert not store.path_for(orphan).exists()
    assert (transaction / "payload" / candidate.candidate_id).is_file()
    assert not (transaction / "receipts" / f"{candidate.candidate_id}.json").exists()

    destination = transaction / "payload" / candidate.candidate_id
    source_parent = (store.root / candidate.body.source_relative_path).parent
    durability_inodes = (
        destination.stat().st_ino,
        (transaction / "payload").stat().st_ino,
        source_parent.stat().st_ino,
    )
    observed_syncs: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        inode = os.fstat(descriptor).st_ino
        if inode in durability_inodes:
            observed_syncs.append(inode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    result = quarantine_orphans(state)

    assert result.outcome is QuarantineOutcome.COMPLETED
    assert result.resumed
    assert result.transaction_id == active.transaction_id
    assert observed_syncs[:3] == list(durability_inodes)
    assert (transaction / "receipts" / f"{candidate.candidate_id}.json").is_file()
    assert not (state / "quarantine" / "v1" / "ACTIVE").exists()


@pytest.mark.parametrize("ambiguous", ["both", "neither"])
def test_restart_fails_closed_when_candidate_is_at_both_or_neither_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous: str,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("ambiguous restart candidate")
    _, plan, transaction = _interrupt_after_move(monkeypatch, state)
    candidate = plan.body.candidates[0]
    source = store.root / candidate.body.source_relative_path
    destination = transaction / "payload" / candidate.candidate_id
    if ambiguous == "both":
        source.write_bytes(destination.read_bytes())
    else:
        destination.unlink()

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.CANDIDATE_AMBIGUOUS
    assert raised.value.transaction_id == plan.transaction_id
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


def test_corrupt_durable_plan_fails_closed_and_keeps_active_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan with a later corrupt plan")
    _, plan, transaction = _interrupt_after_move(monkeypatch, state)
    (transaction / "PLAN.json").write_bytes(b"{}")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.RECORD_INVALID
    assert raised.value.transaction_id == plan.transaction_id
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


def test_database_inode_replacement_after_fencing_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan protected by database identity")
    _, plan, _ = _interrupt_after_move(monkeypatch, state)
    replacement = state / "replacement.db"
    shutil.copyfile(state / "runs.db", replacement)
    os.replace(replacement, state / "runs.db")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.PLAN_CHANGED
    assert raised.value.transaction_id == plan.transaction_id
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


@pytest.mark.parametrize("database_shape", ["hardlink", "symlink"])
def test_database_links_are_denied_before_fencing(
    tmp_path: Path,
    database_shape: str,
) -> None:
    state, store = _initialized_state(tmp_path)
    orphan = store.put_text("orphan beside an invalid database path")
    database = state / "runs.db"
    if database_shape == "hardlink":
        os.link(database, tmp_path / "runs-link.db")
    else:
        preserved = tmp_path / "preserved-runs.db"
        database.rename(preserved)
        database.symlink_to(preserved)

    with pytest.raises(QuarantineDeniedError):
        quarantine_orphans(state)

    assert store.path_for(orphan).is_file()
    assert not (state / "quarantine").exists()


def test_unexplained_payload_entry_is_not_adopted_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan with unexplained payload neighbor")
    _, plan, transaction = _interrupt_after_move(monkeypatch, state)
    (transaction / "payload" / "unexpected").write_bytes(b"not in the durable plan")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.NAMESPACE_INVALID
    assert raised.value.transaction_id == plan.transaction_id
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


def test_known_active_receipt_and_completion_temps_are_cleaned_during_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan with interrupted record publications")
    _, plan, transaction = _interrupt_after_move(monkeypatch, state)
    candidate = plan.body.candidates[0]
    version = state / "quarantine" / "v1"
    active_temp = version / f".ACTIVE.tmp-{'a' * 32}"
    receipt_temp = transaction / "receipts" / f".{candidate.candidate_id}.json.tmp-{'b' * 32}"
    after_temp = transaction / f".AFTER.json.tmp-{'c' * 32}"
    active_temp.write_bytes(b"partial")
    receipt_temp.write_bytes(b"partial")
    after_temp.write_bytes(b"partial")

    result = quarantine_orphans(state)

    assert result.outcome is QuarantineOutcome.COMPLETED
    assert not active_temp.exists()
    assert not receipt_temp.exists()
    assert not after_temp.exists()


def test_unexplained_version_entry_blocks_resume_and_keeps_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan with invalid version namespace")
    _, plan, _ = _interrupt_after_move(monkeypatch, state)
    (state / "quarantine" / "v1" / "unexpected").write_bytes(b"unexplained")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.NAMESPACE_INVALID
    assert raised.value.transaction_id is None
    assert plan.transaction_id
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


def test_corrupt_active_record_is_a_fenced_error_with_unknown_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan with a later corrupt active marker")
    _interrupt_after_move(monkeypatch, state)
    (state / "quarantine" / "v1" / "ACTIVE").write_bytes(b"{}")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.RECORD_INVALID
    assert raised.value.transaction_id is None
    assert (state / "quarantine" / "v1" / "ACTIVE").is_file()


def test_malformed_active_temporary_without_active_is_a_pre_fence_denial(
    tmp_path: Path,
) -> None:
    state, _ = _initialized_state(tmp_path)
    version = state / "quarantine" / "v1"
    version.mkdir(parents=True)
    malformed_temporary = version / f".ACTIVE.tmp-{'a' * 32}"
    malformed_temporary.mkdir()

    with pytest.raises(QuarantineDeniedError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.RECORD_INVALID
    assert malformed_temporary.is_dir()
    with MaintenanceLease.acquire_shared(state):
        pass


def test_malformed_active_temporary_with_active_is_a_fenced_error(
    tmp_path: Path,
) -> None:
    state, _ = _initialized_state(tmp_path)
    version = state / "quarantine" / "v1"
    version.mkdir(parents=True)
    malformed_temporary = version / f".ACTIVE.tmp-{'a' * 32}"
    malformed_temporary.mkdir()
    (version / "ACTIVE").write_bytes(b"{}")

    with pytest.raises(QuarantineIncompleteError) as raised:
        quarantine_orphans(state)

    assert raised.value.reason is QuarantineFailureReason.RECORD_INVALID
    assert raised.value.transaction_id is None
    assert malformed_temporary.is_dir()
    assert (version / "ACTIVE").is_file()


def test_exact_pre_active_prepared_plan_is_reused_after_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("prepared deterministic orphan")
    original = quarantine_module._publish_active

    def interrupt_before_active(*_: object) -> None:
        raise OSError("injected pre-ACTIVE interruption")

    monkeypatch.setattr(quarantine_module, "_publish_active", interrupt_before_active)
    with pytest.raises(QuarantineDeniedError) as raised:
        quarantine_orphans(state)
    assert raised.value.reason is QuarantineFailureReason.IO_ERROR
    assert not (state / "quarantine" / "v1" / "ACTIVE").exists()
    transactions = tuple((state / "quarantine" / "v1" / "transactions").iterdir())
    assert len(transactions) == 1
    prepared_id = transactions[0].name
    monkeypatch.setattr(quarantine_module, "_publish_active", original)

    result = quarantine_orphans(state)

    assert result.outcome is QuarantineOutcome.COMPLETED
    assert result.transaction_id == prepared_id
    assert not result.resumed


def test_existing_directory_and_immutable_record_repair_parent_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    active = QuarantineActive(
        transaction_id="a" * 64,
        plan_sha256="b" * 64,
        before_sha256="c" * 64,
    )
    record = parent / "ACTIVE"
    record.write_text(canonical_json(active))
    record.chmod(0o600)
    observed: list[int] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        observed.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    try:
        with quarantine_module._ensure_directory_at(parent_descriptor, "child"):
            pass
        quarantine_module._write_immutable_model(parent_descriptor, "ACTIVE", active)
    finally:
        os.close(parent_descriptor)

    assert observed.count(parent_descriptor) >= 2


def test_open_directory_closes_descriptor_when_final_identity_check_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    (parent / "child").mkdir(parents=True)
    parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    real_stat = os.stat
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []
    calls = 0

    def fail_final_stat(*args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected final identity failure")
        return real_stat(*args, **kwargs)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    real_open = os.open

    def record_open(*args: Any, **kwargs: Any) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "stat", fail_final_stat)
    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "close", record_close)
    try:
        with pytest.raises(OSError, match="final identity"):
            quarantine_module._open_existing_directory_descriptor(parent_descriptor, "child")
    finally:
        real_close(parent_descriptor)

    assert opened[-1] in closed


def test_before_report_semantics_are_revalidated_against_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, store = _initialized_state(tmp_path)
    store.put_text("orphan for BEFORE validation")
    _, plan, transaction = _interrupt_after_move(monkeypatch, state)
    before = QuarantineBefore.model_validate_json(
        (transaction / "BEFORE.json").read_bytes(),
        strict=True,
    )
    inconsistent_body = plan.body.model_copy(update={"reachable_sha256": "f" * 64})
    inconsistent_plan = QuarantinePlan(
        transaction_id=canonical_sha256(inconsistent_body),
        body=inconsistent_body,
    )

    with pytest.raises(
        quarantine_module._ProtocolFailure,
        match="reachable graph does not match",
    ):
        quarantine_module._require_before_matches_plan(before, inconsistent_plan)


def test_namespace_enumeration_limit_fails_closed_without_unbounded_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "bounded"
    directory.mkdir()
    (directory / "one").touch()
    (directory / "two").touch()
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    monkeypatch.setattr(quarantine_module, "_MAX_DIRECTORY_ENTRIES", 1)
    try:
        with pytest.raises(quarantine_module._ProtocolFailure, match="entry limit"):
            tuple(quarantine_module._iter_entry_names(descriptor, "bounded test"))
    finally:
        os.close(descriptor)


def test_record_parent_sync_failure_is_never_reported_as_immutable_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "records"
    directory.mkdir()
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    active = QuarantineActive(
        transaction_id="a" * 64,
        plan_sha256="b" * 64,
        before_sha256="c" * 64,
    )
    record = directory / "ACTIVE"
    record.write_text(canonical_json(active))
    record.chmod(0o600)
    real_fsync = os.fsync

    def fail_parent_fsync(candidate: int) -> None:
        if candidate == descriptor:
            raise OSError("injected record parent sync failure")
        real_fsync(candidate)

    monkeypatch.setattr(os, "fsync", fail_parent_fsync)
    try:
        with pytest.raises(OSError, match="record parent sync"):
            quarantine_module._write_immutable_model(descriptor, "ACTIVE", active)
    finally:
        os.close(descriptor)
