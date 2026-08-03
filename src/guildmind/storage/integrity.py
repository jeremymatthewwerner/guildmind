"""Pure, typed reachability and integrity audit for the filesystem CAS.

The caller supplies verified SQLite roots binding each manifest to its ledger head.
This module validates those root identities again, never mutates either store, verifies
direct manifest references, follows only the two fixed structured artifact roles
understood today, and inventories the CAS without following symlinks.

Correct use currently requires one quiescent, exclusive-writer maintenance window
spanning ledger snapshot verification through the completed CAS scan.  The audit is
read-only, but its result cannot authorize later mutation if concurrent or out-of-band
actors can change paths during that window.

``complete`` means the bounded filesystem inventory itself completed.  A complete
audit can still contain integrity findings.  ``quarantine_allowed`` additionally
requires every committed reference and typed relationship to verify, so callers can
never quarantine bytes while the authoritative reachability graph is uncertain.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from guildmind.domain import (
    ArtifactRef,
    EvaluationResult,
    RunManifest,
    TaskSpec,
    canonical_json,
    canonical_sha256,
)
from guildmind.storage.artifacts import ArtifactCorruptionError, FileArtifactStore
from guildmind.storage.events import VerifiedRunRoot, verified_run_roots_sha256

_SCHEMA_VERSION: Literal["guildmind.artifact-audit/v1"] = "guildmind.artifact-audit/v1"
_TASK_MEDIA_TYPE = "application/vnd.guildmind.task+json"
_EVALUATION_MEDIA_TYPE = "application/vnd.guildmind.evaluation+json"
_DIGEST_NAME = re.compile(r"^[0-9a-f]{64}$")
_PREFIX_NAME = re.compile(r"^[0-9a-f]{2}$")
_TEMP_PREFIX = ".artifact-"

_MAX_ROOTS = 4_096
_MAX_REACHABLE_REFERENCES = 16_384
_MAX_INVENTORY_ENTRIES = 65_536
_MAX_ARTIFACT_BYTES = 268_435_456
_MAX_STRUCTURED_BYTES = 8_388_608
_MAX_TOTAL_HASHED_BYTES = 1_073_741_824
_READ_CHUNK_BYTES = 65_536


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ArtifactFindingKind(StrEnum):
    MISSING_REFERENCED = "missing_referenced"
    CORRUPT_REFERENCED = "corrupt_referenced"
    VALID_FINALIZED_ORPHAN = "valid_finalized_orphan"
    CORRUPT_FINALIZED_ORPHAN = "corrupt_finalized_orphan"
    TEMP_ORPHAN = "temp_orphan"
    NONCANONICAL_ENTRY = "noncanonical_entry"
    HARDLINK = "hardlink"
    SYMLINK = "symlink"
    SPECIAL_FILE = "special_file"
    SCAN_ERROR = "scan_error"
    LIMIT_EXCEEDED = "limit_exceeded"


class ArtifactOwner(_AuditModel):
    run_id: str = Field(min_length=1)
    path: tuple[str, ...]

    @model_validator(mode="after")
    def _path_is_nonempty(self) -> Self:
        if not self.run_id.strip():
            raise ValueError("artifact owner run_id cannot be blank")
        if not self.path or any(not part.strip() for part in self.path):
            raise ValueError("artifact owner path must contain nonblank components")
        return self


class ReachableArtifact(_AuditModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    storage_ref: str = Field(min_length=1)
    media_types: tuple[str, ...]
    owners: tuple[ArtifactOwner, ...]
    bytes_verified: bool

    @model_validator(mode="after")
    def _inventories_are_canonical(self) -> Self:
        if self.storage_ref != _canonical_relative_path(self.sha256):
            raise ValueError("reachable storage_ref must be the canonical digest path")
        if self.media_types != tuple(sorted(set(self.media_types))):
            raise ValueError("reachable media types must be sorted and unique")
        if not self.media_types or any(not value.strip() for value in self.media_types):
            raise ValueError("reachable artifact requires nonblank media types")
        if self.owners != tuple(sorted(set(self.owners), key=_owner_key)):
            raise ValueError("reachable owners must be sorted and unique")
        if not self.owners:
            raise ValueError("reachable artifact requires at least one owner")
        return self


class ArtifactFinding(_AuditModel):
    kind: ArtifactFindingKind
    relative_path: str = Field(min_length=1)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    detail: str | None = None
    errno: int | None = Field(default=None, ge=0)
    owners: tuple[ArtifactOwner, ...] = ()

    @model_validator(mode="after")
    def _owners_are_canonical(self) -> Self:
        if self.owners != tuple(sorted(set(self.owners), key=_owner_key)):
            raise ValueError("finding owners must be sorted and unique")
        if (
            self.kind
            in {
                ArtifactFindingKind.MISSING_REFERENCED,
                ArtifactFindingKind.CORRUPT_REFERENCED,
            }
            and not self.owners
        ):
            raise ValueError("referenced findings require an owner")
        return self


class ArtifactAudit(_AuditModel):
    schema_version: Literal["guildmind.artifact-audit/v1"] = _SCHEMA_VERSION
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reachable: tuple[ReachableArtifact, ...]
    findings: tuple[ArtifactFinding, ...]
    complete: bool
    quarantine_allowed: bool

    @model_validator(mode="after")
    def _claims_and_order_are_derived(self) -> Self:
        if tuple(item.sha256 for item in self.reachable) != tuple(
            sorted({item.sha256 for item in self.reachable})
        ):
            raise ValueError("reachable artifacts must be unique and sorted by digest")
        if self.findings != tuple(sorted(set(self.findings), key=_finding_key)):
            raise ValueError("artifact findings must be sorted and unique")
        complete = not any(
            item.kind in {ArtifactFindingKind.SCAN_ERROR, ArtifactFindingKind.LIMIT_EXCEEDED}
            for item in self.findings
        )
        if self.complete is not complete:
            raise ValueError("audit completeness must be derived from scan findings")
        referenced_failure = any(item.owners for item in self.findings)
        all_reachable_verified = all(item.bytes_verified for item in self.reachable)
        if self.quarantine_allowed is not (
            complete and not referenced_failure and all_reachable_verified
        ):
            raise ValueError(
                "quarantine_allowed must require a complete, byte-verified trusted graph"
            )
        return self


@dataclass(frozen=True, slots=True)
class _ReferenceNode:
    reference: ArtifactRef
    owner: ArtifactOwner
    structured_role: Literal["task_spec", "evaluation"] | None


@dataclass(frozen=True, slots=True)
class _BlobRead:
    ok: bool
    observed_sha256: str | None
    size_bytes: int | None
    data: bytes | None
    kind: ArtifactFindingKind | None = None
    detail: str | None = None
    errno: int | None = None


@dataclass(frozen=True, slots=True)
class _PathHash:
    observed_sha256: str | None
    errno: int | None = None
    limit_exceeded: bool = False


@dataclass(slots=True)
class _HashBudget:
    maximum: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return self.maximum - self.used

    def consume(self, size_bytes: int) -> bool:
        if size_bytes > self.remaining:
            return False
        self.used += size_bytes
        return True


@dataclass(slots=True)
class _ReachableBuilder:
    size_bytes: int
    storage_ref: str
    media_types: set[str]
    owners: set[ArtifactOwner]
    bytes_verified: bool = False


class _FindingBuilders:
    def __init__(self) -> None:
        self._owners: dict[
            tuple[
                ArtifactFindingKind,
                str,
                str | None,
                str | None,
                int | None,
                str | None,
                int | None,
            ],
            set[ArtifactOwner],
        ] = defaultdict(set)

    def add(
        self,
        kind: ArtifactFindingKind,
        relative_path: str,
        *,
        expected_sha256: str | None = None,
        observed_sha256: str | None = None,
        size_bytes: int | None = None,
        detail: str | None = None,
        error_number: int | None = None,
        owners: Iterable[ArtifactOwner] = (),
    ) -> None:
        key = (
            kind,
            relative_path,
            expected_sha256,
            observed_sha256,
            size_bytes,
            detail,
            error_number,
        )
        self._owners[key].update(owners)

    def freeze(self) -> tuple[ArtifactFinding, ...]:
        result = [
            ArtifactFinding(
                kind=key[0],
                relative_path=key[1],
                expected_sha256=key[2],
                observed_sha256=key[3],
                size_bytes=key[4],
                detail=key[5],
                errno=key[6],
                owners=tuple(sorted(owners, key=_owner_key)),
            )
            for key, owners in self._owners.items()
        ]
        return tuple(sorted(result, key=_finding_key))


def audit_artifact_store(
    roots: Sequence[VerifiedRunRoot],
    artifact_store: FileArtifactStore,
) -> ArtifactAudit:
    """Audit one ledger/CAS snapshot inside an exclusive-writer maintenance window."""
    ordered_roots, snapshot_sha256 = _validate_roots(roots)
    findings = _FindingBuilders()
    try:
        artifact_store.verify_root_identity()
    except ArtifactCorruptionError:
        findings.add(
            ArtifactFindingKind.SCAN_ERROR,
            ".",
            detail="artifact_store_root_identity",
        )
        frozen_findings = findings.freeze()
        return ArtifactAudit(
            snapshot_sha256=snapshot_sha256,
            reachable=(),
            findings=frozen_findings,
            complete=False,
            quarantine_allowed=False,
        )
    if not _validate_store_root(artifact_store.root, findings):
        frozen_findings = findings.freeze()
        return ArtifactAudit(
            snapshot_sha256=snapshot_sha256,
            reachable=(),
            findings=frozen_findings,
            complete=False,
            quarantine_allowed=False,
        )
    builders: dict[str, _ReachableBuilder] = {}
    reads: dict[tuple[str, int, str], _BlobRead] = {}
    hash_budget = _HashBudget(_MAX_TOTAL_HASHED_BYTES)
    manifest_by_run: dict[str, RunManifest] = {}
    tasks_by_run: dict[str, TaskSpec] = {}
    evaluations_by_run: dict[
        str,
        tuple[EvaluationResult, ArtifactRef, ArtifactOwner],
    ] = {}

    if len(ordered_roots) > _MAX_ROOTS:
        findings.add(
            ArtifactFindingKind.LIMIT_EXCEEDED,
            ".",
            detail="root_limit",
        )
        ordered_roots = ordered_roots[:_MAX_ROOTS]
    for root in ordered_roots:
        manifest = root.manifest
        manifest_by_run[manifest.run_id] = manifest

    pending: deque[_ReferenceNode] = deque()
    for root in ordered_roots:
        manifest = root.manifest
        for role, reference in sorted(manifest.artifacts.items()):
            structured: Literal["task_spec", "evaluation"] | None = None
            if role == "task_spec":
                structured = "task_spec"
            elif role == "evaluation":
                structured = "evaluation"
            pending.append(
                _ReferenceNode(
                    reference=reference,
                    owner=ArtifactOwner(run_id=manifest.run_id, path=(role,)),
                    structured_role=structured,
                )
            )

    processed_nodes = 0
    while pending:
        node = pending.popleft()
        processed_nodes += 1
        if processed_nodes > _MAX_REACHABLE_REFERENCES:
            findings.add(
                ArtifactFindingKind.LIMIT_EXCEEDED,
                ".",
                detail="reachable_reference_limit",
            )
            break
        reference = node.reference
        expected_path = _canonical_relative_path(reference.sha256)
        builder = builders.get(reference.sha256)
        if builder is None:
            builder = _ReachableBuilder(
                size_bytes=reference.size_bytes,
                storage_ref=reference.storage_ref,
                media_types=set(),
                owners=set(),
            )
            builders[reference.sha256] = builder
        elif (
            builder.size_bytes != reference.size_bytes
            or builder.storage_ref != reference.storage_ref
        ):
            findings.add(
                ArtifactFindingKind.CORRUPT_REFERENCED,
                expected_path,
                expected_sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                detail="conflicting_reference_metadata",
                owners=(*builder.owners, node.owner),
            )
        builder.media_types.add(reference.media_type)
        builder.owners.add(node.owner)

        capture = node.structured_role is not None
        cache_key = (reference.sha256, reference.size_bytes, reference.storage_ref)
        read = reads.get(cache_key)
        if read is None or (capture and read.ok and read.data is None):
            read = _read_reference(
                artifact_store.root,
                reference,
                capture=capture,
                hash_budget=hash_budget,
            )
            reads[cache_key] = read
        if not read.ok:
            if read.kind is None:
                raise AssertionError("failed artifact read lacks a finding kind")
            findings.add(
                read.kind,
                expected_path,
                expected_sha256=reference.sha256,
                observed_sha256=read.observed_sha256,
                size_bytes=read.size_bytes,
                detail=read.detail,
                error_number=read.errno,
                owners=(node.owner,),
            )
            continue
        if read.size_bytes is None:
            raise AssertionError("successful artifact read lacks an observed size")
        builder.size_bytes = read.size_bytes
        builder.bytes_verified = True
        if node.structured_role is None:
            continue
        if read.data is None:
            findings.add(
                ArtifactFindingKind.CORRUPT_REFERENCED,
                expected_path,
                expected_sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                detail="structured_artifact_not_captured",
                owners=(node.owner,),
            )
            continue
        if node.structured_role == "task_spec":
            if reference.media_type != _TASK_MEDIA_TYPE:
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="task_spec_media_type",
                    owners=(node.owner,),
                )
                continue
            try:
                task = TaskSpec.model_validate_json(read.data)
            except (TypeError, ValueError):
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="task_spec_parse",
                    owners=(node.owner,),
                )
                continue
            try:
                canonical_task = canonical_json(task).encode("utf-8")
            except (TypeError, ValueError):
                canonical_task = None
            if canonical_task is None or read.data != canonical_task:
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="task_spec_noncanonical_json",
                    owners=(node.owner,),
                )
                continue
            if "evaluator_version" in task.metadata:
                task_evaluator_version = task.metadata["evaluator_version"]
                if (
                    not isinstance(task_evaluator_version, str)
                    or not task_evaluator_version.strip()
                ):
                    findings.add(
                        ArtifactFindingKind.CORRUPT_REFERENCED,
                        expected_path,
                        expected_sha256=reference.sha256,
                        size_bytes=reference.size_bytes,
                        detail="task_spec_evaluator_version_invalid",
                        owners=(node.owner,),
                    )
            manifest = manifest_by_run[node.owner.run_id]
            if task.task_id != manifest.task_id or task.image_digest != manifest.environment_digest:
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="task_spec_manifest_mismatch",
                    owners=(node.owner,),
                )
            tasks_by_run[node.owner.run_id] = task
            pending.extend(_task_references(node.owner.run_id, task))
        else:
            if reference.media_type != _EVALUATION_MEDIA_TYPE:
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="evaluation_media_type",
                    owners=(node.owner,),
                )
                continue
            try:
                evaluation = EvaluationResult.model_validate_json(read.data)
            except (TypeError, ValueError):
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="evaluation_parse",
                    owners=(node.owner,),
                )
                continue
            try:
                canonical_evaluation = canonical_json(evaluation).encode("utf-8")
            except (TypeError, ValueError):
                canonical_evaluation = None
            if canonical_evaluation is None or read.data != canonical_evaluation:
                findings.add(
                    ArtifactFindingKind.CORRUPT_REFERENCED,
                    expected_path,
                    expected_sha256=reference.sha256,
                    size_bytes=reference.size_bytes,
                    detail="evaluation_noncanonical_json",
                    owners=(node.owner,),
                )
                continue
            manifest = manifest_by_run[node.owner.run_id]
            _crosscheck_evaluation(
                manifest,
                evaluation,
                reference,
                node.owner,
                findings,
            )
            evaluations_by_run[node.owner.run_id] = (evaluation, reference, node.owner)
            pending.extend(_evaluation_references(node.owner.run_id, evaluation))

    _crosscheck_task_evaluations(tasks_by_run, evaluations_by_run, findings)

    reachable_owners = {digest: set(builder.owners) for digest, builder in builders.items()}
    exactly_observed = _inventory_store(
        artifact_store.root,
        reachable_owners,
        findings,
        hash_budget,
    )
    for digest, builder in sorted(builders.items()):
        if digest in exactly_observed:
            continue
        findings.add(
            ArtifactFindingKind.CORRUPT_REFERENCED,
            _canonical_relative_path(digest),
            expected_sha256=digest,
            size_bytes=builder.size_bytes,
            detail="canonical_path_not_observed",
            owners=builder.owners,
        )
    reachable = tuple(
        ReachableArtifact(
            sha256=digest,
            size_bytes=builder.size_bytes,
            storage_ref=_canonical_relative_path(digest),
            media_types=tuple(sorted(builder.media_types)),
            owners=tuple(sorted(builder.owners, key=_owner_key)),
            bytes_verified=builder.bytes_verified,
        )
        for digest, builder in sorted(builders.items())
    )
    frozen_findings = findings.freeze()
    complete = not any(
        item.kind in {ArtifactFindingKind.SCAN_ERROR, ArtifactFindingKind.LIMIT_EXCEEDED}
        for item in frozen_findings
    )
    quarantine_allowed = (
        complete
        and not any(item.owners for item in frozen_findings)
        and all(item.bytes_verified for item in reachable)
    )
    return ArtifactAudit(
        snapshot_sha256=snapshot_sha256,
        reachable=reachable,
        findings=frozen_findings,
        complete=complete,
        quarantine_allowed=quarantine_allowed,
    )


def _validate_roots(
    roots: Sequence[VerifiedRunRoot],
) -> tuple[tuple[VerifiedRunRoot, ...], str]:
    ordered = tuple(sorted(roots, key=lambda item: item.manifest.run_id))
    previous_run_id: str | None = None
    for root in ordered:
        run_id = root.manifest.run_id
        if run_id == previous_run_id:
            raise ValueError(f"duplicate verified run root: {run_id}")
        previous_run_id = run_id
        _validate_root_integer(
            root.manifest_revision,
            field="manifest_revision",
            run_id=run_id,
            minimum=0,
        )
        _validate_root_integer(
            root.event_count,
            field="event_count",
            run_id=run_id,
            minimum=1,
        )
        if root.manifest_revision >= root.event_count:
            raise ValueError(
                f"verified run root manifest_revision must be less than event_count: {run_id}"
            )
        _validate_root_digest(
            root.manifest_sha256,
            field="manifest_sha256",
            run_id=run_id,
        )
        expected_manifest_sha256 = canonical_sha256(root.manifest)
        if root.manifest_sha256 != expected_manifest_sha256:
            raise ValueError(f"verified run root manifest hash mismatch: {run_id}")
        _validate_root_digest(
            root.head_event_sha256,
            field="head_event_sha256",
            run_id=run_id,
        )
    return ordered, verified_run_roots_sha256(ordered)


def _validate_root_integer(
    value: int,
    *,
    field: str,
    run_id: str,
    minimum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"verified run root {field} must be an integer >= {minimum}: {run_id}")


def _validate_root_digest(value: str, *, field: str, run_id: str) -> None:
    if not isinstance(value, str) or _DIGEST_NAME.fullmatch(value) is None:
        raise ValueError(f"verified run root {field} must be a lowercase SHA-256: {run_id}")


def _validate_store_root(root: Path, findings: _FindingBuilders) -> bool:
    if not root.is_absolute():
        findings.add(
            ArtifactFindingKind.SCAN_ERROR,
            ".",
            detail="artifact_store_root_not_absolute",
        )
        return False
    current = Path(root.anchor)
    candidates = [current]
    for component in root.parts[1:]:
        current /= component
        candidates.append(current)
    for current in candidates:
        is_root = current == root
        try:
            metadata = current.lstat()
        except OSError as error:
            findings.add(
                ArtifactFindingKind.SCAN_ERROR,
                ".",
                detail=(
                    "artifact_store_root_lstat" if is_root else "artifact_store_ancestor_lstat"
                ),
                error_number=_errno(error),
            )
            return False
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if stat.S_ISLNK(metadata.st_mode):
            detail = "artifact_store_root_symlink" if is_root else "artifact_store_ancestor_symlink"
        else:
            detail = (
                "artifact_store_root_not_directory"
                if is_root
                else "artifact_store_ancestor_not_directory"
            )
        findings.add(
            ArtifactFindingKind.SCAN_ERROR,
            ".",
            size_bytes=metadata.st_size,
            detail=detail,
        )
        return False
    return True


def _task_references(run_id: str, task: TaskSpec) -> tuple[_ReferenceNode, ...]:
    nodes = [
        _ReferenceNode(
            reference=task.problem_statement,
            owner=ArtifactOwner(run_id=run_id, path=("task_spec", "problem_statement")),
            structured_role=None,
        ),
        _ReferenceNode(
            reference=task.repository_snapshot,
            owner=ArtifactOwner(run_id=run_id, path=("task_spec", "repository_snapshot")),
            structured_role=None,
        ),
    ]
    nodes.extend(
        _ReferenceNode(
            reference=reference,
            owner=ArtifactOwner(
                run_id=run_id,
                path=("task_spec", "visible_tests", f"{index:08d}"),
            ),
            structured_role=None,
        )
        for index, reference in enumerate(task.visible_tests)
    )
    return tuple(nodes)


def _evaluation_references(
    run_id: str,
    evaluation: EvaluationResult,
) -> tuple[_ReferenceNode, ...]:
    return tuple(
        _ReferenceNode(
            reference=reference,
            owner=ArtifactOwner(
                run_id=run_id,
                path=("evaluation", "evidence", f"{index:08d}"),
            ),
            structured_role=None,
        )
        for index, reference in enumerate(evaluation.evidence)
    )


def _crosscheck_evaluation(
    manifest: RunManifest,
    evaluation: EvaluationResult,
    reference: ArtifactRef,
    owner: ArtifactOwner,
    findings: _FindingBuilders,
) -> None:
    roles = (
        "evaluation_stdout",
        "evaluation_stderr",
        "evaluation_candidate_stdout",
        "evaluation_scorer_stdout",
    )
    expected = tuple(manifest.artifacts[role] for role in roles if role in manifest.artifacts)
    if evaluation.run_id != manifest.run_id:
        findings.add(
            ArtifactFindingKind.CORRUPT_REFERENCED,
            _canonical_relative_path(reference.sha256),
            expected_sha256=reference.sha256,
            size_bytes=reference.size_bytes,
            detail="evaluation_run_id",
            owners=(owner,),
        )
    patch = manifest.artifacts.get("patch")
    if evaluation.run_status is not manifest.status or (
        patch is not None and evaluation.patch_hash != patch.sha256
    ):
        findings.add(
            ArtifactFindingKind.CORRUPT_REFERENCED,
            _canonical_relative_path(reference.sha256),
            expected_sha256=reference.sha256,
            size_bytes=reference.size_bytes,
            detail="evaluation_manifest_mismatch",
            owners=(owner,),
        )
    if evaluation.evidence != expected:
        findings.add(
            ArtifactFindingKind.CORRUPT_REFERENCED,
            _canonical_relative_path(reference.sha256),
            expected_sha256=reference.sha256,
            size_bytes=reference.size_bytes,
            detail="evaluation_evidence_mismatch",
            owners=(owner,),
        )


def _crosscheck_task_evaluations(
    tasks_by_run: dict[str, TaskSpec],
    evaluations_by_run: dict[
        str,
        tuple[EvaluationResult, ArtifactRef, ArtifactOwner],
    ],
    findings: _FindingBuilders,
) -> None:
    for run_id in sorted(tasks_by_run.keys() & evaluations_by_run.keys()):
        task = tasks_by_run[run_id]
        evaluation, reference, owner = evaluations_by_run[run_id]
        if evaluation.task_hash != task.task_content_hash:
            findings.add(
                ArtifactFindingKind.CORRUPT_REFERENCED,
                _canonical_relative_path(reference.sha256),
                expected_sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                detail="evaluation_task_hash_mismatch",
                owners=(owner,),
            )
        if "evaluator_version" not in task.metadata:
            continue
        task_evaluator_version = task.metadata["evaluator_version"]
        if not isinstance(task_evaluator_version, str) or not task_evaluator_version.strip():
            continue
        if task_evaluator_version != evaluation.evaluator_version:
            findings.add(
                ArtifactFindingKind.CORRUPT_REFERENCED,
                _canonical_relative_path(reference.sha256),
                expected_sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                detail="evaluation_evaluator_version_mismatch",
                owners=(owner,),
            )


def _canonical_relative_path(digest: str) -> str:
    return f"sha256/{digest[:2]}/{digest}"


def _read_reference(
    root: Path,
    reference: ArtifactRef,
    *,
    capture: bool,
    hash_budget: _HashBudget,
) -> _BlobRead:
    expected_relative = _canonical_relative_path(reference.sha256)
    if reference.storage_ref != expected_relative:
        return _BlobRead(
            ok=False,
            observed_sha256=None,
            size_bytes=None,
            data=None,
            kind=ArtifactFindingKind.CORRUPT_REFERENCED,
            detail="noncanonical_storage_ref",
        )
    if reference.size_bytes > _MAX_ARTIFACT_BYTES:
        return _BlobRead(
            ok=False,
            observed_sha256=None,
            size_bytes=reference.size_bytes,
            data=None,
            kind=ArtifactFindingKind.LIMIT_EXCEEDED,
            detail="referenced_artifact_bytes",
        )
    if capture and reference.size_bytes > _MAX_STRUCTURED_BYTES:
        return _BlobRead(
            ok=False,
            observed_sha256=None,
            size_bytes=reference.size_bytes,
            data=None,
            kind=ArtifactFindingKind.LIMIT_EXCEEDED,
            detail="structured_artifact_bytes",
        )

    relative = Path("sha256") / reference.sha256[:2] / reference.sha256
    path = root / relative
    components = (
        (root / "sha256", True),
        (root / "sha256" / reference.sha256[:2], True),
        (path, False),
    )
    for component, requires_directory in components:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=None,
                data=None,
                kind=ArtifactFindingKind.MISSING_REFERENCED,
                detail="missing_path",
                errno=errno.ENOENT,
            )
        except OSError as error:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=None,
                data=None,
                kind=ArtifactFindingKind.CORRUPT_REFERENCED,
                detail="path_lstat",
                errno=_errno(error),
            )
        if stat.S_ISLNK(metadata.st_mode):
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.SYMLINK,
                detail="referenced_path_symlink",
            )
        if requires_directory and not stat.S_ISDIR(metadata.st_mode):
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.SPECIAL_FILE,
                detail="referenced_parent_not_directory",
            )
        if not requires_directory and not stat.S_ISREG(metadata.st_mode):
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.SPECIAL_FILE,
                detail="referenced_path_not_regular",
            )
        if not requires_directory and metadata.st_nlink != 1:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.HARDLINK,
                detail="referenced_path_hardlink",
            )

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        kind = (
            ArtifactFindingKind.SYMLINK
            if error.errno == errno.ELOOP
            else (
                ArtifactFindingKind.MISSING_REFERENCED
                if error.errno == errno.ENOENT
                else ArtifactFindingKind.CORRUPT_REFERENCED
            )
        )
        return _BlobRead(
            ok=False,
            observed_sha256=None,
            size_bytes=None,
            data=None,
            kind=kind,
            detail="referenced_open",
            errno=_errno(error),
        )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.SPECIAL_FILE,
                detail="referenced_descriptor_not_regular",
            )
        if metadata.st_nlink != 1:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.HARDLINK,
                detail="referenced_path_hardlink",
            )
        if metadata.st_size != reference.size_bytes:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.CORRUPT_REFERENCED,
                detail="referenced_descriptor_size",
            )
        if metadata.st_size > hash_budget.remaining:
            return _BlobRead(
                ok=False,
                observed_sha256=None,
                size_bytes=metadata.st_size,
                data=None,
                kind=ArtifactFindingKind.LIMIT_EXCEEDED,
                detail="total_hashed_bytes",
            )
        digest = hashlib.sha256()
        captured = bytearray() if capture else None
        total = 0
        while True:
            read_size = min(
                _READ_CHUNK_BYTES,
                reference.size_bytes - total + 1,
                hash_budget.remaining + 1,
            )
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            next_total = total + len(chunk)
            if next_total > reference.size_bytes:
                return _BlobRead(
                    ok=False,
                    observed_sha256=None,
                    size_bytes=next_total,
                    data=None,
                    kind=ArtifactFindingKind.CORRUPT_REFERENCED,
                    detail="referenced_grew_beyond_claimed_size",
                )
            if not hash_budget.consume(len(chunk)):
                return _BlobRead(
                    ok=False,
                    observed_sha256=None,
                    size_bytes=next_total,
                    data=None,
                    kind=ArtifactFindingKind.LIMIT_EXCEEDED,
                    detail="total_hashed_bytes",
                )
            total = next_total
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)
        observed = digest.hexdigest()
        if total != reference.size_bytes or observed != reference.sha256:
            return _BlobRead(
                ok=False,
                observed_sha256=observed,
                size_bytes=total,
                data=None,
                kind=ArtifactFindingKind.CORRUPT_REFERENCED,
                detail="byte_identity",
            )
        return _BlobRead(
            ok=True,
            observed_sha256=observed,
            size_bytes=total,
            data=None if captured is None else bytes(captured),
        )
    except OSError as error:
        return _BlobRead(
            ok=False,
            observed_sha256=None,
            size_bytes=None,
            data=None,
            kind=ArtifactFindingKind.CORRUPT_REFERENCED,
            detail="referenced_read",
            errno=_errno(error),
        )
    finally:
        os.close(descriptor)


def _inventory_store(
    root: Path,
    reachable_owners: dict[str, set[ArtifactOwner]],
    findings: _FindingBuilders,
    hash_budget: _HashBudget,
) -> set[str]:
    exactly_observed: set[str] = set()
    counter = [0]
    root_entries = _scan_directory(root, ".", findings, counter)
    if root_entries is None:
        return exactly_observed
    for entry in root_entries:
        if entry.name != "sha256":
            _classify_noncanonical(entry, entry.name, findings)
            continue
        metadata = _entry_stat(entry, "sha256", findings)
        if metadata is None:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            owners = tuple(
                sorted(
                    {owner for values in reachable_owners.values() for owner in values},
                    key=_owner_key,
                )
            )
            findings.add(
                ArtifactFindingKind.SYMLINK,
                "sha256",
                size_bytes=metadata.st_size,
                detail="referenced_parent_symlink" if owners else None,
                owners=owners,
            )
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            findings.add(
                ArtifactFindingKind.NONCANONICAL_ENTRY,
                "sha256",
                size_bytes=metadata.st_size,
                detail="expected_directory",
            )
            continue
        prefix_entries = _scan_directory(Path(entry.path), "sha256", findings, counter)
        if prefix_entries is None:
            continue
        for prefix_entry in prefix_entries:
            prefix_relative = f"sha256/{prefix_entry.name}"
            prefix_metadata = _entry_stat(prefix_entry, prefix_relative, findings)
            if prefix_metadata is None:
                continue
            if stat.S_ISLNK(prefix_metadata.st_mode):
                owners = tuple(
                    sorted(
                        {
                            owner
                            for digest, values in reachable_owners.items()
                            if digest.startswith(prefix_entry.name)
                            for owner in values
                        },
                        key=_owner_key,
                    )
                )
                findings.add(
                    ArtifactFindingKind.SYMLINK,
                    prefix_relative,
                    size_bytes=prefix_metadata.st_size,
                    detail="referenced_parent_symlink" if owners else None,
                    owners=owners,
                )
                continue
            if _PREFIX_NAME.fullmatch(prefix_entry.name) is None or not stat.S_ISDIR(
                prefix_metadata.st_mode
            ):
                kind = (
                    ArtifactFindingKind.SPECIAL_FILE
                    if not stat.S_ISREG(prefix_metadata.st_mode)
                    and not stat.S_ISDIR(prefix_metadata.st_mode)
                    else ArtifactFindingKind.NONCANONICAL_ENTRY
                )
                findings.add(
                    kind,
                    prefix_relative,
                    size_bytes=prefix_metadata.st_size,
                    detail="invalid_shard",
                )
                continue
            _inventory_shard(
                Path(prefix_entry.path),
                prefix_entry.name,
                reachable_owners,
                findings,
                counter,
                exactly_observed,
                hash_budget,
            )
    return exactly_observed


def _inventory_shard(
    shard: Path,
    prefix: str,
    reachable_owners: dict[str, set[ArtifactOwner]],
    findings: _FindingBuilders,
    counter: list[int],
    exactly_observed: set[str],
    hash_budget: _HashBudget,
) -> None:
    entries = _scan_directory(shard, f"sha256/{prefix}", findings, counter)
    if entries is None:
        return
    for entry in entries:
        relative = f"sha256/{prefix}/{entry.name}"
        metadata = _entry_stat(entry, relative, findings)
        if metadata is None:
            continue
        canonical = _DIGEST_NAME.fullmatch(entry.name) is not None and entry.name.startswith(prefix)
        owners = tuple(sorted(reachable_owners.get(entry.name, ()), key=_owner_key))
        if stat.S_ISLNK(metadata.st_mode):
            findings.add(
                ArtifactFindingKind.SYMLINK,
                relative,
                expected_sha256=entry.name if canonical and owners else None,
                size_bytes=metadata.st_size,
                detail="referenced_path_symlink" if owners else None,
                owners=owners,
            )
            continue
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            findings.add(
                ArtifactFindingKind.HARDLINK,
                relative,
                expected_sha256=entry.name if canonical else None,
                size_bytes=metadata.st_size,
                detail="referenced_path_hardlink" if owners else None,
                owners=owners,
            )
            continue
        if entry.name.startswith(_TEMP_PREFIX):
            if stat.S_ISREG(metadata.st_mode):
                findings.add(
                    ArtifactFindingKind.TEMP_ORPHAN,
                    relative,
                    size_bytes=metadata.st_size,
                )
            else:
                findings.add(
                    ArtifactFindingKind.SPECIAL_FILE,
                    relative,
                    size_bytes=metadata.st_size,
                    detail="temporary_not_regular",
                )
            continue
        if not canonical:
            _classify_mode_noncanonical(metadata.st_mode, relative, metadata.st_size, findings)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            _classify_mode_noncanonical(metadata.st_mode, relative, metadata.st_size, findings)
            continue
        if entry.name in reachable_owners:
            exactly_observed.add(entry.name)
            continue
        if metadata.st_size > _MAX_ARTIFACT_BYTES:
            findings.add(
                ArtifactFindingKind.LIMIT_EXCEEDED,
                relative,
                expected_sha256=entry.name,
                size_bytes=metadata.st_size,
                detail="orphan_artifact_bytes",
            )
            continue
        path_hash = _hash_path_no_follow(Path(entry.path), hash_budget)
        if path_hash.limit_exceeded:
            findings.add(
                ArtifactFindingKind.LIMIT_EXCEEDED,
                relative,
                expected_sha256=entry.name,
                size_bytes=metadata.st_size,
                detail="total_hashed_bytes",
            )
            continue
        if path_hash.observed_sha256 is None:
            findings.add(
                ArtifactFindingKind.SCAN_ERROR,
                relative,
                expected_sha256=entry.name,
                size_bytes=metadata.st_size,
                detail="orphan_read",
                error_number=path_hash.errno,
            )
            continue
        findings.add(
            (
                ArtifactFindingKind.VALID_FINALIZED_ORPHAN
                if path_hash.observed_sha256 == entry.name
                else ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN
            ),
            relative,
            expected_sha256=entry.name,
            observed_sha256=path_hash.observed_sha256,
            size_bytes=metadata.st_size,
        )


def _scan_directory(
    path: Path,
    relative: str,
    findings: _FindingBuilders,
    counter: list[int],
) -> tuple[os.DirEntry[str], ...] | None:
    try:
        with os.scandir(path) as iterator:
            entries: list[os.DirEntry[str]] = []
            for entry in iterator:
                counter[0] += 1
                if counter[0] > _MAX_INVENTORY_ENTRIES:
                    findings.add(
                        ArtifactFindingKind.LIMIT_EXCEEDED,
                        relative,
                        detail="inventory_entry_limit",
                    )
                    return None
                try:
                    entry.name.encode("utf-8")
                except UnicodeEncodeError:
                    raw_hex = os.fsencode(entry.name).hex()
                    display = (
                        f"<raw-name-{raw_hex}>"
                        if relative == "."
                        else f"{relative}/<raw-name-{raw_hex}>"
                    )
                    findings.add(
                        ArtifactFindingKind.SCAN_ERROR,
                        display,
                        detail=f"undecodable_entry_name_raw_hex:{raw_hex}",
                    )
                    continue
                entries.append(entry)
    except OSError as error:
        findings.add(
            ArtifactFindingKind.SCAN_ERROR,
            relative,
            detail="scandir",
            error_number=_errno(error),
        )
        return None
    return tuple(sorted(entries, key=lambda item: item.name))


def _entry_stat(
    entry: os.DirEntry[str],
    relative: str,
    findings: _FindingBuilders,
) -> os.stat_result | None:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as error:
        findings.add(
            ArtifactFindingKind.SCAN_ERROR,
            relative,
            detail="lstat",
            error_number=_errno(error),
        )
        return None


def _classify_noncanonical(
    entry: os.DirEntry[str],
    relative: str,
    findings: _FindingBuilders,
) -> None:
    metadata = _entry_stat(entry, relative, findings)
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        findings.add(ArtifactFindingKind.SYMLINK, relative, size_bytes=metadata.st_size)
    else:
        _classify_mode_noncanonical(metadata.st_mode, relative, metadata.st_size, findings)


def _classify_mode_noncanonical(
    mode: int,
    relative: str,
    size_bytes: int,
    findings: _FindingBuilders,
) -> None:
    kind = (
        ArtifactFindingKind.NONCANONICAL_ENTRY
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode)
        else ArtifactFindingKind.SPECIAL_FILE
    )
    findings.add(kind, relative, size_bytes=size_bytes)


def _hash_path_no_follow(path: Path, hash_budget: _HashBudget) -> _PathHash:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return _PathHash(observed_sha256=None, errno=_errno(error))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            return _PathHash(observed_sha256=None, errno=errno.EINVAL)
        if metadata.st_size > _MAX_ARTIFACT_BYTES or metadata.st_size > hash_budget.remaining:
            return _PathHash(observed_sha256=None, limit_exceeded=True)
        digest = hashlib.sha256()
        total = 0
        while True:
            read_size = min(
                _READ_CHUNK_BYTES,
                _MAX_ARTIFACT_BYTES - total + 1,
                hash_budget.remaining + 1,
            )
            chunk = os.read(descriptor, read_size)
            if not chunk:
                break
            next_total = total + len(chunk)
            if next_total > _MAX_ARTIFACT_BYTES or not hash_budget.consume(len(chunk)):
                return _PathHash(observed_sha256=None, limit_exceeded=True)
            total = next_total
            digest.update(chunk)
        return _PathHash(observed_sha256=digest.hexdigest())
    except OSError as error:
        return _PathHash(observed_sha256=None, errno=_errno(error))
    finally:
        os.close(descriptor)


def _errno(error: OSError) -> int:
    return error.errno if error.errno is not None and error.errno >= 0 else errno.EIO


def _owner_key(owner: ArtifactOwner) -> tuple[str, tuple[str, ...]]:
    return owner.run_id, owner.path


def _finding_key(
    finding: ArtifactFinding,
) -> tuple[
    str,
    str,
    str,
    str,
    int,
    str,
    int,
    tuple[tuple[str, tuple[str, ...]], ...],
]:
    return (
        finding.kind.value,
        finding.relative_path,
        finding.expected_sha256 or "",
        finding.observed_sha256 or "",
        -1 if finding.size_bytes is None else finding.size_bytes,
        finding.detail or "",
        -1 if finding.errno is None else finding.errno,
        tuple(_owner_key(owner) for owner in finding.owners),
    )
