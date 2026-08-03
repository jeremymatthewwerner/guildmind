"""Existing-only recovery guarded by a fresh ledger and artifact audit.

No-follow path identities bind the initial audit to the writable operation at every
deterministic hand-off. This does not make several independent pathname checks atomic
against an actively hostile same-UID process. This supported path holds the cooperative
shared maintenance lease across its fresh audit and mutation; direct low-level callers
remain responsible for the same coordination boundary.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from guildmind.domain import EventRecord, RunManifest
from guildmind.runtime.clock import Clock, SystemClock
from guildmind.storage.artifacts import ArtifactCorruptionError, FileArtifactStore
from guildmind.storage.coordinator import (
    StorageIntegrityReport,
    StorageIntegrityState,
    audit_storage,
)
from guildmind.storage.events import (
    EventStore,
    StoreIntegrityError,
    VerifiedRunRoot,
    verified_run_roots_sha256,
)
from guildmind.storage.integrity import audit_artifact_store
from guildmind.storage.maintenance import (
    MaintenanceBusyError,
    MaintenanceIntegrityError,
    MaintenanceLease,
)


class RecoveryDenialReason(StrEnum):
    """Stable reasons that an existing run was not safe to recover."""

    STORAGE_NOT_RECOVERABLE = "storage_not_recoverable"
    RUN_NOT_FOUND = "run_not_found"
    STORAGE_CHANGED = "storage_changed"
    REFERENCED_EVIDENCE_INVALID = "referenced_evidence_invalid"
    MAINTENANCE_BUSY = "maintenance_busy"


class _FixtureTerminalizationOperation(StrEnum):
    RECOVER_INTERRUPTED = "recover_interrupted"
    BUDGET_REFUSAL = "budget_refusal"


class RecoveryDeniedError(RuntimeError):
    """Raised before guarded terminalization can safely commit a state transition."""

    def __init__(
        self,
        reason: RecoveryDenialReason,
        *,
        run_id: str,
        storage_state: StorageIntegrityState | None = None,
    ) -> None:
        self.reason = reason
        self.run_id = run_id
        self.storage_state = storage_state
        super().__init__(f"recovery denied for run {run_id!r}: {reason.value}")


@dataclass(frozen=True, slots=True)
class FixtureRecoveryResult:
    """A terminal manifest and validated event stream from one guarded operation."""

    manifest: RunManifest
    events: tuple[EventRecord, ...]


class RecoveryPostCommitMaintenanceError(RuntimeError):
    """Raised when terminalization committed but its maintenance lease closed unsafely."""

    def __init__(
        self,
        result: FixtureRecoveryResult,
        release_error: MaintenanceIntegrityError,
    ) -> None:
        self.result = result
        self.release_error = release_error
        super().__init__(
            f"recovery for run {result.manifest.run_id!r} committed, "
            "but maintenance lease release failed"
        )


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    file_type: int | None
    device: int | None
    inode: int | None
    error_number: int | None = None


@dataclass(frozen=True, slots=True)
class _StorageIdentity:
    trusted_base: _PathIdentity
    state_directory: _PathIdentity
    database: _PathIdentity
    artifact_root: _PathIdentity
    wal: _PathIdentity
    shm: _PathIdentity
    journal: _PathIdentity


def recover_existing_fixture_run(
    *,
    state_directory: Path,
    run_id: str,
    clock: Clock | None = None,
    terminal_reason: str = "interrupted_run_recovered",
) -> FixtureRecoveryResult:
    """Recover one existing run without initializing or repairing its data stores.

    The serialized storage report is deliberately not an input. Every call performs
    its own fresh audit, then verifies the same complete ledger commitment and all
    recursively reachable artifact bytes again under SQLite's writer window. A valid
    legacy state can gain the persistent empty maintenance-lock inode.
    """

    return _terminalize_existing_fixture_run(
        state_directory=state_directory,
        run_id=run_id,
        clock=clock,
        terminal_reason=terminal_reason,
        operation=_FixtureTerminalizationOperation.RECOVER_INTERRUPTED,
    )


def terminalize_existing_fixture_budget_refusal(
    *,
    state_directory: Path,
    run_id: str,
    clock: Clock | None = None,
    terminal_reason: str = "model_reservation_refused",
) -> FixtureRecoveryResult:
    """Terminalize one pre-dispatch budget refusal behind the full recovery guard."""

    return _terminalize_existing_fixture_run(
        state_directory=state_directory,
        run_id=run_id,
        clock=clock,
        terminal_reason=terminal_reason,
        operation=_FixtureTerminalizationOperation.BUDGET_REFUSAL,
    )


def _terminalize_existing_fixture_run(
    *,
    state_directory: Path,
    run_id: str,
    clock: Clock | None,
    terminal_reason: str,
    operation: _FixtureTerminalizationOperation,
) -> FixtureRecoveryResult:
    try:
        lease = MaintenanceLease.acquire_shared(state_directory)
    except MaintenanceBusyError as error:
        raise RecoveryDeniedError(
            RecoveryDenialReason.MAINTENANCE_BUSY,
            run_id=run_id,
        ) from error
    except MaintenanceIntegrityError as error:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_NOT_RECOVERABLE,
            run_id=run_id,
            storage_state=_diagnostic_storage_state(state_directory),
        ) from error
    try:
        result = _terminalize_existing_fixture_run_under_shared_lease(
            state_directory=state_directory,
            run_id=run_id,
            clock=clock,
            terminal_reason=terminal_reason,
            operation=operation,
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
        raise RecoveryPostCommitMaintenanceError(result, error) from error
    return result


def _diagnostic_storage_state(state_directory: Path) -> StorageIntegrityState | None:
    """Classify a lease-open denial without creating or authorizing storage."""

    state = Path(os.path.abspath(state_directory))
    if state == Path(state.anchor):
        return None
    try:
        return audit_storage(state).state
    except (ArtifactCorruptionError, OSError, StoreIntegrityError, ValueError):
        return None


def _terminalize_existing_fixture_run_under_shared_lease(
    *,
    state_directory: Path,
    run_id: str,
    clock: Clock | None,
    terminal_reason: str,
    operation: _FixtureTerminalizationOperation,
) -> FixtureRecoveryResult:
    state = Path(os.path.abspath(state_directory))
    if state == Path(state.anchor):
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_NOT_RECOVERABLE,
            run_id=run_id,
        )
    selected_clock = clock or SystemClock()
    expected_identity = _capture_storage_identity(state)
    try:
        report = audit_storage(state)
    except (ArtifactCorruptionError, OSError, StoreIntegrityError, ValueError) as error:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_NOT_RECOVERABLE,
            run_id=run_id,
        ) from error
    _require_storage_identity(
        state,
        expected_identity,
        run_id=run_id,
        storage_state=report.state,
        include_sidecars=True,
    )

    if not report.mutation_allowed or not report.references_verified:
        reason = (
            RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID
            if report.state is StorageIntegrityState.REFERENCED_EVIDENCE_INVALID
            else RecoveryDenialReason.STORAGE_NOT_RECOVERABLE
        )
        raise RecoveryDeniedError(
            reason,
            run_id=run_id,
            storage_state=report.state,
        )
    snapshot = report.ledger_snapshot
    if snapshot is None:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_NOT_RECOVERABLE,
            run_id=run_id,
            storage_state=report.state,
        )
    if not snapshot.contains_run(run_id):
        raise RecoveryDeniedError(
            RecoveryDenialReason.RUN_NOT_FOUND,
            run_id=run_id,
            storage_state=report.state,
        )

    return _commit_existing_fixture_terminalization(
        state=state,
        run_id=run_id,
        selected_clock=selected_clock,
        terminal_reason=terminal_reason,
        operation=operation,
        expected_identity=expected_identity,
        report=report,
        snapshot_sha256=snapshot.snapshot_sha256,
    )


def _commit_existing_fixture_terminalization(
    *,
    state: Path,
    run_id: str,
    selected_clock: Clock,
    terminal_reason: str,
    operation: _FixtureTerminalizationOperation,
    expected_identity: _StorageIdentity,
    report: StorageIntegrityReport,
    snapshot_sha256: str,
) -> FixtureRecoveryResult:
    database = state / "runs.db"

    def verify_locked_artifacts(roots: tuple[VerifiedRunRoot, ...]) -> None:
        _require_storage_identity(
            state,
            expected_identity,
            run_id=run_id,
            storage_state=report.state,
            include_sidecars=False,
        )
        _verify_locked_artifacts(
            state_directory=state,
            run_id=run_id,
            roots=roots,
        )
        _require_storage_identity(
            state,
            expected_identity,
            run_id=run_id,
            storage_state=report.state,
            include_sidecars=False,
        )

    try:
        _require_storage_identity(
            state,
            expected_identity,
            run_id=run_id,
            storage_state=report.state,
            include_sidecars=True,
        )
        with EventStore.open_existing_writable(
            database,
            clock=selected_clock,
            trusted_base=state.parent,
        ) as event_store:
            _require_storage_identity(
                state,
                expected_identity,
                run_id=run_id,
                storage_state=report.state,
                include_sidecars=False,
            )
            finished_at = selected_clock.stamp().occurred_at
            if operation is _FixtureTerminalizationOperation.RECOVER_INTERRUPTED:
                terminalized = event_store.recover_run_with_events(
                    run_id,
                    finished_at=finished_at,
                    terminal_reason=terminal_reason,
                    expected_snapshot_sha256=snapshot_sha256,
                    integrity_guard=verify_locked_artifacts,
                )
            else:
                terminalized = event_store.complete_budget_exhaustion_with_events(
                    run_id,
                    finished_at=finished_at,
                    terminal_reason=terminal_reason,
                    expected_snapshot_sha256=snapshot_sha256,
                    integrity_guard=verify_locked_artifacts,
                )
    except RecoveryDeniedError:
        raise
    except (
        ArtifactCorruptionError,
        KeyError,
        OSError,
        StoreIntegrityError,
        ValueError,
        sqlite3.DatabaseError,
    ) as error:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_CHANGED,
            run_id=run_id,
            storage_state=report.state,
        ) from error

    return FixtureRecoveryResult(manifest=terminalized.manifest, events=terminalized.events)


def _capture_storage_identity(state_directory: Path) -> _StorageIdentity:
    database = state_directory / "runs.db"
    return _StorageIdentity(
        trusted_base=_observe_path(state_directory.parent),
        state_directory=_observe_path(state_directory),
        database=_observe_path(database),
        artifact_root=_observe_path(state_directory / "artifacts"),
        wal=_observe_path(Path(f"{database}-wal")),
        shm=_observe_path(Path(f"{database}-shm")),
        journal=_observe_path(Path(f"{database}-journal")),
    )


def _observe_path(path: Path) -> _PathIdentity:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return _PathIdentity(file_type=None, device=None, inode=None)
    except OSError as error:
        return _PathIdentity(
            file_type=None,
            device=None,
            inode=None,
            error_number=error.errno if error.errno is not None else -1,
        )
    return _PathIdentity(
        file_type=stat.S_IFMT(metadata.st_mode),
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _require_storage_identity(
    state_directory: Path,
    expected: _StorageIdentity,
    *,
    run_id: str,
    storage_state: StorageIntegrityState,
    include_sidecars: bool,
) -> None:
    observed = _capture_storage_identity(state_directory)
    stable = (
        observed.trusted_base == expected.trusted_base
        and observed.state_directory == expected.state_directory
        and observed.database == expected.database
        and observed.artifact_root == expected.artifact_root
    )
    if include_sidecars:
        stable = stable and (
            observed.wal == expected.wal
            and observed.shm == expected.shm
            and observed.journal == expected.journal
        )
    if not stable:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_CHANGED,
            run_id=run_id,
            storage_state=storage_state,
        )


def _verify_locked_artifacts(
    *,
    state_directory: Path,
    run_id: str,
    roots: tuple[VerifiedRunRoot, ...],
) -> None:
    artifact_root = state_directory / "artifacts"
    expected_snapshot_sha256 = verified_run_roots_sha256(roots)
    has_references = any(root.manifest.artifacts for root in roots)
    if not os.path.lexists(artifact_root):
        if has_references:
            raise RecoveryDeniedError(
                RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID,
                run_id=run_id,
            )
        return

    try:
        artifact_store = FileArtifactStore.open_existing_read_only(
            artifact_root,
            trusted_base=state_directory.parent,
        )
        audit = audit_artifact_store(roots, artifact_store)
    except (ArtifactCorruptionError, OSError, StoreIntegrityError, ValueError) as error:
        raise RecoveryDeniedError(
            RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID,
            run_id=run_id,
        ) from error

    if audit.snapshot_sha256 != expected_snapshot_sha256:
        raise RecoveryDeniedError(
            RecoveryDenialReason.STORAGE_CHANGED,
            run_id=run_id,
        )
    referenced_failure = any(finding.owners for finding in audit.findings) or any(
        not artifact.bytes_verified for artifact in audit.reachable
    )
    if not audit.complete or referenced_failure:
        raise RecoveryDeniedError(
            RecoveryDenialReason.REFERENCED_EVIDENCE_INVALID,
            run_id=run_id,
        )
