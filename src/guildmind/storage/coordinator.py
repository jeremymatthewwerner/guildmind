"""Read-only coordination of the SQLite ledger and filesystem artifact store.

Neither :func:`audit_storage` nor any model in this module initializes, repairs, or
quarantines storage.  Path shape is inspected without following the state, database,
or artifact-root leaf before either storage implementation is constructed.  A valid
database is then held at one verified read snapshot while its committed artifact
graph is checked.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guildmind.domain import canonical_sha256
from guildmind.storage.artifacts import ArtifactCorruptionError, FileArtifactStore
from guildmind.storage.events import (
    EventStore,
    StoreIntegrityError,
    VerifiedRunRoot,
    verified_run_roots_sha256,
)
from guildmind.storage.integrity import (
    ArtifactAudit,
    ArtifactFinding,
    ArtifactFindingKind,
    ArtifactOwner,
    ReachableArtifact,
    audit_artifact_store,
)

_SCHEMA_VERSION: Literal["guildmind.storage-integrity/v1"] = "guildmind.storage-integrity/v1"


class _CoordinatorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StorageIntegrityState(StrEnum):
    """Exhaustive high-level classifications for one local storage pair."""

    UNINITIALIZED = "uninitialized"
    DATABASE_MISSING_WITH_ARTIFACTS = "database_missing_with_artifacts"
    DATABASE_INVALID = "database_invalid"
    INITIALIZED_EMPTY = "initialized_empty"
    INITIALIZED_EMPTY_WITH_UNREFERENCED_FINDINGS = "initialized_empty_with_unreferenced_findings"
    AUDIT_INCOMPLETE = "audit_incomplete"
    REFERENCED_EVIDENCE_INVALID = "referenced_evidence_invalid"
    HEALTHY_WITH_UNREFERENCED_FINDINGS = "healthy_with_unreferenced_findings"
    HEALTHY = "healthy"


class VerifiedRunRootCommitment(_CoordinatorModel):
    """Public, compact commitment to one fully validated run root."""

    run_id: str = Field(min_length=1)
    manifest_revision: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_count: int = Field(ge=1)
    head_event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _root_relation_is_valid(self) -> Self:
        if not self.run_id.strip():
            raise ValueError("verified ledger run ID cannot be blank")
        if self.manifest_revision >= self.event_count:
            raise ValueError("manifest revision must be less than event count")
        return self


class VerifiedLedgerSnapshot(_CoordinatorModel):
    """The exact ledger roots against which an artifact audit was performed."""

    schema_version: Literal["guildmind.verified-run-root-snapshot/v1"] = (
        "guildmind.verified-run-root-snapshot/v1"
    )
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    roots: tuple[VerifiedRunRootCommitment, ...]

    @model_validator(mode="after")
    def _identity_is_canonical(self) -> Self:
        run_ids = tuple(root.run_id for root in self.roots)
        if run_ids != tuple(sorted(set(run_ids))):
            raise ValueError("verified ledger roots must be unique and sorted by run ID")
        expected = canonical_sha256(
            {
                "roots": [root.model_dump(mode="json") for root in self.roots],
                "schema_version": self.schema_version,
            }
        )
        if self.snapshot_sha256 != expected:
            raise ValueError("verified ledger snapshot hash mismatch")
        return self

    def contains_run(self, run_id: str) -> bool:
        return any(root.run_id == run_id for root in self.roots)


class StorageIntegrityReport(_CoordinatorModel):
    """Typed observation and derived operation gates for one state directory.

    This serializable report is evidence, not an authorization capability. A future
    mutating entrypoint must perform its own fresh coordinated audit; it must never
    accept a caller-supplied report as permission to write.
    """

    schema_version: Literal["guildmind.storage-integrity/v1"] = _SCHEMA_VERSION
    state: StorageIntegrityState
    database_present: bool
    artifact_root_present: bool
    ledger_snapshot: VerifiedLedgerSnapshot | None = None
    artifact_audit: ArtifactAudit | None = None
    diagnostic: str | None = None
    initialization_allowed: bool
    references_verified: bool
    read_allowed: bool
    mutation_allowed: bool
    quarantine_allowed: bool
    clean: bool

    @model_validator(mode="after")
    def _state_and_gates_are_derived(self) -> Self:
        derivation = _derive_report(
            database_present=self.database_present,
            artifact_root_present=self.artifact_root_present,
            ledger_snapshot=self.ledger_snapshot,
            artifact_audit=self.artifact_audit,
            diagnostic=self.diagnostic,
        )
        observed = (
            self.state,
            self.initialization_allowed,
            self.references_verified,
            self.read_allowed,
            self.mutation_allowed,
            self.quarantine_allowed,
            self.clean,
        )
        expected = (
            derivation.state,
            derivation.initialization_allowed,
            derivation.references_verified,
            derivation.read_allowed,
            derivation.mutation_allowed,
            derivation.quarantine_allowed,
            derivation.clean,
        )
        if observed != expected:
            raise ValueError("storage integrity state and gates must be derived")
        if (
            self.ledger_snapshot is not None
            and self.artifact_audit is not None
            and self.ledger_snapshot.snapshot_sha256 != self.artifact_audit.snapshot_sha256
        ):
            raise ValueError("artifact audit is not bound to the ledger snapshot")
        return self


@dataclass(frozen=True, slots=True)
class _Derivation:
    state: StorageIntegrityState
    initialization_allowed: bool
    references_verified: bool
    read_allowed: bool
    mutation_allowed: bool
    quarantine_allowed: bool
    clean: bool


class _PathKind(StrEnum):
    MISSING = "missing"
    DIRECTORY = "directory"
    REGULAR = "regular"
    SYMLINK = "symlink"
    OTHER = "other"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class _PathObservation:
    kind: _PathKind
    error_number: int | None = None
    device: int | None = None
    inode: int | None = None


def audit_storage(state_directory: Path) -> StorageIntegrityReport:
    """Inspect one ledger/CAS pair without creating or changing either store."""

    state = Path(os.path.abspath(state_directory))
    state_observation = _observe_path(state)
    if state_observation.kind is _PathKind.MISSING:
        if not _paths_are_unchanged(((state, state_observation),)):
            return _report(
                database_present=False,
                artifact_root_present=False,
                diagnostic="state_directory_changed_during_audit",
            )
        return _report(
            database_present=False,
            artifact_root_present=False,
            diagnostic=None,
        )
    if state_observation.kind is not _PathKind.DIRECTORY:
        return _report(
            database_present=False,
            artifact_root_present=False,
            diagnostic=_path_diagnostic("state_directory", state_observation),
        )

    database_path = state / "runs.db"
    artifact_root = state / "artifacts"
    sidecar_paths = (
        (Path(f"{database_path}-wal"), "database_wal"),
        (Path(f"{database_path}-shm"), "database_shm"),
        (Path(f"{database_path}-journal"), "database_journal"),
    )
    database_observation = _observe_path(database_path)
    artifact_observation = _observe_path(artifact_root)
    sidecar_observations = tuple((path, name, _observe_path(path)) for path, name in sidecar_paths)
    observed_paths = (
        (state, state_observation),
        (database_path, database_observation),
        (artifact_root, artifact_observation),
        *((path, observation) for path, _, observation in sidecar_observations),
    )
    artifact_present = artifact_observation.kind is not _PathKind.MISSING

    if database_observation.kind is _PathKind.MISSING:
        artifact_audit = _audit_without_database(
            state,
            artifact_root,
            artifact_observation,
        )
        diagnostic = None
        if artifact_observation.kind not in {_PathKind.MISSING, _PathKind.DIRECTORY}:
            diagnostic = _path_diagnostic("artifact_root", artifact_observation)
        for _, name, observation in sidecar_observations:
            if observation.kind is not _PathKind.MISSING:
                diagnostic = _path_diagnostic(name, observation)
                break
        if not _paths_are_unchanged(observed_paths):
            return _unstable_report(state, database_path, artifact_root)
        return _report(
            database_present=False,
            artifact_root_present=artifact_present,
            artifact_audit=artifact_audit,
            diagnostic=diagnostic,
        )

    if database_observation.kind is not _PathKind.REGULAR:
        artifact_audit = _audit_without_database(
            state,
            artifact_root,
            artifact_observation,
        )
        if not _paths_are_unchanged(observed_paths):
            return _unstable_report(state, database_path, artifact_root)
        return _report(
            database_present=True,
            artifact_root_present=artifact_present,
            artifact_audit=artifact_audit,
            diagnostic=_path_diagnostic("database", database_observation),
        )

    try:
        with (
            EventStore.open_existing_read_only(
                database_path,
                trusted_base=state.parent,
            ) as event_store,
            event_store.verified_snapshot() as roots,
        ):
            _require_same_path(state, state_observation, "state_directory")
            _require_same_path(database_path, database_observation, "database")
            snapshot = _ledger_snapshot(roots)
            artifact_audit = _audit_for_snapshot(
                roots,
                snapshot,
                state,
                artifact_root,
                artifact_observation,
            )
            _require_same_path(state, state_observation, "state_directory")
            _require_same_path(database_path, database_observation, "database")
            _require_same_path(artifact_root, artifact_observation, "artifact_root")
            for path, name, observation in sidecar_observations:
                _require_same_path(path, observation, name)
    except (OSError, sqlite3.DatabaseError, StoreIntegrityError, ValueError):
        return _report(
            database_present=True,
            artifact_root_present=artifact_present,
            diagnostic="database_integrity_validation_failed",
        )

    return _report(
        database_present=True,
        artifact_root_present=artifact_present,
        ledger_snapshot=snapshot,
        artifact_audit=artifact_audit,
        diagnostic=(
            _path_diagnostic("artifact_root", artifact_observation)
            if artifact_observation.kind not in {_PathKind.MISSING, _PathKind.DIRECTORY}
            else None
        ),
    )


def _audit_for_snapshot(
    roots: tuple[VerifiedRunRoot, ...],
    snapshot: VerifiedLedgerSnapshot,
    state_directory: Path,
    artifact_root: Path,
    observation: _PathObservation,
) -> ArtifactAudit:
    if observation.kind is _PathKind.DIRECTORY:
        try:
            store = FileArtifactStore.open_existing_read_only(
                artifact_root,
                trusted_base=state_directory.parent,
            )
            audit = audit_artifact_store(roots, store)
            _require_same_path(artifact_root, observation, "artifact_root")
        except (ArtifactCorruptionError, OSError, StoreIntegrityError, ValueError):
            return _incomplete_audit(snapshot.snapshot_sha256, "artifact_store_open_failed")
        if audit.snapshot_sha256 != snapshot.snapshot_sha256:
            raise ValueError("artifact audit snapshot binding mismatch")
        return audit

    has_references = any(root.manifest.artifacts for root in roots)
    if observation.kind is _PathKind.MISSING:
        if has_references:
            return _missing_store_audit(roots, snapshot.snapshot_sha256)
        return _empty_audit(snapshot.snapshot_sha256)
    return _incomplete_audit(
        snapshot.snapshot_sha256,
        "artifact_store_path_invalid",
    )


def _audit_without_database(
    state_directory: Path,
    artifact_root: Path,
    observation: _PathObservation,
) -> ArtifactAudit | None:
    if observation.kind is _PathKind.MISSING:
        return None
    snapshot = _ledger_snapshot(())
    if observation.kind is not _PathKind.DIRECTORY:
        return _incomplete_audit(snapshot.snapshot_sha256, "artifact_store_path_invalid")
    try:
        store = FileArtifactStore.open_existing_read_only(
            artifact_root,
            trusted_base=state_directory.parent,
        )
        audit = audit_artifact_store((), store)
        _require_same_path(artifact_root, observation, "artifact_root")
        return audit
    except (ArtifactCorruptionError, OSError, StoreIntegrityError, ValueError):
        return _incomplete_audit(snapshot.snapshot_sha256, "artifact_store_open_failed")


def _ledger_snapshot(roots: tuple[VerifiedRunRoot, ...]) -> VerifiedLedgerSnapshot:
    commitments = tuple(
        VerifiedRunRootCommitment(
            run_id=root.manifest.run_id,
            manifest_revision=root.manifest_revision,
            manifest_sha256=root.manifest_sha256,
            event_count=root.event_count,
            head_event_sha256=root.head_event_sha256,
        )
        for root in sorted(roots, key=lambda item: item.manifest.run_id)
    )
    return VerifiedLedgerSnapshot(
        snapshot_sha256=verified_run_roots_sha256(roots),
        roots=commitments,
    )


def _empty_audit(snapshot_sha256: str) -> ArtifactAudit:
    return ArtifactAudit(
        snapshot_sha256=snapshot_sha256,
        reachable=(),
        findings=(),
        complete=True,
        quarantine_allowed=True,
    )


def _incomplete_audit(snapshot_sha256: str, detail: str) -> ArtifactAudit:
    finding = ArtifactFinding(
        kind=ArtifactFindingKind.SCAN_ERROR,
        relative_path=".",
        detail=detail,
        errno=errno.ENOENT if detail == "artifact_store_missing" else None,
    )
    return ArtifactAudit(
        snapshot_sha256=snapshot_sha256,
        reachable=(),
        findings=(finding,),
        complete=False,
        quarantine_allowed=False,
    )


def _missing_store_audit(
    roots: tuple[VerifiedRunRoot, ...],
    snapshot_sha256: str,
) -> ArtifactAudit:
    references: dict[str, tuple[int, set[str], set[ArtifactOwner]]] = {}
    for root in roots:
        for role, reference in sorted(root.manifest.artifacts.items()):
            owner = ArtifactOwner(run_id=root.manifest.run_id, path=(role,))
            existing = references.get(reference.sha256)
            if existing is None:
                references[reference.sha256] = (
                    reference.size_bytes,
                    {reference.media_type},
                    {owner},
                )
            else:
                existing[1].add(reference.media_type)
                existing[2].add(owner)

    reachable: list[ReachableArtifact] = []
    findings: list[ArtifactFinding] = []
    for digest, (size_bytes, media_types, owners) in sorted(references.items()):
        ordered_owners = tuple(sorted(owners, key=lambda item: (item.run_id, item.path)))
        relative_path = f"sha256/{digest[:2]}/{digest}"
        reachable.append(
            ReachableArtifact(
                sha256=digest,
                size_bytes=size_bytes,
                storage_ref=relative_path,
                media_types=tuple(sorted(media_types)),
                owners=ordered_owners,
                bytes_verified=False,
            )
        )
        findings.append(
            ArtifactFinding(
                kind=ArtifactFindingKind.MISSING_REFERENCED,
                relative_path=relative_path,
                expected_sha256=digest,
                size_bytes=size_bytes,
                detail="artifact_store_missing",
                errno=errno.ENOENT,
                owners=ordered_owners,
            )
        )
    return ArtifactAudit(
        snapshot_sha256=snapshot_sha256,
        reachable=tuple(reachable),
        findings=tuple(findings),
        complete=True,
        quarantine_allowed=False,
    )


def _report(
    *,
    database_present: bool,
    artifact_root_present: bool,
    ledger_snapshot: VerifiedLedgerSnapshot | None = None,
    artifact_audit: ArtifactAudit | None = None,
    diagnostic: str | None,
) -> StorageIntegrityReport:
    derivation = _derive_report(
        database_present=database_present,
        artifact_root_present=artifact_root_present,
        ledger_snapshot=ledger_snapshot,
        artifact_audit=artifact_audit,
        diagnostic=diagnostic,
    )
    return StorageIntegrityReport(
        state=derivation.state,
        database_present=database_present,
        artifact_root_present=artifact_root_present,
        ledger_snapshot=ledger_snapshot,
        artifact_audit=artifact_audit,
        diagnostic=diagnostic,
        initialization_allowed=derivation.initialization_allowed,
        references_verified=derivation.references_verified,
        read_allowed=derivation.read_allowed,
        mutation_allowed=derivation.mutation_allowed,
        quarantine_allowed=derivation.quarantine_allowed,
        clean=derivation.clean,
    )


def _derive_report(
    *,
    database_present: bool,
    artifact_root_present: bool,
    ledger_snapshot: VerifiedLedgerSnapshot | None,
    artifact_audit: ArtifactAudit | None,
    diagnostic: str | None,
) -> _Derivation:
    del artifact_root_present
    if not database_present:
        if diagnostic is not None and diagnostic.startswith("state_directory_"):
            return _Derivation(
                state=StorageIntegrityState.DATABASE_INVALID,
                initialization_allowed=False,
                references_verified=False,
                read_allowed=False,
                mutation_allowed=False,
                quarantine_allowed=False,
                clean=False,
            )
        artifact_content = artifact_audit is not None and (
            bool(artifact_audit.findings) or bool(artifact_audit.reachable)
        )
        invalid_path = diagnostic is not None
        state = (
            StorageIntegrityState.DATABASE_MISSING_WITH_ARTIFACTS
            if artifact_content or invalid_path
            else StorageIntegrityState.UNINITIALIZED
        )
        initialization_allowed = state is StorageIntegrityState.UNINITIALIZED
        return _Derivation(
            state=state,
            initialization_allowed=initialization_allowed,
            references_verified=False,
            read_allowed=False,
            mutation_allowed=False,
            quarantine_allowed=False,
            clean=initialization_allowed,
        )

    if ledger_snapshot is None:
        return _Derivation(
            state=StorageIntegrityState.DATABASE_INVALID,
            initialization_allowed=False,
            references_verified=False,
            read_allowed=False,
            mutation_allowed=False,
            quarantine_allowed=False,
            clean=False,
        )

    if diagnostic is not None:
        return _Derivation(
            state=StorageIntegrityState.AUDIT_INCOMPLETE,
            initialization_allowed=False,
            references_verified=False,
            read_allowed=False,
            mutation_allowed=False,
            quarantine_allowed=False,
            clean=False,
        )

    if artifact_audit is None:
        raise ValueError("a validated database requires a bound artifact audit")
    if artifact_audit.snapshot_sha256 != ledger_snapshot.snapshot_sha256:
        raise ValueError("artifact audit is not bound to the ledger snapshot")

    referenced_failure = any(finding.owners for finding in artifact_audit.findings) or any(
        not artifact.bytes_verified for artifact in artifact_audit.reachable
    )
    references_verified = artifact_audit.complete and not referenced_failure
    if referenced_failure:
        state = StorageIntegrityState.REFERENCED_EVIDENCE_INVALID
    elif not artifact_audit.complete:
        state = StorageIntegrityState.AUDIT_INCOMPLETE
    elif artifact_audit.findings:
        state = (
            StorageIntegrityState.INITIALIZED_EMPTY_WITH_UNREFERENCED_FINDINGS
            if not ledger_snapshot.roots
            else StorageIntegrityState.HEALTHY_WITH_UNREFERENCED_FINDINGS
        )
    elif not ledger_snapshot.roots:
        state = StorageIntegrityState.INITIALIZED_EMPTY
    else:
        state = StorageIntegrityState.HEALTHY

    operational = references_verified
    clean = state in {
        StorageIntegrityState.INITIALIZED_EMPTY,
        StorageIntegrityState.HEALTHY,
    }
    return _Derivation(
        state=state,
        initialization_allowed=False,
        references_verified=references_verified,
        read_allowed=operational,
        mutation_allowed=operational,
        quarantine_allowed=operational and artifact_audit.quarantine_allowed,
        clean=clean,
    )


def _observe_path(path: Path) -> _PathObservation:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return _PathObservation(_PathKind.MISSING)
    except OSError as error:
        return _PathObservation(_PathKind.ERROR, _errno(error))
    if stat.S_ISLNK(metadata.st_mode):
        kind = _PathKind.SYMLINK
    elif stat.S_ISDIR(metadata.st_mode):
        kind = _PathKind.DIRECTORY
    elif stat.S_ISREG(metadata.st_mode):
        kind = _PathKind.REGULAR
    else:
        kind = _PathKind.OTHER
    return _PathObservation(
        kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


def _require_same_path(
    path: Path,
    expected: _PathObservation,
    name: str,
) -> None:
    observed = _observe_path(path)
    if (
        observed.kind is not expected.kind
        or observed.device != expected.device
        or observed.inode != expected.inode
    ):
        raise StoreIntegrityError(f"{name} changed during storage integrity audit")


def _paths_are_unchanged(
    observations: tuple[tuple[Path, _PathObservation], ...],
) -> bool:
    return all(_observe_path(path) == expected for path, expected in observations)


def _unstable_report(
    state: Path,
    database_path: Path,
    artifact_root: Path,
) -> StorageIntegrityReport:
    state_observation = _observe_path(state)
    if state_observation.kind is not _PathKind.DIRECTORY:
        return _report(
            database_present=False,
            artifact_root_present=False,
            diagnostic="state_directory_changed_during_audit",
        )
    return _report(
        database_present=_observe_path(database_path).kind is not _PathKind.MISSING,
        artifact_root_present=_observe_path(artifact_root).kind is not _PathKind.MISSING,
        diagnostic="storage_paths_changed_during_audit",
    )


def _path_diagnostic(name: str, observation: _PathObservation) -> str:
    suffix = f"_errno_{observation.error_number}" if observation.error_number is not None else ""
    return f"{name}_{observation.kind.value}{suffix}"


def _errno(error: OSError) -> int:
    return error.errno if error.errno is not None and error.errno >= 0 else errno.EIO
