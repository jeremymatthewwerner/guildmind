from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    EvaluationResult,
    RunManifest,
    RunStatus,
    TaskSpec,
    canonical_json,
    canonical_sha256,
)
from guildmind.storage import (
    ArtifactAudit,
    ArtifactFindingKind,
    ArtifactOwner,
    FileArtifactStore,
    ReachableArtifact,
    VerifiedRunRoot,
    audit_artifact_store,
)

_START = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _manifest(
    run_id: str,
    artifacts: dict[str, ArtifactRef],
    *,
    terminal: bool = False,
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        experiment_id="artifact-audit",
        task_id="fixture-audit",
        candidate_id="audit-candidate",
        requested_model="audit-model",
        seed=0,
        environment_digest=f"sha256:{'a' * 64}",
        code_revision="audit-test",
        budget_limits=BudgetLimits(max_model_calls=1),
        status=RunStatus.SUCCEEDED if terminal else RunStatus.PENDING,
        created_at=_START,
        started_at=_START + timedelta(seconds=1) if terminal else None,
        finished_at=_START + timedelta(seconds=2) if terminal else None,
        artifacts=artifacts,
    )


def _task_graph(
    store: FileArtifactStore,
    *,
    evaluator_version: str | None = None,
) -> tuple[ArtifactRef, tuple[ArtifactRef, ...]]:
    problem = store.put_text("Fix the function.")
    repository = store.put_bytes(
        b'{"files":[]}\n', media_type="application/vnd.guildmind.tree+json"
    )
    visible = (
        store.put_bytes(b"def test_one(): pass\n", media_type="text/x-python"),
        store.put_bytes(b"def test_two(): pass\n", media_type="text/x-python"),
    )
    task = TaskSpec(
        task_id="fixture-audit",
        source="test",
        split="fixture",
        repository="guildmind/fixture-audit",
        repository_commit="fixture-v1",
        image_digest=f"sha256:{'a' * 64}",
        task_content_hash="c" * 64,
        problem_statement=problem,
        repository_snapshot=repository,
        visible_tests=visible,
        metadata=({} if evaluator_version is None else {"evaluator_version": evaluator_version}),
    )
    task_reference = store.put_text(
        canonical_json(task),
        media_type="application/vnd.guildmind.task+json",
    )
    return task_reference, (problem, repository, *visible)


def _isolated_task_artifact(store: FileArtifactStore) -> ArtifactRef:
    missing = ArtifactRef(
        media_type="application/octet-stream",
        size_bytes=0,
        sha256="f" * 64,
        storage_ref=f"sha256/ff/{'f' * 64}",
    )
    task = TaskSpec(
        task_id="fixture-audit",
        source="test",
        split="fixture",
        repository="guildmind/fixture-audit",
        repository_commit="fixture-v1",
        image_digest=f"sha256:{'a' * 64}",
        task_content_hash="c" * 64,
        problem_statement=missing,
        repository_snapshot=missing,
    )
    return store.put_text(
        canonical_json(task),
        media_type="application/vnd.guildmind.task+json",
    )


def _root(
    manifest: RunManifest,
    *,
    manifest_revision: int = 0,
    event_count: int = 1,
) -> VerifiedRunRoot:
    return VerifiedRunRoot(
        manifest=manifest,
        manifest_revision=manifest_revision,
        manifest_sha256=canonical_sha256(manifest),
        event_count=event_count,
        head_event_sha256=canonical_sha256({"event_count": event_count, "run_id": manifest.run_id}),
    )


def _roots(*manifests: RunManifest) -> tuple[VerifiedRunRoot, ...]:
    return tuple(_root(manifest) for manifest in manifests)


def _finding_kinds(audit: ArtifactAudit) -> tuple[ArtifactFindingKind, ...]:
    return tuple(item.kind for item in audit.findings)


def test_nested_task_reachability_verifies_every_typed_reference(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    task, nested = _task_graph(store)

    audit = audit_artifact_store(_roots(_manifest("run-a", {"task_spec": task})), store)

    assert audit.complete
    assert audit.quarantine_allowed
    assert audit.findings == ()
    assert {item.sha256 for item in audit.reachable} == {
        task.sha256,
        *(reference.sha256 for reference in nested),
    }
    visible = next(item for item in audit.reachable if item.sha256 == nested[2].sha256)
    assert visible.owners == (
        ArtifactOwner(
            run_id="run-a",
            path=("task_spec", "visible_tests", "00000000"),
        ),
    )
    assert all(item.bytes_verified for item in audit.reachable)


def test_noncanonical_structured_json_is_rejected_without_traversal(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    canonical_task, nested = _task_graph(store)
    canonical_bytes = store.get_bytes(canonical_task)
    duplicate_key_bytes = b'{"task_id":"shadow",' + canonical_bytes[1:]
    task = store.put_bytes(
        duplicate_key_bytes,
        media_type="application/vnd.guildmind.task+json",
    )

    audit = audit_artifact_store(_roots(_manifest("run-a", {"task_spec": task})), store)

    finding = next(item for item in audit.findings if item.detail == "task_spec_noncanonical_json")
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("task_spec",)),)
    assert {item.sha256 for item in audit.reachable} == {task.sha256}
    assert not {reference.sha256 for reference in nested} & {
        item.sha256 for item in audit.reachable
    }
    assert not audit.quarantine_allowed


@pytest.mark.parametrize("nonfinite", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_structured_json_is_a_finding_not_an_audit_exception(
    tmp_path: Path,
    nonfinite: str,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    canonical_task, _ = _task_graph(store)
    raw = store.get_bytes(canonical_task).decode("utf-8")
    noncanonical = raw.replace(
        '"metadata":{}',
        f'"metadata":{{"nonfinite":{nonfinite}}}',
        1,
    )
    assert noncanonical != raw
    task = store.put_text(
        noncanonical,
        media_type="application/vnd.guildmind.task+json",
    )

    audit = audit_artifact_store(_roots(_manifest("run-a", {"task_spec": task})), store)

    finding = next(item for item in audit.findings if item.detail == "task_spec_noncanonical_json")
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("task_spec",)),)
    assert not audit.quarantine_allowed


def test_shared_digest_deduplicates_bytes_and_preserves_stable_owners(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    shared = store.put_text("shared")
    manifests = (
        _manifest("run-b", {"patch": shared}),
        _manifest("run-a", {"patch": shared}),
    )

    audit = audit_artifact_store(_roots(*manifests), store)

    assert len(audit.reachable) == 1
    assert audit.reachable[0].owners == (
        ArtifactOwner(run_id="run-a", path=("patch",)),
        ArtifactOwner(run_id="run-b", path=("patch",)),
    )
    assert audit.findings == ()


def test_verified_size_wins_conflicting_same_digest_metadata_regardless_of_order(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    correct = store.put_text("trusted")
    wrong = correct.model_copy(update={"size_bytes": correct.size_bytes + 1})
    wrong_first = audit_artifact_store(
        _roots(_manifest("run-a", {"a": wrong, "b": correct})),
        store,
    )
    correct_first = audit_artifact_store(
        _roots(_manifest("run-a", {"a": correct, "b": wrong})),
        store,
    )

    assert wrong_first.reachable == correct_first.reachable
    assert wrong_first.reachable[0].size_bytes == correct.size_bytes
    assert wrong_first.reachable[0].bytes_verified
    assert any(
        finding.detail == "conflicting_reference_metadata" for finding in wrong_first.findings
    )
    assert any(
        finding.detail == "conflicting_reference_metadata" for finding in correct_first.findings
    )
    assert not wrong_first.quarantine_allowed
    assert not correct_first.quarantine_allowed


def test_snapshot_identity_is_deterministic_order_independent_and_root_bound(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    root_b = _root(
        _manifest("run-b", {}),
        manifest_revision=2,
        event_count=5,
    )
    root_a = _root(_manifest("run-a", {}))

    forward = audit_artifact_store((root_a, root_b), store)
    reverse = audit_artifact_store((root_b, root_a), store)
    changed = audit_artifact_store(
        (
            root_a,
            replace(
                root_b,
                head_event_sha256=canonical_sha256({"changed": root_b.manifest.run_id}),
            ),
        ),
        store,
    )

    expected = canonical_sha256(
        {
            "roots": [
                {
                    "event_count": root.event_count,
                    "head_event_sha256": root.head_event_sha256,
                    "manifest_revision": root.manifest_revision,
                    "manifest_sha256": root.manifest_sha256,
                    "run_id": root.manifest.run_id,
                }
                for root in (root_a, root_b)
            ],
            "schema_version": "guildmind.verified-run-root-snapshot/v1",
        }
    )
    assert forward.snapshot_sha256 == reverse.snapshot_sha256 == expected
    assert changed.snapshot_sha256 != forward.snapshot_sha256


def test_inconsistent_or_duplicate_verified_roots_are_rejected(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    root = _root(_manifest("run-a", {}))
    invalid_roots = (
        replace(root, manifest_sha256="0" * 64),
        replace(root, manifest_revision=-1),
        replace(root, event_count=0),
        replace(root, manifest_revision=1, event_count=1),
        replace(root, head_event_sha256="A" * 64),
    )

    for invalid in invalid_roots:
        with pytest.raises(ValueError, match="verified run root"):
            audit_artifact_store((invalid,), store)
    with pytest.raises(ValueError, match="duplicate verified run root: run-a"):
        audit_artifact_store((root, root), store)


def test_audit_model_rejects_quarantine_claim_with_unverified_reachable_bytes(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": reference})),
        store,
    )
    unverified = audit.reachable[0].model_copy(update={"bytes_verified": False})

    with pytest.raises(ValueError, match="byte-verified trusted graph"):
        ArtifactAudit.model_validate(
            {
                "complete": True,
                "findings": (),
                "quarantine_allowed": True,
                "reachable": (unverified,),
                "snapshot_sha256": audit.snapshot_sha256,
            }
        )


def test_reachable_model_rejects_noncanonical_storage_path(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": reference})),
        store,
    )
    payload = audit.reachable[0].model_dump(mode="python")
    payload["storage_ref"] = "arbitrary/live/path"

    with pytest.raises(ValueError, match="canonical digest path"):
        ReachableArtifact.model_validate(payload)


def test_evaluation_evidence_is_crosschecked_and_deduplicated(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    stdout = store.put_text("stdout")
    stderr = store.put_text("stderr")
    evaluation = EvaluationResult(
        evaluation_id="evaluation-a",
        run_id="run-a",
        run_status=RunStatus.SUCCEEDED,
        evaluator_version="audit-evaluator",
        task_hash="c" * 64,
        patch_hash="d" * 64,
        outcome="passed",
        score=1.0,
        result={},
        result_sha256=canonical_sha256({}),
        evidence=(stdout, stderr),
        evaluated_at=_START + timedelta(seconds=2),
    )
    evaluation_ref = store.put_text(
        canonical_json(evaluation),
        media_type="application/vnd.guildmind.evaluation+json",
    )
    manifest = _manifest(
        "run-a",
        {
            "evaluation": evaluation_ref,
            "evaluation_stderr": stderr,
            "evaluation_stdout": stdout,
        },
        terminal=True,
    )

    audit = audit_artifact_store(_roots(manifest), store)

    assert audit.findings == ()
    reachable_stdout = next(item for item in audit.reachable if item.sha256 == stdout.sha256)
    assert reachable_stdout.owners == (
        ArtifactOwner(run_id="run-a", path=("evaluation", "evidence", "00000000")),
        ArtifactOwner(run_id="run-a", path=("evaluation_stdout",)),
    )


@pytest.mark.parametrize(
    ("task_hash", "task_evaluator_version", "expected_detail"),
    [
        ("b" * 64, None, "evaluation_task_hash_mismatch"),
        ("c" * 64, "frozen-evaluator", "evaluation_evaluator_version_mismatch"),
    ],
    ids=("task-hash", "evaluator-version"),
)
def test_evaluation_identity_must_match_bound_task_spec(
    tmp_path: Path,
    task_hash: str,
    task_evaluator_version: str | None,
    expected_detail: str,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    task, _ = _task_graph(store, evaluator_version=task_evaluator_version)
    stdout = store.put_text("stdout")
    stderr = store.put_text("stderr")
    evaluation = EvaluationResult(
        evaluation_id="evaluation-a",
        run_id="run-a",
        run_status=RunStatus.SUCCEEDED,
        evaluator_version="audit-evaluator",
        task_hash=task_hash,
        patch_hash="d" * 64,
        outcome="passed",
        score=1.0,
        result={},
        result_sha256=canonical_sha256({}),
        evidence=(stdout, stderr),
        evaluated_at=_START + timedelta(seconds=2),
    )
    evaluation_ref = store.put_text(
        canonical_json(evaluation),
        media_type="application/vnd.guildmind.evaluation+json",
    )
    manifest = _manifest(
        "run-a",
        {
            "evaluation": evaluation_ref,
            "evaluation_stderr": stderr,
            "evaluation_stdout": stdout,
            "task_spec": task,
        },
        terminal=True,
    )

    audit = audit_artifact_store(_roots(manifest), store)

    assert len(audit.findings) == 1
    finding = audit.findings[0]
    assert finding.kind is ArtifactFindingKind.CORRUPT_REFERENCED
    assert finding.detail == expected_detail
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("evaluation",)),)
    assert not audit.quarantine_allowed


@pytest.mark.parametrize("corrupt", [False, True], ids=("missing", "corrupt"))
def test_missing_or_corrupt_referenced_bytes_block_quarantine(
    tmp_path: Path,
    corrupt: bool,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    if corrupt:
        store.path_for(reference).write_text("tampered", encoding="utf-8")
        expected = ArtifactFindingKind.CORRUPT_REFERENCED
    else:
        store.path_for(reference).unlink()
        expected = ArtifactFindingKind.MISSING_REFERENCED

    audit = audit_artifact_store(_roots(_manifest("run-a", {"patch": reference})), store)

    assert audit.complete
    assert not audit.quarantine_allowed
    finding = next(item for item in audit.findings if item.kind is expected)
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("patch",)),)
    assert audit.reachable[0].bytes_verified is False


def test_noncanonical_committed_storage_ref_is_reported_with_canonical_output(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    stored = store.put_text("trusted")
    noncanonical = stored.model_copy(update={"storage_ref": "live/alias"})

    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": noncanonical})),
        store,
    )

    finding = next(item for item in audit.findings if item.detail == "noncanonical_storage_ref")
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("patch",)),)
    assert audit.reachable[0].storage_ref == stored.storage_ref
    assert not audit.reachable[0].bytes_verified
    assert not audit.quarantine_allowed


def test_sparse_structured_reference_size_mismatch_aborts_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    task = _isolated_task_artifact(store)
    with store.path_for(task).open("r+b") as stream:
        stream.truncate(task.size_bytes + 1_000_000)

    def unexpected_read(_: int, __: int) -> bytes:
        raise AssertionError("size-mismatched artifact must not be read")

    monkeypatch.setattr("guildmind.storage.integrity.os.read", unexpected_read)

    audit = audit_artifact_store(_roots(_manifest("run-a", {"task_spec": task})), store)

    finding = next(item for item in audit.findings if item.detail == "referenced_descriptor_size")
    assert finding.size_bytes == task.size_bytes + 1_000_000
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("task_spec",)),)
    assert not audit.reachable[0].bytes_verified
    assert not audit.quarantine_allowed


def test_oversized_structured_reference_is_rejected_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    task = _isolated_task_artifact(store)
    monkeypatch.setattr(
        "guildmind.storage.integrity._MAX_STRUCTURED_BYTES",
        task.size_bytes - 1,
    )

    def unexpected_open(*_: object) -> int:
        raise AssertionError("oversized structured artifact must not be opened")

    monkeypatch.setattr("guildmind.storage.integrity.os.open", unexpected_open)

    audit = audit_artifact_store(_roots(_manifest("run-a", {"task_spec": task})), store)

    finding = next(item for item in audit.findings if item.detail == "structured_artifact_bytes")
    assert finding.kind is ArtifactFindingKind.LIMIT_EXCEEDED
    assert not audit.complete
    assert not audit.quarantine_allowed


def test_total_hash_budget_is_shared_by_reachable_and_orphan_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"live", media_type="application/octet-stream")
    orphan = store.put_bytes(b"orphan", media_type="application/octet-stream")
    monkeypatch.setattr(
        "guildmind.storage.integrity._MAX_TOTAL_HASHED_BYTES",
        reference.size_bytes + orphan.size_bytes - 1,
    )

    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": reference})),
        store,
    )

    finding = next(item for item in audit.findings if item.detail == "total_hashed_bytes")
    assert finding.kind is ArtifactFindingKind.LIMIT_EXCEEDED
    assert finding.relative_path == orphan.storage_ref
    assert audit.reachable[0].bytes_verified
    assert not audit.complete
    assert not audit.quarantine_allowed


def test_valid_and_corrupt_finalized_orphans_are_distinguished(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    valid = store.put_text("valid orphan")
    corrupt = store.put_text("corrupt orphan")
    store.path_for(corrupt).write_text("changed bytes", encoding="utf-8")

    audit = audit_artifact_store((), store)

    by_path = {item.relative_path: item for item in audit.findings}
    assert by_path[valid.storage_ref].kind is ArtifactFindingKind.VALID_FINALIZED_ORPHAN
    assert by_path[valid.storage_ref].observed_sha256 == valid.sha256
    assert by_path[corrupt.storage_ref].kind is ArtifactFindingKind.CORRUPT_FINALIZED_ORPHAN
    assert by_path[corrupt.storage_ref].observed_sha256 != corrupt.sha256
    assert audit.complete
    assert audit.quarantine_allowed
    assert audit.reachable == ()


def test_temporary_orphan_is_inventoried_without_reading_or_deleting_it(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    shard = store.root / "sha256" / "ab"
    shard.mkdir(parents=True)
    temporary = shard / ".artifact-interrupted"
    temporary.write_bytes(b"partial")

    audit = audit_artifact_store((), store)

    assert _finding_kinds(audit) == (ArtifactFindingKind.TEMP_ORPHAN,)
    assert audit.findings[0].relative_path == "sha256/ab/.artifact-interrupted"
    assert audit.findings[0].size_bytes == 7
    assert temporary.read_bytes() == b"partial"
    assert audit.quarantine_allowed


def test_replaced_store_root_symlink_is_rejected_without_scanning_target(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    outside_store = FileArtifactStore(tmp_path / "outside-artifacts")
    outside = outside_store.put_text("outside bytes must not be audited")
    outside_path = outside_store.path_for(outside)
    original_bytes = outside_path.read_bytes()
    store.root.rmdir()
    store.root.symlink_to(outside_store.root, target_is_directory=True)

    audit = audit_artifact_store((), store)

    assert not audit.complete
    assert not audit.quarantine_allowed
    assert audit.reachable == ()
    assert len(audit.findings) == 1
    assert audit.findings[0].kind is ArtifactFindingKind.SCAN_ERROR
    assert audit.findings[0].relative_path == "."
    assert audit.findings[0].detail == "artifact_store_root_identity"
    assert outside.storage_ref not in {finding.relative_path for finding in audit.findings}
    assert outside_path.read_bytes() == original_bytes


def test_replaced_store_ancestor_symlink_is_rejected_without_scanning_target(
    tmp_path: Path,
) -> None:
    original_parent = tmp_path / "original-parent"
    store = FileArtifactStore(original_parent / "artifacts")
    outside_parent = tmp_path / "outside-parent"
    outside_store = FileArtifactStore(outside_parent / "artifacts")
    outside = outside_store.put_text("outside ancestor bytes must not be audited")
    outside_path = outside_store.path_for(outside)
    original_bytes = outside_path.read_bytes()
    moved_parent = tmp_path / "moved-original-parent"
    original_parent.rename(moved_parent)
    original_parent.symlink_to(outside_parent, target_is_directory=True)

    audit = audit_artifact_store((), store)

    assert not audit.complete
    assert not audit.quarantine_allowed
    assert audit.reachable == ()
    assert len(audit.findings) == 1
    assert audit.findings[0].kind is ArtifactFindingKind.SCAN_ERROR
    assert audit.findings[0].detail == "artifact_store_root_identity"
    assert outside.storage_ref not in {finding.relative_path for finding in audit.findings}
    assert outside_path.read_bytes() == original_bytes


def test_replaced_store_root_real_directory_fails_captured_identity(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    original_root = tmp_path / "original-artifacts"
    store.root.rename(original_root)
    store.root.mkdir()
    untrusted = store.root / "must-not-scan"
    untrusted.write_text("replacement contents", encoding="utf-8")

    audit = audit_artifact_store((), store)

    assert not audit.complete
    assert not audit.quarantine_allowed
    assert audit.reachable == ()
    assert len(audit.findings) == 1
    assert audit.findings[0].kind is ArtifactFindingKind.SCAN_ERROR
    assert audit.findings[0].detail == "artifact_store_root_identity"
    assert untrusted.read_text(encoding="utf-8") == "replacement contents"


@pytest.mark.parametrize("component", ["top", "shard", "digest"])
def test_case_varied_cas_alias_never_permits_quarantine(
    tmp_path: Path,
    component: str,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    canonical = store.path_for(reference)
    if component == "top":
        source = store.root / "sha256"
        alias = store.root / "SHA256"
    elif component == "shard":
        source = canonical.parent
        alias = source.with_name(source.name.upper())
    else:
        source = canonical
        alias = source.with_name(source.name.upper())
    assert source.name != alias.name
    source.rename(alias)

    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": reference})),
        store,
    )

    finding = next(item for item in audit.findings if item.detail == "canonical_path_not_observed")
    assert finding.kind is ArtifactFindingKind.CORRUPT_REFERENCED
    assert finding.relative_path == reference.storage_ref
    assert finding.owners == (ArtifactOwner(run_id="run-a", path=("patch",)),)
    assert not audit.quarantine_allowed
    assert alias.exists()


def test_undecodable_directory_entry_is_json_safe_and_makes_scan_incomplete(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    raw_name = b"\xff-invalid-name"
    raw_path = os.fsencode(store.root) + b"/" + raw_name
    try:
        descriptor = os.open(raw_path, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError as error:
        pytest.skip(f"filesystem rejected an invalid UTF-8 name: {error}")
    else:
        os.close(descriptor)

    audit = audit_artifact_store((), store)

    raw_hex = raw_name.hex()
    finding = next(
        item
        for item in audit.findings
        if item.detail == f"undecodable_entry_name_raw_hex:{raw_hex}"
    )
    assert finding.kind is ArtifactFindingKind.SCAN_ERROR
    assert finding.relative_path == f"<raw-name-{raw_hex}>"
    assert not audit.complete
    assert not audit.quarantine_allowed
    assert canonical_json(audit).encode("utf-8")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_symlink_noncanonical_and_special_entries_are_never_followed(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "outside-secret"
    outside.write_text("do not read or change", encoding="utf-8")
    (store.root / "escape-link").symlink_to(outside)
    (store.root / "unexpected").write_text("noncanonical", encoding="utf-8")
    fifo = store.root / "named-pipe"
    os.mkfifo(fifo)

    audit = audit_artifact_store((), store)

    assert set(_finding_kinds(audit)) == {
        ArtifactFindingKind.NONCANONICAL_ENTRY,
        ArtifactFindingKind.SPECIAL_FILE,
        ArtifactFindingKind.SYMLINK,
    }
    assert outside.read_text(encoding="utf-8") == "do not read or change"
    assert (store.root / "escape-link").is_symlink()
    assert stat_is_fifo(fifo)
    assert audit.complete
    assert audit.quarantine_allowed


def test_referenced_symlink_is_owned_and_blocks_quarantine_without_following(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    outside = tmp_path / "outside-secret"
    outside.write_text("outside remains intact", encoding="utf-8")
    path = store.path_for(reference)
    path.unlink()
    path.symlink_to(outside)

    audit = audit_artifact_store(_roots(_manifest("run-a", {"patch": reference})), store)

    owned = [
        item for item in audit.findings if item.kind is ArtifactFindingKind.SYMLINK and item.owners
    ]
    assert len(owned) == 1
    assert owned[0].owners == (ArtifactOwner(run_id="run-a", path=("patch",)),)
    assert not audit.quarantine_allowed
    assert outside.read_text(encoding="utf-8") == "outside remains intact"


@pytest.mark.parametrize("parent", ["sha256", "shard"])
def test_referenced_parent_symlink_is_owned_and_never_follows_outside_cas(
    tmp_path: Path,
    parent: str,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    outside_store = FileArtifactStore(tmp_path / "outside-artifacts")
    outside_reference = outside_store.put_text("trusted")
    outside_path = outside_store.path_for(outside_reference)
    original_bytes = outside_path.read_bytes()
    if parent == "sha256":
        source = store.root / "sha256"
        target = outside_store.root / "sha256"
    else:
        source = store.root / "sha256" / reference.sha256[:2]
        target = outside_store.root / "sha256" / reference.sha256[:2]
    backup = tmp_path / f"original-{parent}"
    source.rename(backup)
    source.symlink_to(target, target_is_directory=True)

    audit = audit_artifact_store(
        _roots(_manifest("run-a", {"patch": reference})),
        store,
    )

    owner = ArtifactOwner(run_id="run-a", path=("patch",))
    assert any(
        finding.kind is ArtifactFindingKind.SYMLINK and owner in finding.owners
        for finding in audit.findings
    )
    assert any(
        finding.detail == "canonical_path_not_observed" and owner in finding.owners
        for finding in audit.findings
    )
    assert not audit.quarantine_allowed
    assert source.is_symlink()
    assert outside_path.read_bytes() == original_bytes


def test_nonempty_cas_without_roots_is_a_complete_orphan_inventory(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"unbound", media_type="application/octet-stream")

    audit = audit_artifact_store((), store)

    assert audit.reachable == ()
    assert len(audit.findings) == 1
    assert audit.findings[0].kind is ArtifactFindingKind.VALID_FINALIZED_ORPHAN
    assert audit.findings[0].relative_path == reference.storage_ref
    assert audit.complete
    assert audit.quarantine_allowed


def stat_is_fifo(path: Path) -> bool:
    import stat

    return stat.S_ISFIFO(path.lstat().st_mode)
