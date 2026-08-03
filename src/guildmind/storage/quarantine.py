"""Crash-resumable quarantine for unreferenced regular CAS files.

The quarantine protocol is deliberately narrower than the read-only artifact audit.
Version 1 moves only ownerless finalized or temporary regular files.  Links, special
files, malformed namespace entries, incomplete scans, and referenced findings deny the
whole operation before a durable fence is created.

Every mutating path holds the state-wide maintenance lease exclusively.  A canonical,
content-addressed plan is made durable before ``quarantine/v1/ACTIVE`` is published.
Once that marker exists, every error leaves it in place.  Restart reconciliation is
forward-only: each candidate must exist at exactly its planned source or destination,
and a missing deterministic receipt can be reconstructed from the plan.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from enum import StrEnum
from pathlib import Path, PurePosixPath
from secrets import token_hex
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from guildmind.domain import canonical_json, canonical_sha256, sha256_bytes
from guildmind.storage._fsops import rename_noreplace_at
from guildmind.storage.coordinator import StorageIntegrityReport, audit_storage
from guildmind.storage.integrity import ArtifactFinding, ArtifactFindingKind
from guildmind.storage.maintenance import (
    MaintenanceBusyError,
    MaintenanceIntegrityError,
    MaintenanceLease,
)

Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]

_ACTIVE_NAME = "ACTIVE"
_QUARANTINE_NAME = "quarantine"
_VERSION_NAME = "v1"
_TRANSACTIONS_NAME = "transactions"
_BEFORE_NAME = "BEFORE.json"
_PLAN_NAME = "PLAN.json"
_PAYLOAD_NAME = "payload"
_RECEIPTS_NAME = "receipts"
_AFTER_NAME = "AFTER.json"
_COMPLETE_NAME = "COMPLETE.json"

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)

_FINALIZED_PATH = re.compile(r"^sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
_TEMP_PATH = re.compile(r"^sha256/[0-9a-f]{2}/\.artifact-[^/]+$")
_TRANSACTION_NAME = re.compile(r"^[0-9a-f]{64}$")
_RECORD_TEMP = re.compile(
    r"^\.(BEFORE\.json|PLAN\.json|AFTER\.json|COMPLETE\.json|ACTIVE|[0-9a-f]{64}\.json)"
    r"\.tmp-[0-9a-f]{32}$"
)

_MAX_CANDIDATES = 16_384
_MAX_DIRECTORY_ENTRIES = 65_536
_MAX_CANDIDATE_BYTES = 268_435_456
_MAX_TOTAL_HASHED_BYTES = 1_073_741_824
_MAX_ACTIVE_BYTES = 4_096
_MAX_RECORD_BYTES = 67_108_864
_READ_CHUNK_BYTES = 1_048_576

_ALLOWED_FINDINGS = frozenset(
    {
        ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
        ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN,
        ArtifactFindingKind.TEMP_ORPHAN,
    }
)


class _QuarantineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class QuarantineOutcome(StrEnum):
    """Stable successful outcomes for one invocation."""

    NO_OP = "no_op"
    COMPLETED = "completed"


class QuarantineFailureReason(StrEnum):
    """Stable fail-closed reasons for quarantine denial or interruption."""

    MAINTENANCE_DENIED = "maintenance_denied"
    STORAGE_NOT_QUARANTINABLE = "storage_not_quarantinable"
    UNSUPPORTED_FINDING = "unsupported_finding"
    CANDIDATE_LIMIT_EXCEEDED = "candidate_limit_exceeded"
    HASH_LIMIT_EXCEEDED = "hash_limit_exceeded"
    NAMESPACE_INVALID = "namespace_invalid"
    ACTIVE_INVALID = "active_invalid"
    RECORD_INVALID = "record_invalid"
    PLAN_CHANGED = "plan_changed"
    LEDGER_CHANGED = "ledger_changed"
    REACHABLE_GRAPH_CHANGED = "reachable_graph_changed"
    CANDIDATE_CHANGED = "candidate_changed"
    CANDIDATE_AMBIGUOUS = "candidate_ambiguous"
    DESTINATION_COLLISION = "destination_collision"
    CROSS_DEVICE = "cross_device"
    FINAL_AUDIT_FAILED = "final_audit_failed"
    IO_ERROR = "io_error"


class DirectoryIdentity(_QuarantineModel):
    device: NonNegativeInt
    inode: PositiveInt


class DatabaseIdentity(_QuarantineModel):
    device: NonNegativeInt
    inode: PositiveInt


class FileIdentity(_QuarantineModel):
    file_type: Literal["regular"] = "regular"
    device: NonNegativeInt
    inode: PositiveInt
    mode: NonNegativeInt
    link_count: PositiveInt
    size_bytes: NonNegativeInt
    mtime_ns: NonNegativeInt
    ctime_ns: NonNegativeInt

    @model_validator(mode="after")
    def _is_single_link(self) -> Self:
        if self.link_count != 1:
            raise ValueError("quarantine candidates must be single-link regular files")
        return self


class QuarantineCandidateBody(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-candidate-body/v1"] = (
        "guildmind.quarantine-candidate-body/v1"
    )
    finding: ArtifactFinding
    source_relative_path: str = Field(min_length=1)
    source_identity: FileIdentity
    content_sha256: Sha256

    @model_validator(mode="after")
    def _finding_and_path_are_safe(self) -> Self:
        if self.finding.kind not in _ALLOWED_FINDINGS or self.finding.owners:
            raise ValueError("quarantine candidate finding is not an ownerless v1 orphan")
        if self.source_relative_path != self.finding.relative_path:
            raise ValueError("candidate source path must match its audit finding")
        if self.finding.size_bytes != self.source_identity.size_bytes:
            raise ValueError("candidate size must match its audit finding")
        if self.finding.kind is ArtifactFindingKind.TEMP_ORPHAN:
            if _TEMP_PATH.fullmatch(self.source_relative_path) is None:
                raise ValueError("temporary candidate path is not canonical")
        else:
            matched = _FINALIZED_PATH.fullmatch(self.source_relative_path)
            if matched is None or matched.group(2).startswith(matched.group(1)) is False:
                raise ValueError("finalized candidate path is not canonical")
            if self.finding.expected_sha256 != matched.group(2):
                raise ValueError("finalized candidate digest does not match its path")
            if self.finding.observed_sha256 != self.content_sha256:
                raise ValueError("finalized candidate content hash changed after audit")
        return self


class QuarantineCandidate(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-candidate/v1"] = (
        "guildmind.quarantine-candidate/v1"
    )
    candidate_id: Sha256
    body: QuarantineCandidateBody

    @model_validator(mode="after")
    def _id_is_content_addressed(self) -> Self:
        if self.candidate_id != canonical_sha256(self.body):
            raise ValueError("quarantine candidate ID does not match its body")
        return self

    @property
    def destination_relative_path(self) -> str:
        return f"{_PAYLOAD_NAME}/{self.candidate_id}"


class QuarantineBefore(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-before/v1"] = "guildmind.quarantine-before/v1"
    report: StorageIntegrityReport


class QuarantinePlanBody(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-plan-body/v1"] = (
        "guildmind.quarantine-plan-body/v1"
    )
    before_sha256: Sha256
    ledger_snapshot_sha256: Sha256
    reachable_sha256: Sha256
    state_identity: DirectoryIdentity
    database_identity: DatabaseIdentity
    artifact_root_identity: DirectoryIdentity
    candidates: tuple[QuarantineCandidate, ...]

    @model_validator(mode="after")
    def _candidates_are_sorted_unique(self) -> Self:
        candidate_ids = tuple(candidate.candidate_id for candidate in self.candidates)
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("quarantine candidates must be unique and sorted by ID")
        paths = tuple(candidate.body.source_relative_path for candidate in self.candidates)
        if len(paths) != len(set(paths)):
            raise ValueError("quarantine candidate paths must be unique")
        if not self.candidates:
            raise ValueError("a quarantine plan requires at least one candidate")
        if len(self.candidates) > _MAX_CANDIDATES:
            raise ValueError("quarantine plan exceeds the candidate-count bound")
        sizes = tuple(candidate.body.source_identity.size_bytes for candidate in self.candidates)
        if any(size > _MAX_CANDIDATE_BYTES for size in sizes):
            raise ValueError("quarantine plan exceeds the per-candidate byte bound")
        if sum(sizes) > _MAX_TOTAL_HASHED_BYTES:
            raise ValueError("quarantine plan exceeds the total hashed-byte bound")
        expected_device = self.state_identity.device
        if self.artifact_root_identity.device != expected_device or any(
            candidate.body.source_identity.device != expected_device
            for candidate in self.candidates
        ):
            raise ValueError("quarantine plan candidates and roots must share the state filesystem")
        return self


class QuarantinePlan(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-plan/v1"] = "guildmind.quarantine-plan/v1"
    transaction_id: Sha256
    body: QuarantinePlanBody

    @model_validator(mode="after")
    def _id_is_content_addressed(self) -> Self:
        if self.transaction_id != canonical_sha256(self.body):
            raise ValueError("quarantine transaction ID does not match its plan body")
        return self


class QuarantineActive(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-active/v1"] = "guildmind.quarantine-active/v1"
    transaction_id: Sha256
    plan_sha256: Sha256
    before_sha256: Sha256


class QuarantineReceipt(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-receipt/v1"] = "guildmind.quarantine-receipt/v1"
    transaction_id: Sha256
    plan_sha256: Sha256
    candidate_id: Sha256
    source_relative_path: str = Field(min_length=1)
    destination_relative_path: str = Field(min_length=1)
    content_sha256: Sha256
    size_bytes: NonNegativeInt
    device: NonNegativeInt
    inode: PositiveInt
    mode: NonNegativeInt
    link_count: Literal[1] = 1
    mtime_ns: NonNegativeInt


class QuarantineAfter(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-after/v1"] = "guildmind.quarantine-after/v1"
    transaction_id: Sha256
    report: StorageIntegrityReport


class ReceiptCommitment(_QuarantineModel):
    candidate_id: Sha256
    receipt_sha256: Sha256


class QuarantineComplete(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-complete/v1"] = "guildmind.quarantine-complete/v1"
    transaction_id: Sha256
    plan_sha256: Sha256
    before_sha256: Sha256
    after_sha256: Sha256
    receipts: tuple[ReceiptCommitment, ...]

    @model_validator(mode="after")
    def _receipts_are_sorted_unique(self) -> Self:
        candidate_ids = tuple(item.candidate_id for item in self.receipts)
        if candidate_ids != tuple(sorted(set(candidate_ids))):
            raise ValueError("receipt commitments must be unique and sorted")
        return self


class QuarantineResult(_QuarantineModel):
    schema_version: Literal["guildmind.quarantine-result/v1"] = "guildmind.quarantine-result/v1"
    outcome: QuarantineOutcome
    transaction_id: Sha256 | None = None
    resumed: bool
    quarantined_count: NonNegativeInt
    final_report: StorageIntegrityReport
    completion_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def _outcome_fields_agree(self) -> Self:
        if self.outcome is QuarantineOutcome.NO_OP:
            if (
                self.transaction_id is not None
                or self.quarantined_count != 0
                or self.completion_sha256 is not None
                or self.resumed
            ):
                raise ValueError("no-op quarantine result contains transaction fields")
        elif (
            self.transaction_id is None
            or self.quarantined_count <= 0
            or self.completion_sha256 is None
        ):
            raise ValueError("completed quarantine result lacks transaction fields")
        return self


class QuarantineDeniedError(RuntimeError):
    """Raised when this invocation obtained no move authority and moved no CAS entry."""

    def __init__(
        self,
        reason: QuarantineFailureReason,
        *,
        state_directory: Path,
        detail: str,
    ) -> None:
        self.reason = reason
        self.state_directory = state_directory
        self.detail = detail
        super().__init__(f"quarantine denied for {state_directory}: {reason.value}: {detail}")


class QuarantineIncompleteError(RuntimeError):
    """Raised when ACTIVE is present or its absence cannot safely be proven.

    Shared mutation remains fenced until a later invocation safely resolves the state.
    """

    def __init__(
        self,
        reason: QuarantineFailureReason,
        *,
        state_directory: Path,
        transaction_id: str | None,
        detail: str,
    ) -> None:
        self.reason = reason
        self.state_directory = state_directory
        self.transaction_id = transaction_id
        self.detail = detail
        context = transaction_id if transaction_id is not None else "unknown"
        super().__init__(
            f"quarantine incomplete for {state_directory} transaction {context}: "
            f"{reason.value}: {detail}"
        )


class QuarantineFinalizationError(RuntimeError):
    """Raised when final cleanup fails after an authoritative result exists.

    For ``COMPLETED``, the COMPLETE evidence and result remain authoritative, but an
    ACTIVE unlink may have succeeded without proven parent-directory durability or
    later descriptor/lease cleanup. For ``NO_OP``, no quarantine transaction was
    created, but final descriptor/lease cleanup was not proven. Callers must re-invoke
    or inspect the state rather than inferring rollback or durable unfencing.
    """

    def __init__(self, result: QuarantineResult, *, detail: str) -> None:
        self.result = result
        self.detail = detail
        super().__init__(f"quarantine result is authoritative but finalization failed: {detail}")


class _ProtocolFailure(RuntimeError):
    def __init__(self, reason: QuarantineFailureReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


class _CommittedFailure(RuntimeError):
    def __init__(self, result: QuarantineResult, detail: str) -> None:
        self.result = result
        self.detail = detail
        super().__init__(detail)


class _HashBudget:
    def __init__(self) -> None:
        self.remaining = _MAX_TOTAL_HASHED_BYTES

    def consume(self, amount: int) -> None:
        if amount > self.remaining:
            raise _ProtocolFailure(
                QuarantineFailureReason.HASH_LIMIT_EXCEEDED,
                "quarantine candidate hashing exceeded the total byte limit",
            )
        self.remaining -= amount


def quarantine_orphans(state_directory: Path) -> QuarantineResult:
    """Start or resume the sole fenced orphan-quarantine transaction.

    The function performs its own fresh audit under an exclusive maintenance lease.
    Serialized reports or plans supplied by a caller are never accepted as authority.
    """

    state = Path(os.path.abspath(state_directory))
    try:
        lease = MaintenanceLease.acquire_exclusive(state)
    except (MaintenanceBusyError, MaintenanceIntegrityError) as error:
        raise QuarantineDeniedError(
            QuarantineFailureReason.MAINTENANCE_DENIED,
            state_directory=state,
            detail=str(error),
        ) from error

    result: QuarantineResult | None = None
    try:
        with (
            lease,
            lease.verified_state_descriptor(require_exclusive=True) as state_descriptor,
        ):
            result = _run_with_exclusive_lease(state, state_descriptor)
    except (QuarantineDeniedError, QuarantineIncompleteError, QuarantineFinalizationError):
        raise
    except _CommittedFailure as error:
        raise QuarantineFinalizationError(error.result, detail=error.detail) from error
    except (MaintenanceBusyError, MaintenanceIntegrityError, OSError) as error:
        if result is not None:
            raise QuarantineFinalizationError(result, detail=str(error)) from error
        raise QuarantineDeniedError(
            QuarantineFailureReason.MAINTENANCE_DENIED,
            state_directory=state,
            detail=str(error),
        ) from error
    if result is None:
        raise AssertionError("quarantine completed without a result")
    return result


def _run_with_exclusive_lease(state: Path, state_descriptor: int) -> QuarantineResult:
    try:
        active = _read_active(state_descriptor)
    except _ProtocolFailure as error:
        if _active_path_exists(state_descriptor):
            raise QuarantineIncompleteError(
                error.reason,
                state_directory=state,
                transaction_id=None,
                detail=error.detail,
            ) from error
        raise QuarantineDeniedError(
            error.reason,
            state_directory=state,
            detail=error.detail,
        ) from error
    except OSError as error:
        detail = f"ACTIVE could not be inspected: {error}"
        if _active_path_exists(state_descriptor):
            raise QuarantineIncompleteError(
                QuarantineFailureReason.ACTIVE_INVALID,
                state_directory=state,
                transaction_id=None,
                detail=detail,
            ) from error
        raise QuarantineDeniedError(
            QuarantineFailureReason.ACTIVE_INVALID,
            state_directory=state,
            detail=detail,
        ) from error

    if active is not None:
        try:
            return _resume_transaction(state, state_descriptor, active, resumed=True)
        except QuarantineIncompleteError:
            raise
        except _ProtocolFailure as error:
            raise QuarantineIncompleteError(
                error.reason,
                state_directory=state,
                transaction_id=active.transaction_id,
                detail=error.detail,
            ) from error
        except (OSError, ValidationError, ValueError) as error:
            raise QuarantineIncompleteError(
                QuarantineFailureReason.IO_ERROR,
                state_directory=state,
                transaction_id=active.transaction_id,
                detail=str(error),
            ) from error

    try:
        prepared = _prepare_fresh_plan(state, state_descriptor)
    except _ProtocolFailure as error:
        raise QuarantineDeniedError(
            error.reason,
            state_directory=state,
            detail=error.detail,
        ) from error
    except (OSError, ValidationError, ValueError) as error:
        raise QuarantineDeniedError(
            QuarantineFailureReason.IO_ERROR,
            state_directory=state,
            detail=str(error),
        ) from error

    if isinstance(prepared, QuarantineResult):
        return prepared
    before, plan = prepared
    active = QuarantineActive(
        transaction_id=plan.transaction_id,
        plan_sha256=canonical_sha256(plan),
        before_sha256=plan.body.before_sha256,
    )
    try:
        _prepare_transaction_records(state_descriptor, before, plan)
        _publish_active(state_descriptor, active)
    except BaseException as error:
        if _active_path_exists(state_descriptor):
            detail = error.detail if isinstance(error, _ProtocolFailure) else str(error)
            reason = (
                error.reason
                if isinstance(error, _ProtocolFailure)
                else QuarantineFailureReason.IO_ERROR
            )
            raise QuarantineIncompleteError(
                reason,
                state_directory=state,
                transaction_id=plan.transaction_id,
                detail=detail,
            ) from error
        if isinstance(error, _ProtocolFailure):
            raise QuarantineDeniedError(
                error.reason,
                state_directory=state,
                detail=error.detail,
            ) from error
        raise QuarantineDeniedError(
            QuarantineFailureReason.IO_ERROR,
            state_directory=state,
            detail=str(error),
        ) from error

    try:
        return _resume_transaction(state, state_descriptor, active, resumed=False)
    except QuarantineFinalizationError:
        raise
    except _CommittedFailure:
        raise
    except _ProtocolFailure as error:
        raise QuarantineIncompleteError(
            error.reason,
            state_directory=state,
            transaction_id=plan.transaction_id,
            detail=error.detail,
        ) from error
    except (OSError, ValidationError, ValueError) as error:
        raise QuarantineIncompleteError(
            QuarantineFailureReason.IO_ERROR,
            state_directory=state,
            transaction_id=plan.transaction_id,
            detail=str(error),
        ) from error


def _prepare_fresh_plan(
    state: Path,
    state_descriptor: int,
) -> tuple[QuarantineBefore, QuarantinePlan] | QuarantineResult:
    report = audit_storage(state)
    if report.clean and report.artifact_audit is not None and not report.artifact_audit.findings:
        return QuarantineResult(
            outcome=QuarantineOutcome.NO_OP,
            resumed=False,
            quarantined_count=0,
            final_report=report,
        )
    _require_quarantinable(report)
    audit = report.artifact_audit
    snapshot = report.ledger_snapshot
    if audit is None or snapshot is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            "quarantinable storage lacks its bound audit or ledger snapshot",
        )
    if len(audit.findings) > _MAX_CANDIDATES:
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_LIMIT_EXCEEDED,
            "quarantine candidate count exceeds the v1 bound",
        )
    paths = tuple(finding.relative_path for finding in audit.findings)
    if len(paths) != len(set(paths)):
        raise _ProtocolFailure(
            QuarantineFailureReason.UNSUPPORTED_FINDING,
            "multiple findings identify one candidate path",
        )

    state_identity = _directory_identity(os.fstat(state_descriptor))
    database_identity = _database_identity_at(state_descriptor)
    with _open_existing_directory_at(state_descriptor, "artifacts") as artifact_descriptor:
        artifact_identity = _directory_identity(os.fstat(artifact_descriptor))
        if artifact_identity.device != state_identity.device:
            raise _ProtocolFailure(
                QuarantineFailureReason.CROSS_DEVICE,
                "artifact and quarantine roots are on different filesystems",
            )
        budget = _HashBudget()
        candidates = tuple(
            sorted(
                (
                    _candidate_from_finding(artifact_descriptor, finding, budget)
                    for finding in audit.findings
                ),
                key=lambda candidate: candidate.candidate_id,
            )
        )
        if any(
            candidate.body.source_identity.device != state_identity.device
            for candidate in candidates
        ):
            raise _ProtocolFailure(
                QuarantineFailureReason.CROSS_DEVICE,
                "a quarantine candidate is not on the state filesystem",
            )

    # Candidate hashing is not part of the read-only audit. Repeat that audit after
    # observation so the durable plan is bound to an unchanged authoritative view.
    repeated = audit_storage(state)
    if canonical_sha256(repeated) != canonical_sha256(report):
        raise _ProtocolFailure(
            QuarantineFailureReason.PLAN_CHANGED,
            "storage changed while the deterministic quarantine plan was prepared",
        )
    if _database_identity_at(state_descriptor) != database_identity:
        raise _ProtocolFailure(
            QuarantineFailureReason.PLAN_CHANGED,
            "database identity changed while the quarantine plan was prepared",
        )
    before = QuarantineBefore(report=report)
    body = QuarantinePlanBody(
        before_sha256=canonical_sha256(before),
        ledger_snapshot_sha256=snapshot.snapshot_sha256,
        reachable_sha256=_reachable_sha256(audit.reachable),
        state_identity=state_identity,
        database_identity=database_identity,
        artifact_root_identity=artifact_identity,
        candidates=candidates,
    )
    return before, QuarantinePlan(transaction_id=canonical_sha256(body), body=body)


def _require_quarantinable(report: StorageIntegrityReport) -> None:
    if not report.quarantine_allowed or report.artifact_audit is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            f"storage state {report.state.value} does not authorize quarantine",
        )
    if not report.artifact_audit.findings:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            "storage is not clean but has no actionable artifact finding",
        )
    unsupported = tuple(
        finding
        for finding in report.artifact_audit.findings
        if finding.kind not in _ALLOWED_FINDINGS or finding.owners
    )
    if unsupported:
        kinds = ",".join(sorted({finding.kind.value for finding in unsupported}))
        raise _ProtocolFailure(
            QuarantineFailureReason.UNSUPPORTED_FINDING,
            f"v1 cannot quarantine the complete finding set: {kinds}",
        )


def _candidate_from_finding(
    artifact_descriptor: int,
    finding: ArtifactFinding,
    budget: _HashBudget,
) -> QuarantineCandidate:
    if finding.kind not in _ALLOWED_FINDINGS or finding.owners:
        raise _ProtocolFailure(
            QuarantineFailureReason.UNSUPPORTED_FINDING,
            f"unsupported quarantine finding at {finding.relative_path}",
        )
    identity, content_sha256 = _observe_candidate(
        artifact_descriptor,
        finding.relative_path,
        budget=budget,
        expected_identity=None,
        expected_sha256=None,
        sync_file=False,
    )
    body = QuarantineCandidateBody(
        finding=finding,
        source_relative_path=finding.relative_path,
        source_identity=identity,
        content_sha256=content_sha256,
    )
    return QuarantineCandidate(candidate_id=canonical_sha256(body), body=body)


def _prepare_transaction_records(
    state_descriptor: int,
    before: QuarantineBefore,
    plan: QuarantinePlan,
) -> None:
    with ExitStack() as stack:
        _, version, transaction, payload, receipts = _open_transaction_directories(
            stack,
            state_descriptor,
            plan.transaction_id,
            create=True,
        )
        del version
        _clean_known_record_temps(transaction)
        _clean_known_record_temps(receipts)
        _require_directory_empty(payload, "prepared transaction payload")
        _require_directory_empty(receipts, "prepared transaction receipts")
        _require_entry_absent(transaction, _AFTER_NAME)
        _require_entry_absent(transaction, _COMPLETE_NAME)
        _write_immutable_model(transaction, _BEFORE_NAME, before)
        _write_immutable_model(transaction, _PLAN_NAME, plan)
        allowed = {_BEFORE_NAME, _PLAN_NAME, _PAYLOAD_NAME, _RECEIPTS_NAME}
        _require_exact_entries(transaction, allowed, "prepared transaction")
        os.fsync(transaction)


def _publish_active(state_descriptor: int, active: QuarantineActive) -> None:
    with ExitStack() as stack:
        _, version, _, _, _ = _open_transaction_directories(
            stack,
            state_descriptor,
            active.transaction_id,
            create=False,
        )
        _clean_known_record_temps(version)
        _require_exact_entries(version, {_TRANSACTIONS_NAME}, "prepared quarantine version")
        _write_immutable_model(version, _ACTIVE_NAME, active, maximum=_MAX_ACTIVE_BYTES)
        _require_exact_entries(
            version,
            {_TRANSACTIONS_NAME, _ACTIVE_NAME},
            "active quarantine version",
        )
        os.fsync(version)


def _resume_transaction(
    state: Path,
    state_descriptor: int,
    active: QuarantineActive,
    *,
    resumed: bool,
) -> QuarantineResult:
    result: QuarantineResult | None = None
    unfenced = False
    try:
        with ExitStack() as stack:
            _, version, transaction, payload, receipts = _open_transaction_directories(
                stack,
                state_descriptor,
                active.transaction_id,
                create=False,
            )
            _require_exact_entries(
                version,
                {_TRANSACTIONS_NAME, _ACTIVE_NAME},
                "active quarantine version",
            )
            before, _ = _read_model(transaction, _BEFORE_NAME, QuarantineBefore)
            plan, plan_sha256 = _read_model(transaction, _PLAN_NAME, QuarantinePlan)
            if (
                plan.transaction_id != active.transaction_id
                or plan_sha256 != active.plan_sha256
                or plan.body.before_sha256 != active.before_sha256
            ):
                raise _ProtocolFailure(
                    QuarantineFailureReason.RECORD_INVALID,
                    "ACTIVE does not bind the durable quarantine plan",
                )
            if canonical_sha256(before) != plan.body.before_sha256:
                raise _ProtocolFailure(
                    QuarantineFailureReason.RECORD_INVALID,
                    "BEFORE does not match the durable quarantine plan",
                )
            _require_before_matches_plan(before, plan)
            _clean_known_record_temps(transaction)
            _clean_known_record_temps(receipts)
            _require_expected_transaction_entries(transaction)
            _require_plan_storage_identity(state_descriptor, plan)
            with _open_existing_directory_at(
                state_descriptor,
                "artifacts",
            ) as artifact_descriptor:
                statuses = _reconcile_candidates(
                    artifact_descriptor,
                    payload,
                    receipts,
                    plan,
                    plan_sha256,
                )
                current = audit_storage(state)
                _require_current_report(plan, current, statuses)
                for candidate, pending in zip(plan.body.candidates, statuses, strict=True):
                    if not pending:
                        continue
                    _move_candidate(
                        artifact_descriptor,
                        payload,
                        receipts,
                        plan,
                        plan_sha256,
                        candidate,
                    )

            final_report = audit_storage(state)
            _require_final_report(plan, final_report)
            after = QuarantineAfter(transaction_id=plan.transaction_id, report=final_report)
            after_sha256 = _write_immutable_model(transaction, _AFTER_NAME, after)
            commitments = tuple(
                ReceiptCommitment(
                    candidate_id=candidate.candidate_id,
                    receipt_sha256=_read_expected_receipt(
                        receipts,
                        _expected_receipt(plan, plan_sha256, candidate),
                    ),
                )
                for candidate in plan.body.candidates
            )
            complete = QuarantineComplete(
                transaction_id=plan.transaction_id,
                plan_sha256=plan_sha256,
                before_sha256=plan.body.before_sha256,
                after_sha256=after_sha256,
                receipts=commitments,
            )
            completion_sha256 = _write_immutable_model(transaction, _COMPLETE_NAME, complete)
            _require_exact_entries(
                payload,
                {candidate.candidate_id for candidate in plan.body.candidates},
                "quarantine payload",
            )
            _require_exact_entries(
                receipts,
                {f"{candidate.candidate_id}.json" for candidate in plan.body.candidates},
                "quarantine receipts",
            )
            _require_expected_transaction_entries(transaction)
            os.fsync(transaction)
            result = QuarantineResult(
                outcome=QuarantineOutcome.COMPLETED,
                transaction_id=plan.transaction_id,
                resumed=resumed,
                quarantined_count=len(plan.body.candidates),
                final_report=final_report,
                completion_sha256=completion_sha256,
            )
            _remove_active(version, active, result)
            unfenced = True
    except BaseException as error:
        if unfenced and result is not None and not isinstance(error, _CommittedFailure):
            raise _CommittedFailure(
                result,
                f"quarantine was unfenced before descriptor cleanup failed: {error}",
            ) from error
        raise
    if result is None:
        raise AssertionError("completed quarantine transaction lacks a result")
    return result


def _require_plan_storage_identity(state_descriptor: int, plan: QuarantinePlan) -> None:
    if _directory_identity(os.fstat(state_descriptor)) != plan.body.state_identity:
        raise _ProtocolFailure(
            QuarantineFailureReason.PLAN_CHANGED,
            "state directory identity differs from the durable plan",
        )
    if _database_identity_at(state_descriptor) != plan.body.database_identity:
        raise _ProtocolFailure(
            QuarantineFailureReason.PLAN_CHANGED,
            "database identity differs from the durable plan",
        )
    with _open_existing_directory_at(state_descriptor, "artifacts") as artifact_descriptor:
        if _directory_identity(os.fstat(artifact_descriptor)) != plan.body.artifact_root_identity:
            raise _ProtocolFailure(
                QuarantineFailureReason.PLAN_CHANGED,
                "artifact root identity differs from the durable plan",
            )


def _require_before_matches_plan(before: QuarantineBefore, plan: QuarantinePlan) -> None:
    report = before.report
    _require_quarantinable(report)
    if report.ledger_snapshot is None or report.artifact_audit is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            "BEFORE lacks its bound ledger snapshot or artifact audit",
        )
    if report.ledger_snapshot.snapshot_sha256 != plan.body.ledger_snapshot_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            "BEFORE ledger snapshot does not match PLAN",
        )
    if _reachable_sha256(report.artifact_audit.reachable) != plan.body.reachable_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            "BEFORE reachable graph does not match PLAN",
        )
    planned_findings = tuple(candidate.body.finding for candidate in plan.body.candidates)
    if _finding_set_sha256(report.artifact_audit.findings) != _finding_set_sha256(planned_findings):
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            "BEFORE findings do not match PLAN candidates",
        )


def _reconcile_candidates(
    artifact_descriptor: int,
    payload_descriptor: int,
    receipts_descriptor: int,
    plan: QuarantinePlan,
    plan_sha256: str,
) -> tuple[bool, ...]:
    candidate_ids = {candidate.candidate_id for candidate in plan.body.candidates}
    receipt_names = {f"{candidate_id}.json" for candidate_id in candidate_ids}
    _require_entries_subset(payload_descriptor, candidate_ids, "quarantine payload")
    _require_entries_subset(receipts_descriptor, receipt_names, "quarantine receipts")
    pending: list[bool] = []
    for candidate in plan.body.candidates:
        source_present = _candidate_source_exists(artifact_descriptor, candidate)
        destination_present = _entry_exists_exact(payload_descriptor, candidate.candidate_id)
        receipt = _expected_receipt(plan, plan_sha256, candidate)
        receipt_present = _entry_exists_exact(
            receipts_descriptor,
            f"{candidate.candidate_id}.json",
        )
        if source_present and not destination_present:
            if receipt_present:
                raise _ProtocolFailure(
                    QuarantineFailureReason.RECORD_INVALID,
                    f"pending candidate {candidate.candidate_id} already has a receipt",
                )
            _validate_candidate_source(artifact_descriptor, candidate, sync_file=False)
            pending.append(True)
            continue
        if destination_present and not source_present:
            _repair_moved_candidate_durability(
                artifact_descriptor,
                payload_descriptor,
                candidate,
            )
            _write_or_verify_receipt(receipts_descriptor, receipt)
            pending.append(False)
            continue
        state = "both source and destination exist" if source_present else "both are absent"
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_AMBIGUOUS,
            f"candidate {candidate.candidate_id}: {state}",
        )
    return tuple(pending)


def _require_current_report(
    plan: QuarantinePlan,
    report: StorageIntegrityReport,
    pending: Sequence[bool],
) -> None:
    _require_report_graph(plan, report)
    audit = report.artifact_audit
    if audit is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.REACHABLE_GRAPH_CHANGED,
            "current storage report lacks an artifact audit",
        )
    expected_findings = tuple(
        candidate.body.finding
        for candidate, is_pending in zip(plan.body.candidates, pending, strict=True)
        if is_pending
    )
    if _finding_set_sha256(audit.findings) != _finding_set_sha256(expected_findings):
        raise _ProtocolFailure(
            QuarantineFailureReason.PLAN_CHANGED,
            "current artifact findings are not exactly the remaining plan candidates",
        )


def _require_report_graph(plan: QuarantinePlan, report: StorageIntegrityReport) -> None:
    if (
        not report.quarantine_allowed
        or report.ledger_snapshot is None
        or report.artifact_audit is None
    ):
        raise _ProtocolFailure(
            QuarantineFailureReason.REACHABLE_GRAPH_CHANGED,
            "current storage audit no longer authorizes quarantine",
        )
    if report.ledger_snapshot.snapshot_sha256 != plan.body.ledger_snapshot_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.LEDGER_CHANGED,
            "verified ledger snapshot changed during quarantine",
        )
    if _reachable_sha256(report.artifact_audit.reachable) != plan.body.reachable_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.REACHABLE_GRAPH_CHANGED,
            "verified reachable artifact graph changed during quarantine",
        )


def _require_final_report(plan: QuarantinePlan, report: StorageIntegrityReport) -> None:
    _require_report_graph(plan, report)
    if report.artifact_audit is None or report.artifact_audit.findings or not report.clean:
        raise _ProtocolFailure(
            QuarantineFailureReason.FINAL_AUDIT_FAILED,
            "post-quarantine storage audit is not clean",
        )


def _move_candidate(
    artifact_descriptor: int,
    payload_descriptor: int,
    receipts_descriptor: int,
    plan: QuarantinePlan,
    plan_sha256: str,
    candidate: QuarantineCandidate,
) -> None:
    with _open_candidate_parent(artifact_descriptor, candidate.body.source_relative_path) as (
        source_parent,
        source_name,
    ):
        _observe_candidate_leaf(
            source_parent,
            source_name,
            candidate,
            sync_file=True,
        )
        if _entry_exists_exact(payload_descriptor, candidate.candidate_id):
            raise _ProtocolFailure(
                QuarantineFailureReason.DESTINATION_COLLISION,
                f"quarantine destination already exists for {candidate.candidate_id}",
            )
        try:
            rename_noreplace_at(
                source_parent,
                source_name,
                payload_descriptor,
                candidate.candidate_id,
            )
        except FileExistsError as error:
            raise _ProtocolFailure(
                QuarantineFailureReason.DESTINATION_COLLISION,
                f"quarantine destination collision for {candidate.candidate_id}",
            ) from error
        except OSError as error:
            if error.errno == errno.EXDEV:
                reason = QuarantineFailureReason.CROSS_DEVICE
            else:
                reason = QuarantineFailureReason.IO_ERROR
            raise _ProtocolFailure(
                reason,
                f"candidate no-replace move failed: {error}",
            ) from error

        # Make the evidence-side name durable before the CAS-side removal.
        os.fsync(payload_descriptor)
        os.fsync(source_parent)
        if _entry_exists_exact(source_parent, source_name):
            raise _ProtocolFailure(
                QuarantineFailureReason.CANDIDATE_AMBIGUOUS,
                "candidate source still exists after atomic rename",
            )
        _validate_candidate_destination(payload_descriptor, candidate)

    receipt = _expected_receipt(plan, plan_sha256, candidate)
    _write_or_verify_receipt(receipts_descriptor, receipt)


def _expected_receipt(
    plan: QuarantinePlan,
    plan_sha256: str,
    candidate: QuarantineCandidate,
) -> QuarantineReceipt:
    identity = candidate.body.source_identity
    return QuarantineReceipt(
        transaction_id=plan.transaction_id,
        plan_sha256=plan_sha256,
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


def _write_or_verify_receipt(parent_descriptor: int, receipt: QuarantineReceipt) -> str:
    return _write_immutable_model(
        parent_descriptor,
        f"{receipt.candidate_id}.json",
        receipt,
    )


def _repair_moved_candidate_durability(
    artifact_descriptor: int,
    payload_descriptor: int,
    candidate: QuarantineCandidate,
) -> None:
    """Repair a kill between rename and either required directory sync."""

    with _open_candidate_parent(artifact_descriptor, candidate.body.source_relative_path) as (
        source_parent,
        source_name,
    ):
        _observe_candidate_leaf(
            payload_descriptor,
            candidate.candidate_id,
            candidate,
            sync_file=True,
            allow_rename_ctime=True,
        )
        os.fsync(payload_descriptor)
        os.fsync(source_parent)
        if _entry_exists_exact(source_parent, source_name):
            raise _ProtocolFailure(
                QuarantineFailureReason.CANDIDATE_AMBIGUOUS,
                f"candidate {candidate.candidate_id} source reappeared during reconciliation",
            )
        _validate_candidate_destination(payload_descriptor, candidate)


def _read_expected_receipt(parent_descriptor: int, expected: QuarantineReceipt) -> str:
    observed, digest = _read_model(
        parent_descriptor,
        f"{expected.candidate_id}.json",
        QuarantineReceipt,
    )
    if observed != expected:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"receipt does not match candidate {expected.candidate_id}",
        )
    return digest


def _remove_active(
    version_descriptor: int,
    expected: QuarantineActive,
    result: QuarantineResult,
) -> None:
    observed, _ = _read_model(
        version_descriptor,
        _ACTIVE_NAME,
        QuarantineActive,
        maximum=_MAX_ACTIVE_BYTES,
    )
    if observed != expected:
        raise _ProtocolFailure(
            QuarantineFailureReason.ACTIVE_INVALID,
            "ACTIVE changed before completion could be unfenced",
        )
    try:
        os.unlink(_ACTIVE_NAME, dir_fd=version_descriptor)
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.IO_ERROR,
            f"ACTIVE could not be removed: {error}",
        ) from error
    try:
        os.fsync(version_descriptor)
    except OSError as error:
        raise _CommittedFailure(
            result,
            f"ACTIVE was removed but its parent directory could not be synced: {error}",
        ) from error


def _read_active(state_descriptor: int) -> QuarantineActive | None:
    quarantine = _try_open_existing_directory_at(state_descriptor, _QUARANTINE_NAME)
    if quarantine is None:
        return None
    try:
        version = _try_open_existing_directory_at(quarantine, _VERSION_NAME)
        if version is None:
            return None
        try:
            _clean_known_record_temps(version)
            if not _entry_exists_exact(version, _ACTIVE_NAME):
                return None
            _require_exact_entries(
                version,
                {_TRANSACTIONS_NAME, _ACTIVE_NAME},
                "active quarantine version",
            )
            active, _ = _read_model(
                version,
                _ACTIVE_NAME,
                QuarantineActive,
                maximum=_MAX_ACTIVE_BYTES,
            )
            return active
        finally:
            os.close(version)
    finally:
        os.close(quarantine)


def _active_path_exists(state_descriptor: int) -> bool:
    try:
        quarantine = _try_open_existing_directory_at(state_descriptor, _QUARANTINE_NAME)
        if quarantine is None:
            return False
        try:
            version = _try_open_existing_directory_at(quarantine, _VERSION_NAME)
            if version is None:
                return False
            try:
                return _entry_exists_exact(version, _ACTIVE_NAME)
            finally:
                os.close(version)
        finally:
            os.close(quarantine)
    except (OSError, _ProtocolFailure):
        # An uninspectable canonical fence namespace must be treated as fenced.
        return True


def _open_transaction_directories(
    stack: ExitStack,
    state_descriptor: int,
    transaction_id: str,
    *,
    create: bool,
) -> tuple[int, int, int, int, int]:
    if _TRANSACTION_NAME.fullmatch(transaction_id) is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            "transaction ID is not a lowercase SHA-256",
        )
    opener = _ensure_directory_at if create else _open_existing_directory_at
    state_device = os.fstat(state_descriptor).st_dev
    quarantine = stack.enter_context(opener(state_descriptor, _QUARANTINE_NAME))
    version = stack.enter_context(opener(quarantine, _VERSION_NAME))
    transactions = stack.enter_context(opener(version, _TRANSACTIONS_NAME))
    transaction = stack.enter_context(opener(transactions, transaction_id))
    payload = stack.enter_context(opener(transaction, _PAYLOAD_NAME))
    receipts = stack.enter_context(opener(transaction, _RECEIPTS_NAME))
    for descriptor in (quarantine, version, transactions, transaction, payload, receipts):
        if os.fstat(descriptor).st_dev != state_device:
            raise _ProtocolFailure(
                QuarantineFailureReason.CROSS_DEVICE,
                "quarantine namespace crosses a filesystem boundary",
            )
    return quarantine, version, transaction, payload, receipts


@contextmanager
def _ensure_directory_at(parent_descriptor: int, name: str) -> Iterator[int]:
    descriptor = _try_open_existing_directory_at(parent_descriptor, name)
    if descriptor is None:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError as error:
            raise _ProtocolFailure(
                QuarantineFailureReason.NAMESPACE_INVALID,
                f"directory {name} appeared with a noncanonical identity",
            ) from error
        except OSError as error:
            raise _ProtocolFailure(
                QuarantineFailureReason.IO_ERROR,
                f"directory {name} could not be created: {error}",
            ) from error
        os.fsync(parent_descriptor)
        descriptor = _open_existing_directory_descriptor(parent_descriptor, name)
    # Repair an inherited mkdir-before-parent-fsync crash before using the child.
    os.fsync(parent_descriptor)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_existing_directory_at(parent_descriptor: int, name: str) -> Iterator[int]:
    descriptor = _try_open_existing_directory_at(parent_descriptor, name)
    if descriptor is None:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"required directory {name} is missing",
        )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _try_open_existing_directory_at(parent_descriptor: int, name: str) -> int | None:
    if not _entry_exists_exact(parent_descriptor, name):
        return None
    return _open_existing_directory_descriptor(parent_descriptor, name)


def _open_existing_directory_descriptor(parent_descriptor: int, name: str) -> int:
    try:
        path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"directory {name} could not be inspected: {error}",
        ) from error
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"namespace entry {name} is not a real directory",
        )
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"directory {name} could not be opened without following links: {error}",
        ) from error
    try:
        opened = os.fstat(descriptor)
        final = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        expected = (path_metadata.st_dev, path_metadata.st_ino, stat.S_IFMT(path_metadata.st_mode))
        if (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode)) != expected or (
            final.st_dev,
            final.st_ino,
            stat.S_IFMT(final.st_mode),
        ) != expected:
            raise _ProtocolFailure(
                QuarantineFailureReason.NAMESPACE_INVALID,
                f"directory {name} changed while it was opened",
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _entry_exists_exact(parent_descriptor: int, name: str) -> bool:
    for observed in _iter_entry_names(parent_descriptor, "exact-name directory"):
        if observed == name:
            return True
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"entry {name} could not be inspected: {error}",
        ) from error
    raise _ProtocolFailure(
        QuarantineFailureReason.NAMESPACE_INVALID,
        f"entry {name} exists with different on-disk spelling",
    )


def _require_entry_absent(parent_descriptor: int, name: str) -> None:
    if _entry_exists_exact(parent_descriptor, name):
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"prepared transaction unexpectedly contains {name}",
        )


def _require_directory_empty(descriptor: int, label: str) -> None:
    if next(_iter_entry_names(descriptor, label), None) is not None:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"{label} is not empty",
        )


def _require_exact_entries(descriptor: int, expected: set[str], label: str) -> None:
    observed: set[str] = set()
    for name in _iter_entry_names(descriptor, label):
        if name not in expected:
            raise _ProtocolFailure(
                QuarantineFailureReason.NAMESPACE_INVALID,
                f"{label} contains unexplained entry {name!r}",
            )
        observed.add(name)
    if observed != expected:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"{label} does not contain exactly the durable plan entries",
        )


def _require_entries_subset(descriptor: int, allowed: set[str], label: str) -> None:
    for name in _iter_entry_names(descriptor, label):
        if name not in allowed:
            raise _ProtocolFailure(
                QuarantineFailureReason.NAMESPACE_INVALID,
                f"{label} contains unexplained entry {name!r}",
            )


def _require_expected_transaction_entries(descriptor: int) -> None:
    allowed = {
        _BEFORE_NAME,
        _PLAN_NAME,
        _PAYLOAD_NAME,
        _RECEIPTS_NAME,
        _AFTER_NAME,
        _COMPLETE_NAME,
    }
    _require_entries_subset(descriptor, allowed, "quarantine transaction")


def _clean_known_record_temps(descriptor: int) -> None:
    removed = False
    temporary_names = tuple(
        name
        for name in _iter_entry_names(descriptor, "record temporary directory")
        if _RECORD_TEMP.fullmatch(name) is not None
    )
    for name in temporary_names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _ProtocolFailure(
                QuarantineFailureReason.RECORD_INVALID,
                f"record temporary {name} is not a single-link regular file",
            )
        os.unlink(name, dir_fd=descriptor)
        removed = True
    if removed:
        os.fsync(descriptor)


def _iter_entry_names(descriptor: int, label: str) -> Iterator[str]:
    try:
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries, start=1):
                if index > _MAX_DIRECTORY_ENTRIES:
                    raise _ProtocolFailure(
                        QuarantineFailureReason.NAMESPACE_INVALID,
                        f"{label} exceeds the directory-entry limit",
                    )
                yield entry.name
    except _ProtocolFailure:
        raise
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            f"{label} could not be enumerated safely: {error}",
        ) from error


def _write_immutable_model(
    parent_descriptor: int,
    name: str,
    model: BaseModel,
    *,
    maximum: int = _MAX_RECORD_BYTES,
) -> str:
    data = canonical_json(model).encode("utf-8")
    if len(data) > maximum:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"record {name} exceeds its byte limit",
        )
    if _entry_exists_exact(parent_descriptor, name):
        observed = _read_record_bytes(parent_descriptor, name, maximum=maximum)
        if observed != data:
            raise _ProtocolFailure(
                QuarantineFailureReason.RECORD_INVALID,
                f"immutable record {name} differs from the expected bytes",
            )
        os.fsync(parent_descriptor)
        return sha256_bytes(observed)

    temporary = f".{name}.tmp-{token_hex(16)}"
    descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=parent_descriptor)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            amount = os.write(descriptor, view[written:])
            if amount <= 0:
                raise OSError(errno.EIO, "record write made no progress")
            written += amount
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != len(data)
        ):
            raise _ProtocolFailure(
                QuarantineFailureReason.RECORD_INVALID,
                f"record temporary {temporary} failed verification",
            )
    except BaseException as error:
        try:
            os.close(descriptor)
        except OSError as close_error:
            error.add_note(f"record temporary close also failed: {close_error!r}")
        try:
            _remove_record_temporary(parent_descriptor, temporary, missing_ok=True)
        except BaseException as cleanup_error:
            error.add_note(f"record temporary cleanup also failed: {cleanup_error!r}")
        raise
    else:
        os.close(descriptor)

    published = False
    try:
        try:
            rename_noreplace_at(parent_descriptor, temporary, parent_descriptor, name)
            published = True
        except FileExistsError as collision:
            observed = _read_record_bytes(parent_descriptor, name, maximum=maximum)
            if observed != data:
                raise _ProtocolFailure(
                    QuarantineFailureReason.RECORD_INVALID,
                    f"immutable record collision at {name}",
                ) from collision
            _remove_record_temporary(parent_descriptor, temporary, missing_ok=False)
        os.fsync(parent_descriptor)
    except BaseException as error:
        if not published:
            try:
                _remove_record_temporary(parent_descriptor, temporary, missing_ok=True)
            except BaseException as cleanup_error:
                error.add_note(f"record temporary cleanup also failed: {cleanup_error!r}")
        raise
    observed = _read_record_bytes(parent_descriptor, name, maximum=maximum)
    if observed != data:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"published immutable record {name} failed verification",
        )
    return sha256_bytes(observed)


def _remove_record_temporary(
    parent_descriptor: int,
    name: str,
    *,
    missing_ok: bool,
) -> None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"record temporary {name} is not a single-link regular file",
        )
    os.unlink(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)


def _read_model[ModelT: BaseModel](
    parent_descriptor: int,
    name: str,
    model_type: type[ModelT],
    *,
    maximum: int = _MAX_RECORD_BYTES,
) -> tuple[ModelT, str]:
    data = _read_record_bytes(parent_descriptor, name, maximum=maximum)
    try:
        model = model_type.model_validate_json(data, strict=True)
    except (ValidationError, ValueError) as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"record {name} is not valid {model_type.__name__}",
        ) from error
    if canonical_json(model).encode("utf-8") != data:
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"record {name} is not canonical JSON",
        )
    return model, sha256_bytes(data)


def _read_record_bytes(parent_descriptor: int, name: str, *, maximum: int) -> bytes:
    if not _entry_exists_exact(parent_descriptor, name):
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"required immutable record {name} is missing",
        )
    path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(path_metadata.st_mode)
        or path_metadata.st_nlink != 1
        or stat.S_IMODE(path_metadata.st_mode) != 0o600
        or path_metadata.st_size > maximum
    ):
        raise _ProtocolFailure(
            QuarantineFailureReason.RECORD_INVALID,
            f"record {name} has an invalid filesystem identity",
        )
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(path_metadata):
            raise _ProtocolFailure(
                QuarantineFailureReason.RECORD_INVALID,
                f"record {name} changed while it was opened",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, maximum - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise _ProtocolFailure(
                    QuarantineFailureReason.RECORD_INVALID,
                    f"record {name} grew beyond its byte limit",
                )
        final_opened = os.fstat(descriptor)
        final_path = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _file_snapshot(opened) != _file_snapshot(final_opened) or _file_snapshot(
            opened
        ) != _file_snapshot(final_path):
            raise _ProtocolFailure(
                QuarantineFailureReason.RECORD_INVALID,
                f"record {name} changed while it was read",
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _candidate_source_exists(
    artifact_descriptor: int,
    candidate: QuarantineCandidate,
) -> bool:
    with _open_candidate_parent(artifact_descriptor, candidate.body.source_relative_path) as (
        parent,
        name,
    ):
        return _entry_exists_exact(parent, name)


def _validate_candidate_source(
    artifact_descriptor: int,
    candidate: QuarantineCandidate,
    *,
    sync_file: bool,
) -> None:
    with _open_candidate_parent(artifact_descriptor, candidate.body.source_relative_path) as (
        parent,
        name,
    ):
        _observe_candidate_leaf(parent, name, candidate, sync_file=sync_file)


def _validate_candidate_destination(
    payload_descriptor: int,
    candidate: QuarantineCandidate,
) -> None:
    _observe_candidate_leaf(
        payload_descriptor,
        candidate.candidate_id,
        candidate,
        sync_file=False,
        allow_rename_ctime=True,
    )


def _observe_candidate_leaf(
    parent_descriptor: int,
    name: str,
    candidate: QuarantineCandidate,
    *,
    sync_file: bool,
    allow_rename_ctime: bool = False,
) -> None:
    identity, digest = _observe_regular_leaf(
        parent_descriptor,
        name,
        maximum_bytes=_MAX_CANDIDATE_BYTES,
        budget=None,
        sync_file=sync_file,
    )
    expected = candidate.body.source_identity
    identity_matches = (
        _stable_file_identity(identity) == _stable_file_identity(expected)
        if allow_rename_ctime
        else identity == expected
    )
    if not identity_matches or digest != candidate.body.content_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_CHANGED,
            f"candidate {candidate.candidate_id} no longer matches its plan",
        )


def _observe_candidate(
    artifact_descriptor: int,
    relative_path: str,
    *,
    budget: _HashBudget,
    expected_identity: FileIdentity | None,
    expected_sha256: str | None,
    sync_file: bool,
) -> tuple[FileIdentity, str]:
    with _open_candidate_parent(artifact_descriptor, relative_path) as (parent, name):
        identity, digest = _observe_regular_leaf(
            parent,
            name,
            maximum_bytes=_MAX_CANDIDATE_BYTES,
            budget=budget,
            sync_file=sync_file,
        )
    if expected_identity is not None and identity != expected_identity:
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_CHANGED,
            f"candidate {relative_path} changed identity",
        )
    if expected_sha256 is not None and digest != expected_sha256:
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_CHANGED,
            f"candidate {relative_path} changed content",
        )
    return identity, digest


def _observe_regular_leaf(
    parent_descriptor: int,
    name: str,
    *,
    maximum_bytes: int,
    budget: _HashBudget | None,
    sync_file: bool,
) -> tuple[FileIdentity, str]:
    if not _entry_exists_exact(parent_descriptor, name):
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_CHANGED,
            f"candidate {name} is missing",
        )
    path_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    identity = _file_identity(path_metadata)
    if identity.size_bytes > maximum_bytes:
        raise _ProtocolFailure(
            QuarantineFailureReason.HASH_LIMIT_EXCEEDED,
            f"candidate {name} exceeds the per-file byte limit",
        )
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if _file_snapshot(opened) != _file_snapshot(path_metadata):
            raise _ProtocolFailure(
                QuarantineFailureReason.CANDIDATE_CHANGED,
                f"candidate {name} changed while it was opened",
            )
        digest = hashlib.sha256()
        total = 0
        while True:
            read_size = min(
                _READ_CHUNK_BYTES,
                maximum_bytes - total + 1,
                *((budget.remaining + 1,) if budget is not None else ()),
            )
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                raise _ProtocolFailure(
                    QuarantineFailureReason.HASH_LIMIT_EXCEEDED,
                    f"candidate {name} grew beyond the byte limit",
                )
            if budget is not None:
                budget.consume(len(chunk))
            digest.update(chunk)
        if sync_file:
            os.fsync(descriptor)
        final_opened = os.fstat(descriptor)
        final_path = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            total != identity.size_bytes
            or _file_snapshot(opened) != _file_snapshot(final_opened)
            or _file_snapshot(opened) != _file_snapshot(final_path)
        ):
            raise _ProtocolFailure(
                QuarantineFailureReason.CANDIDATE_CHANGED,
                f"candidate {name} changed while it was hashed",
            )
        return identity, digest.hexdigest()
    finally:
        os.close(descriptor)


@contextmanager
def _open_candidate_parent(
    artifact_descriptor: int,
    relative_path: str,
) -> Iterator[tuple[int, str]]:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise _ProtocolFailure(
            QuarantineFailureReason.UNSUPPORTED_FINDING,
            f"candidate path {relative_path!r} is unsafe",
        )
    current = os.dup(artifact_descriptor)
    try:
        for component in path.parts[:-1]:
            next_descriptor = _open_existing_directory_descriptor(current, component)
            os.close(current)
            current = next_descriptor
        yield current, path.name
    finally:
        os.close(current)


def _file_identity(metadata: os.stat_result) -> FileIdentity:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _ProtocolFailure(
            QuarantineFailureReason.CANDIDATE_CHANGED,
            "quarantine candidate is not a single-link regular file",
        )
    return FileIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        link_count=metadata.st_nlink,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> DirectoryIdentity:
    if not stat.S_ISDIR(metadata.st_mode):
        raise _ProtocolFailure(
            QuarantineFailureReason.NAMESPACE_INVALID,
            "expected an open real directory",
        )
    return DirectoryIdentity(device=metadata.st_dev, inode=metadata.st_ino)


def _database_identity_at(state_descriptor: int) -> DatabaseIdentity:
    name = "runs.db"
    if not _entry_exists_exact(state_descriptor, name):
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            "authoritative database is missing",
        )
    try:
        path_metadata = os.stat(name, dir_fd=state_descriptor, follow_symlinks=False)
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            f"authoritative database could not be inspected: {error}",
        ) from error
    if not stat.S_ISREG(path_metadata.st_mode) or path_metadata.st_nlink != 1:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            "authoritative database must be a single-link regular file",
        )
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=state_descriptor)
    except OSError as error:
        raise _ProtocolFailure(
            QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
            f"authoritative database could not be opened without following links: {error}",
        ) from error
    try:
        opened = os.fstat(descriptor)
        final = os.stat(name, dir_fd=state_descriptor, follow_symlinks=False)
        expected = (
            stat.S_IFREG,
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_nlink,
        )
        if (
            stat.S_IFMT(opened.st_mode),
            opened.st_dev,
            opened.st_ino,
            opened.st_nlink,
        ) != expected or (
            stat.S_IFMT(final.st_mode),
            final.st_dev,
            final.st_ino,
            final.st_nlink,
        ) != expected:
            raise _ProtocolFailure(
                QuarantineFailureReason.STORAGE_NOT_QUARANTINABLE,
                "authoritative database changed while its identity was captured",
            )
        return DatabaseIdentity(device=opened.st_dev, inode=opened.st_ino)
    finally:
        os.close(descriptor)


def _file_snapshot(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        stat.S_IFMT(metadata.st_mode),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_size,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _stable_file_identity(identity: FileIdentity) -> tuple[str, int, int, int, int, int, int]:
    """Return fields that an atomic rename must preserve (ctime may change)."""

    return (
        identity.file_type,
        identity.device,
        identity.inode,
        identity.mode,
        identity.link_count,
        identity.size_bytes,
        identity.mtime_ns,
    )


def _reachable_sha256(reachable: Sequence[object]) -> str:
    return canonical_sha256(
        {
            "reachable": list(reachable),
            "schema_version": "guildmind.quarantine-reachable/v1",
        }
    )


def _finding_set_sha256(findings: Sequence[ArtifactFinding]) -> str:
    serialized = sorted(canonical_json(finding) for finding in findings)
    return canonical_sha256(
        {
            "findings": serialized,
            "schema_version": "guildmind.quarantine-finding-set/v1",
        }
    )
