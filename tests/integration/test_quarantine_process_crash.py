"""Real-process SIGKILL coverage for resumable quarantine durability prefixes.

The child installs test-only wrappers around the exact rename, ``fsync``, unlink,
record, and lease methods used by the production protocol.  Each wrapper performs the
real operation first when the named prefix is post-operation, announces the boundary
over a pipe, and blocks.  The parent therefore sends a real ``SIGKILL`` without timing
guesses or production crash hooks.

This is process-crash evidence only.  It does not claim that APFS, a storage
controller, or any other filesystem preserves these prefixes across sudden power loss.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import re
import signal
import stat
import sys
from contextlib import suppress
from enum import StrEnum
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import NoReturn

import pytest
from pydantic import BaseModel

import guildmind.storage.quarantine as quarantine_module
from guildmind.domain import canonical_json, canonical_sha256, sha256_bytes
from guildmind.storage import (
    ArtifactFindingKind,
    EventStore,
    FileArtifactStore,
    MaintenanceBusyError,
    MaintenanceBusyReason,
    MaintenanceLease,
    QuarantineActive,
    QuarantineAfter,
    QuarantineBefore,
    QuarantineCandidate,
    QuarantineComplete,
    QuarantineOutcome,
    QuarantinePlan,
    QuarantineReceipt,
    QuarantineResult,
    StorageIntegrityReport,
    audit_storage,
    quarantine_orphans,
)
from guildmind.storage._fsops import rename_noreplace_at as _real_rename_noreplace_at

_RAW_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")


class _Boundary(StrEnum):
    BEFORE_PUBLISHED = "before_published_before_transaction_sync"
    PLAN_PUBLISHED = "plan_published_before_transaction_sync"
    ACTIVE_PUBLISHED = "active_published_before_version_sync"
    BEFORE_FIRST_MOVE = "immediately_before_first_move"
    AFTER_SELECTED_RENAME = "immediately_after_selected_rename"
    PAYLOAD_SYNCED = "between_payload_and_source_parent_sync"
    MOVE_DIRECTORIES_SYNCED = "after_both_move_directory_syncs_before_receipt"
    RECEIPT_PUBLISHED = "receipt_published_before_receipts_sync"
    FIRST_RECEIPT_DURABLE = "first_durable_receipt_before_second_candidate"
    ALL_RECEIPTS_DURABLE = "all_receipts_durable_before_after"
    AFTER_PUBLISHED = "after_published_before_transaction_sync"
    AFTER_DURABLE = "durable_after_before_complete"
    COMPLETE_PUBLISHED = "complete_published_before_transaction_sync"
    COMPLETE_DURABLE = "durable_complete_before_active_removal"
    ACTIVE_UNLINKED = "active_unlinked_before_version_sync"
    VERSION_SYNCED = "version_synced_before_lease_release"


_BOUNDARIES = tuple(_Boundary)
_TARGET_FINDING: dict[_Boundary, ArtifactFindingKind] = {
    _Boundary.AFTER_SELECTED_RENAME: ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
    _Boundary.PAYLOAD_SYNCED: ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN,
    _Boundary.MOVE_DIRECTORIES_SYNCED: ArtifactFindingKind.TEMP_ORPHAN,
}
_PUBLISHED_RECORD: dict[_Boundary, str] = {
    _Boundary.BEFORE_PUBLISHED: "BEFORE.json",
    _Boundary.PLAN_PUBLISHED: "PLAN.json",
    _Boundary.ACTIVE_PUBLISHED: "ACTIVE",
    _Boundary.AFTER_PUBLISHED: "AFTER.json",
    _Boundary.COMPLETE_PUBLISHED: "COMPLETE.json",
}

_VALID_BYTES = b"valid finalized process-crash orphan\n"
_CORRUPT_BYTES = b"bytes that disagree with their finalized digest name\n"
_TEMP_BYTES = b"interrupted temporary artifact bytes\n"


def _enter_and_block(barrier: Connection, boundary: _Boundary) -> NoReturn:
    barrier.send(("entered", boundary.value))
    barrier.recv()
    raise AssertionError("the quarantine process-crash barrier was unexpectedly released")


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


class _CrashInjector:
    """Install semantic, child-local barriers without raw syscall ordinals."""

    def __init__(
        self,
        state: Path,
        boundary: _Boundary,
        barrier: Connection,
    ) -> None:
        self.state = state
        self.boundary = boundary
        self.barrier = barrier
        self.target_finding = _TARGET_FINDING.get(boundary)
        self.armed_candidate_id: str | None = None
        self.move_calls = 0
        self.receipts_returned = 0
        self.move_sync_phase = 0
        self.payload_identity: tuple[int, int] | None = None
        self.source_parent_identity: tuple[int, int] | None = None
        self.after_durable = False
        self.complete_durable = False
        self.patcher = pytest.MonkeyPatch()

        self.real_rename = _real_rename_noreplace_at
        self.real_fsync = os.fsync
        self.real_unlink = os.unlink
        self.real_move = quarantine_module._move_candidate
        self.real_receipt = quarantine_module._write_or_verify_receipt
        self.real_write = quarantine_module._write_immutable_model
        self.real_remove_active = quarantine_module._remove_active
        self.real_lease_close = MaintenanceLease.close

    def install(self) -> None:
        self.patcher.setattr(quarantine_module, "rename_noreplace_at", self._rename)
        self.patcher.setattr(quarantine_module, "_move_candidate", self._move_candidate)
        self.patcher.setattr(
            quarantine_module,
            "_write_or_verify_receipt",
            self._write_receipt,
        )
        self.patcher.setattr(
            quarantine_module,
            "_write_immutable_model",
            self._write_model,
        )
        self.patcher.setattr(quarantine_module, "_remove_active", self._remove_active)
        if self.boundary in {
            _Boundary.PAYLOAD_SYNCED,
            _Boundary.MOVE_DIRECTORIES_SYNCED,
        }:
            self.patcher.setattr(os, "fsync", self._fsync)
        if self.boundary is _Boundary.ACTIVE_UNLINKED:
            self.patcher.setattr(os, "unlink", self._unlink)
        if self.boundary is _Boundary.VERSION_SYNCED:

            def close_before_release(lease: MaintenanceLease) -> None:
                if (self.state / "quarantine" / "v1" / "ACTIVE").exists():
                    raise AssertionError("ACTIVE still exists before final lease release")
                if not self.complete_durable:
                    raise AssertionError("COMPLETE was not durable before final lease release")
                _enter_and_block(self.barrier, self.boundary)

            self.patcher.setattr(MaintenanceLease, "close", close_before_release)

    def _move_candidate(
        self,
        artifact_descriptor: int,
        payload_descriptor: int,
        receipts_descriptor: int,
        plan: QuarantinePlan,
        plan_sha256: str,
        candidate: QuarantineCandidate,
    ) -> None:
        self.move_calls += 1
        if self.boundary is _Boundary.FIRST_RECEIPT_DURABLE and self.move_calls == 2:
            if self.receipts_returned != 1:
                raise AssertionError("the first receipt was not durable before move two")
            _enter_and_block(self.barrier, self.boundary)
        if candidate.body.finding.kind is self.target_finding:
            if self.armed_candidate_id is not None:
                raise AssertionError("more than one candidate matched the selected finding kind")
            self.armed_candidate_id = candidate.candidate_id
        self.real_move(
            artifact_descriptor,
            payload_descriptor,
            receipts_descriptor,
            plan,
            plan_sha256,
            candidate,
        )

    def _rename(
        self,
        source_descriptor: int,
        source_name: str,
        destination_descriptor: int,
        destination_name: str,
    ) -> None:
        candidate_move = _RAW_SHA256.fullmatch(destination_name) is not None
        if self.boundary is _Boundary.BEFORE_FIRST_MOVE and candidate_move and self.move_calls == 1:
            _enter_and_block(self.barrier, self.boundary)

        self.real_rename(
            source_descriptor,
            source_name,
            destination_descriptor,
            destination_name,
        )

        published_name = _PUBLISHED_RECORD.get(self.boundary)
        if destination_name == published_name:
            _enter_and_block(self.barrier, self.boundary)
        if (
            self.boundary is _Boundary.RECEIPT_PUBLISHED
            and _RECEIPT_NAME.fullmatch(destination_name) is not None
        ):
            _enter_and_block(self.barrier, self.boundary)
        if candidate_move and destination_name == self.armed_candidate_id:
            self.payload_identity = _descriptor_identity(destination_descriptor)
            self.source_parent_identity = _descriptor_identity(source_descriptor)
            if self.boundary is _Boundary.AFTER_SELECTED_RENAME:
                _enter_and_block(self.barrier, self.boundary)
            if self.boundary in {
                _Boundary.PAYLOAD_SYNCED,
                _Boundary.MOVE_DIRECTORIES_SYNCED,
            }:
                self.move_sync_phase = 1

    def _fsync(self, descriptor: int) -> None:
        identity = _descriptor_identity(descriptor)
        if self.move_sync_phase == 1:
            if identity != self.payload_identity:
                raise AssertionError("the first post-move sync was not the payload directory")
            self.real_fsync(descriptor)
            self.move_sync_phase = 2
            if self.boundary is _Boundary.PAYLOAD_SYNCED:
                _enter_and_block(self.barrier, self.boundary)
            return
        if self.move_sync_phase == 2:
            if identity != self.source_parent_identity:
                raise AssertionError("the second post-move sync was not the source parent")
            self.real_fsync(descriptor)
            self.move_sync_phase = 3
            return
        self.real_fsync(descriptor)

    def _write_receipt(self, parent_descriptor: int, receipt: QuarantineReceipt) -> str:
        if (
            self.boundary is _Boundary.MOVE_DIRECTORIES_SYNCED
            and receipt.candidate_id == self.armed_candidate_id
        ):
            if self.move_sync_phase != 3:
                raise AssertionError("both move directory syncs did not return before receipt")
            _enter_and_block(self.barrier, self.boundary)
        digest = self.real_receipt(parent_descriptor, receipt)
        self.receipts_returned += 1
        return digest

    def _write_model(
        self,
        parent_descriptor: int,
        name: str,
        model: BaseModel,
        *,
        maximum: int = quarantine_module._MAX_RECORD_BYTES,
    ) -> str:
        if self.boundary is _Boundary.ALL_RECEIPTS_DURABLE and name == "AFTER.json":
            if self.receipts_returned != 3:
                raise AssertionError("not every candidate receipt was durable before AFTER")
            _enter_and_block(self.barrier, self.boundary)
        if self.boundary is _Boundary.AFTER_DURABLE and name == "COMPLETE.json":
            if not self.after_durable:
                raise AssertionError("AFTER was not durable before COMPLETE")
            _enter_and_block(self.barrier, self.boundary)
        digest = self.real_write(
            parent_descriptor,
            name,
            model,
            maximum=maximum,
        )
        if name == "AFTER.json":
            self.after_durable = True
        elif name == "COMPLETE.json":
            self.complete_durable = True
        return digest

    def _remove_active(
        self,
        version_descriptor: int,
        expected: QuarantineActive,
        result: QuarantineResult,
    ) -> None:
        if self.boundary is _Boundary.COMPLETE_DURABLE:
            if not self.complete_durable:
                raise AssertionError("COMPLETE was not durable before ACTIVE removal")
            _enter_and_block(self.barrier, self.boundary)
        self.real_remove_active(version_descriptor, expected, result)

    def _unlink(
        self,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *,
        dir_fd: int | None = None,
    ) -> None:
        self.real_unlink(path, dir_fd=dir_fd)
        if path == "ACTIVE":
            _enter_and_block(self.barrier, self.boundary)


def _child_quarantine_at_boundary(
    state_directory_text: str,
    boundary_text: str,
    barrier: Connection,
) -> None:
    state = Path(state_directory_text)
    boundary = _Boundary(boundary_text)
    injector = _CrashInjector(state, boundary, barrier)
    injector.install()
    try:
        quarantine_orphans(state)
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(("error", boundary.value, type(error).__name__, str(error)))
        raise
    else:
        barrier.send(("error", boundary.value, "UnexpectedReturn", "quarantine completed"))
    finally:
        injector.patcher.undo()
        barrier.close()


def _child_run_quarantine(state_directory_text: str, result_pipe: Connection) -> None:
    try:
        result = quarantine_orphans(Path(state_directory_text))
    except BaseException as error:
        result_pipe.send(("error", type(error).__name__, str(error)))
        raise
    else:
        result_pipe.send(("result", canonical_json(result)))
    finally:
        result_pipe.close()


def _kill_at_boundary(state: Path, boundary: _Boundary) -> None:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_quarantine_at_boundary,
        args=(str(state), boundary.value, child_barrier),
        name=f"guildmind-quarantine-crash-{boundary.name.lower()}",
    )
    process.start()
    child_barrier.close()
    try:
        ready = wait((parent_barrier, process.sentinel), timeout=30)
        if parent_barrier not in ready:
            process.join(timeout=0)
            pytest.fail(
                "child exited before the quarantine barrier: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        try:
            message = parent_barrier.recv()
        except EOFError:
            process.join(timeout=0)
            pytest.fail(
                "child closed the quarantine barrier: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        if message != ("entered", boundary.value):
            pytest.fail(f"child failed before the expected quarantine barrier: {message!r}")

        if boundary is _Boundary.VERSION_SYNCED:
            with pytest.raises(MaintenanceBusyError) as raised:
                MaintenanceLease.acquire_shared(state)
            assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD

        pid = process.pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        terminated = wait((process.sentinel,), timeout=10)
        assert process.sentinel in terminated
        process.join(timeout=0)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        parent_barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _run_quarantine_in_fresh_process(state: Path) -> QuarantineResult:
    context = multiprocessing.get_context("spawn")
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_run_quarantine,
        args=(str(state), child_pipe),
        name="guildmind-quarantine-fresh-resume",
    )
    process.start()
    child_pipe.close()
    try:
        ready = wait((parent_pipe, process.sentinel), timeout=30)
        if parent_pipe not in ready:
            process.join(timeout=0)
            pytest.fail(f"fresh quarantine process exited without a result: {process.exitcode}")
        try:
            message: object = parent_pipe.recv()
        except EOFError:
            process.join(timeout=0)
            pytest.fail(f"fresh quarantine process closed its result pipe: {process.exitcode}")
        terminated = wait((process.sentinel,), timeout=30)
        assert process.sentinel in terminated
        process.join(timeout=0)
        assert process.exitcode == 0
        if not isinstance(message, tuple) or len(message) != 2:
            pytest.fail(f"fresh quarantine process sent a malformed result: {message!r}")
        message_kind, payload = message
        if message_kind != "result":
            pytest.fail(f"fresh quarantine process failed: {message!r}")
        if not isinstance(payload, str):
            pytest.fail(f"fresh quarantine result payload is not JSON text: {message!r}")
        return QuarantineResult.model_validate_json(payload, strict=True)
    finally:
        parent_pipe.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _prepare_state(root: Path) -> tuple[Path, tuple[str, ...]]:
    state = root / "state"
    state.mkdir(parents=True)
    with EventStore(state / "runs.db"):
        pass
    store = FileArtifactStore(state / "artifacts", trusted_base=root)

    valid = store.put_bytes(_VALID_BYTES, media_type="application/octet-stream")
    valid_path = store.path_for(valid)

    corrupt_digest = "a" * 64
    corrupt_path = store.root / "sha256" / "aa" / corrupt_digest
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(_CORRUPT_BYTES)

    temporary_path = store.root / "sha256" / "bb" / ".artifact-process-crash"
    temporary_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path.write_bytes(_TEMP_BYTES)

    relative_paths = tuple(
        sorted(
            str(path.relative_to(store.root)) for path in (valid_path, corrupt_path, temporary_path)
        )
    )
    return state, relative_paths


def _entry_names(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir()}


def _load_record[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    data = path.read_bytes()
    model = model_type.model_validate_json(data, strict=True)
    assert data == canonical_json(model).encode("utf-8")
    return model


def _expected_receipt(
    plan: QuarantinePlan,
    candidate: QuarantineCandidate,
) -> QuarantineReceipt:
    identity = candidate.body.source_identity
    return QuarantineReceipt(
        transaction_id=plan.transaction_id,
        plan_sha256=canonical_sha256(plan),
        candidate_id=candidate.candidate_id,
        source_relative_path=candidate.body.source_relative_path,
        destination_relative_path=candidate.destination_relative_path,
        content_sha256=candidate.body.content_sha256,
        size_bytes=identity.size_bytes,
        device=identity.device,
        inode=identity.inode,
        mode=identity.mode,
        mtime_ns=identity.mtime_ns,
    )


def _assert_completion_chain(
    transaction: Path,
    before: QuarantineBefore,
    plan: QuarantinePlan,
    after: QuarantineAfter,
) -> QuarantineComplete:
    assert plan.body.before_sha256 == canonical_sha256(before)
    complete = _load_record(transaction / "COMPLETE.json", QuarantineComplete)
    assert complete.transaction_id == plan.transaction_id
    assert complete.plan_sha256 == canonical_sha256(plan)
    assert complete.before_sha256 == canonical_sha256(before)
    assert complete.after_sha256 == canonical_sha256(after)
    assert len(complete.receipts) == len(plan.body.candidates)
    for commitment, candidate in zip(
        complete.receipts,
        plan.body.candidates,
        strict=True,
    ):
        receipt = _load_record(
            transaction / "receipts" / f"{candidate.candidate_id}.json",
            QuarantineReceipt,
        )
        assert receipt == _expected_receipt(plan, candidate)
        assert commitment.candidate_id == candidate.candidate_id
        assert commitment.receipt_sha256 == canonical_sha256(receipt)
    return complete


def _transaction_directory(state: Path) -> Path:
    transactions = state / "quarantine" / "v1" / "transactions"
    entries = tuple(transactions.iterdir())
    assert len(entries) == 1
    transaction = entries[0]
    assert transaction.is_dir()
    assert _RAW_SHA256.fullmatch(transaction.name) is not None
    return transaction


def _expected_progress(boundary: _Boundary, plan: QuarantinePlan) -> tuple[int, int]:
    rank = _BOUNDARIES.index(boundary)
    if rank <= _BOUNDARIES.index(_Boundary.BEFORE_FIRST_MOVE):
        return 0, 0
    target = _TARGET_FINDING.get(boundary)
    if target is not None:
        target_indices = tuple(
            index
            for index, candidate in enumerate(plan.body.candidates)
            if candidate.body.finding.kind is target
        )
        assert len(target_indices) == 1
        target_index = target_indices[0]
        return target_index + 1, target_index
    if boundary in {
        _Boundary.RECEIPT_PUBLISHED,
        _Boundary.FIRST_RECEIPT_DURABLE,
    }:
        return 1, 1
    return len(plan.body.candidates), len(plan.body.candidates)


def _assert_payload_identity(path: Path, candidate: QuarantineCandidate) -> None:
    metadata = path.lstat()
    expected = candidate.body.source_identity
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_nlink == 1
    assert metadata.st_dev == expected.device
    assert metadata.st_ino == expected.inode
    assert stat.S_IMODE(metadata.st_mode) == expected.mode
    assert metadata.st_size == expected.size_bytes
    assert metadata.st_mtime_ns == expected.mtime_ns
    assert sha256_bytes(path.read_bytes()) == candidate.body.content_sha256


def _assert_crash_prefix(
    state: Path,
    boundary: _Boundary,
    original_sources: tuple[str, ...],
    expected_before_report: StorageIntegrityReport,
) -> tuple[Path, QuarantinePlan | None]:
    rank = _BOUNDARIES.index(boundary)
    version = state / "quarantine" / "v1"
    transaction = _transaction_directory(state)
    payload = transaction / "payload"
    receipts = transaction / "receipts"

    active_expected = 2 <= rank <= 13
    assert _entry_names(version) == (
        {"transactions", "ACTIVE"} if active_expected else {"transactions"}
    )

    expected_transaction_entries = {"BEFORE.json", "payload", "receipts"}
    if rank >= 1:
        expected_transaction_entries.add("PLAN.json")
    if rank >= 10:
        expected_transaction_entries.add("AFTER.json")
    if rank >= 12:
        expected_transaction_entries.add("COMPLETE.json")
    assert _entry_names(transaction) == expected_transaction_entries

    before = _load_record(transaction / "BEFORE.json", QuarantineBefore)
    assert canonical_json(before.report) == canonical_json(expected_before_report)
    assert before.report.artifact_audit is not None
    assert {finding.kind for finding in before.report.artifact_audit.findings} == {
        ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
        ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN,
        ArtifactFindingKind.TEMP_ORPHAN,
    }

    if rank == 0:
        assert _entry_names(payload) == set()
        assert _entry_names(receipts) == set()
        assert all((state / "artifacts" / relative).is_file() for relative in original_sources)
        return transaction, None

    plan = _load_record(transaction / "PLAN.json", QuarantinePlan)
    assert plan.transaction_id == transaction.name
    assert plan.body.before_sha256 == canonical_sha256(before)
    assert len(plan.body.candidates) == 3
    assert {candidate.body.source_relative_path for candidate in plan.body.candidates} == set(
        original_sources
    )
    assert {candidate.body.finding.kind for candidate in plan.body.candidates} == {
        ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
        ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN,
        ArtifactFindingKind.TEMP_ORPHAN,
    }

    if active_expected:
        active = _load_record(version / "ACTIVE", QuarantineActive)
        assert active.transaction_id == plan.transaction_id
        assert active.plan_sha256 == canonical_sha256(plan)
        assert active.before_sha256 == canonical_sha256(before)

    moved_count, receipt_count = _expected_progress(boundary, plan)
    moved = plan.body.candidates[:moved_count]
    receipted = plan.body.candidates[:receipt_count]
    assert _entry_names(payload) == {candidate.candidate_id for candidate in moved}
    assert _entry_names(receipts) == {f"{candidate.candidate_id}.json" for candidate in receipted}
    for index, candidate in enumerate(plan.body.candidates):
        source = state / "artifacts" / candidate.body.source_relative_path
        destination = payload / candidate.candidate_id
        receipt_path = receipts / f"{candidate.candidate_id}.json"
        assert source.exists() is (index >= moved_count)
        assert destination.exists() is (index < moved_count)
        assert receipt_path.exists() is (index < receipt_count)
        if destination.exists():
            _assert_payload_identity(destination, candidate)
        if receipt_path.exists():
            receipt = _load_record(receipt_path, QuarantineReceipt)
            assert receipt == _expected_receipt(plan, candidate)

    if rank >= 10:
        after = _load_record(transaction / "AFTER.json", QuarantineAfter)
        assert after.transaction_id == plan.transaction_id
        assert after.report.clean
    if rank >= 12:
        _assert_completion_chain(transaction, before, plan, after)
    return transaction, plan


_EvidenceIdentity = tuple[int, int, int, int, int, int, int, str]


def _evidence_snapshot(transaction: Path) -> dict[str, _EvidenceIdentity]:
    snapshot: dict[str, _EvidenceIdentity] = {}
    for path in sorted(transaction.rglob("*")):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            continue
        snapshot[str(path.relative_to(transaction))] = (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return snapshot


def _assert_completed_state(
    state: Path,
    expected_transaction: Path,
) -> tuple[QuarantinePlan, QuarantineAfter]:
    version = state / "quarantine" / "v1"
    assert _entry_names(version) == {"transactions"}
    transaction = _transaction_directory(state)
    assert transaction == expected_transaction
    assert _entry_names(transaction) == {
        "BEFORE.json",
        "PLAN.json",
        "payload",
        "receipts",
        "AFTER.json",
        "COMPLETE.json",
    }
    before = _load_record(transaction / "BEFORE.json", QuarantineBefore)
    plan = _load_record(transaction / "PLAN.json", QuarantinePlan)
    assert plan.transaction_id == transaction.name
    after = _load_record(transaction / "AFTER.json", QuarantineAfter)
    complete = _assert_completion_chain(transaction, before, plan, after)
    assert after.transaction_id == plan.transaction_id
    assert after.report.clean
    assert complete.transaction_id == plan.transaction_id

    payload = transaction / "payload"
    receipts = transaction / "receipts"
    assert _entry_names(payload) == {candidate.candidate_id for candidate in plan.body.candidates}
    assert _entry_names(receipts) == {
        f"{candidate.candidate_id}.json" for candidate in plan.body.candidates
    }
    for candidate in plan.body.candidates:
        assert not (state / "artifacts" / candidate.body.source_relative_path).exists()
        _assert_payload_identity(payload / candidate.candidate_id, candidate)
        receipt = _load_record(
            receipts / f"{candidate.candidate_id}.json",
            QuarantineReceipt,
        )
        assert receipt == _expected_receipt(plan, candidate)

    report = audit_storage(state)
    assert report.clean
    assert report.artifact_audit is not None
    assert not report.artifact_audit.findings
    assert canonical_json(after.report) == canonical_json(report)
    return plan, after


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or (sys.platform != "darwin" and not sys.platform.startswith("linux")),
    reason="requires POSIX SIGKILL and a supported atomic no-replace host",
)
@pytest.mark.parametrize("boundary", _BOUNDARIES, ids=lambda boundary: boundary.name.lower())
def test_quarantine_resumes_every_real_process_crash_prefix(
    tmp_path: Path,
    boundary: _Boundary,
) -> None:
    state, original_sources = _prepare_state(tmp_path / boundary.name.lower())
    initial_report = audit_storage(state)
    assert initial_report.quarantine_allowed

    _kill_at_boundary(state, boundary)
    transaction, prefix_plan = _assert_crash_prefix(
        state,
        boundary,
        original_sources,
        initial_report,
    )
    prefix_evidence = _evidence_snapshot(transaction)

    first = _run_quarantine_in_fresh_process(state)
    rank = _BOUNDARIES.index(boundary)
    if rank <= 13:
        assert first.outcome is QuarantineOutcome.COMPLETED
        assert first.transaction_id == transaction.name
        assert first.resumed is (rank >= 2)
        assert first.quarantined_count == 3
    else:
        assert first.outcome is QuarantineOutcome.NO_OP
        assert first.transaction_id is None
        assert first.quarantined_count == 0
        assert first.completion_sha256 is None
    assert first.final_report.clean

    completed_plan, durable_after = _assert_completed_state(state, transaction)
    assert canonical_json(first.final_report) == canonical_json(durable_after.report)
    completed = _load_record(transaction / "COMPLETE.json", QuarantineComplete)
    if first.outcome is QuarantineOutcome.COMPLETED:
        assert first.completion_sha256 == canonical_sha256(completed)
    if prefix_plan is not None:
        assert completed_plan == prefix_plan
    completed_evidence = _evidence_snapshot(transaction)
    for relative_path, identity in prefix_evidence.items():
        assert completed_evidence[relative_path] == identity

    second = _run_quarantine_in_fresh_process(state)
    assert second.outcome is QuarantineOutcome.NO_OP
    assert second.transaction_id is None
    assert second.quarantined_count == 0
    assert second.completion_sha256 is None
    assert second.final_report.clean
    assert _evidence_snapshot(transaction) == completed_evidence
    second_plan, second_after = _assert_completed_state(state, transaction)
    assert second_plan == completed_plan
    assert second_after == durable_after
    assert canonical_json(second.final_report) == canonical_json(durable_after.report)
