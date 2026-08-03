"""Real-process SIGKILL coverage for atomic CAS publication boundaries.

The child installs test-only wrappers around the no-replace rename or directory
``fsync``, announces the exact boundary over a pipe, and blocks. The parent sends
``SIGKILL`` only after that announcement, so the matrix does not depend on timing or
add production crash hooks.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
from contextlib import suppress
from enum import StrEnum
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import NoReturn

import pytest

import guildmind.storage.artifacts as artifact_module
from guildmind.domain import sha256_bytes
from guildmind.storage import (
    ArtifactFindingKind,
    FileArtifactStore,
    audit_artifact_store,
)

_DATA = b"atomic CAS publication crash evidence\n"


class _PublicationBoundary(StrEnum):
    ROOT_DIRECTORY_CREATED = "root_directory_created"
    SHA256_DIRECTORY_CREATED = "sha256_directory_created"
    SHARD_DIRECTORY_CREATED = "shard_directory_created"
    PRE_PUBLICATION = "pre_publication"
    POST_PUBLICATION = "post_publication"
    PRE_DIRECTORY_FSYNC = "pre_directory_fsync"


def _enter_and_block(barrier: Connection, boundary: _PublicationBoundary) -> NoReturn:
    barrier.send(("entered", boundary.value))
    barrier.recv()
    raise AssertionError("the artifact publication barrier was unexpectedly released")


def _child_publish_at_boundary(
    root_text: str,
    boundary_text: str,
    barrier: Connection,
) -> None:
    boundary = _PublicationBoundary(boundary_text)
    root = Path(root_text)
    digest = sha256_bytes(_DATA)
    target_parent = root / "sha256" / digest[:2]
    mkdir_target = {
        _PublicationBoundary.ROOT_DIRECTORY_CREATED: root,
        _PublicationBoundary.SHA256_DIRECTORY_CREATED: root / "sha256",
        _PublicationBoundary.SHARD_DIRECTORY_CREATED: target_parent,
    }.get(boundary)
    real_mkdir = os.mkdir

    def wrapped_mkdir(path: Path, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if dir_fd is None:
            real_mkdir(path, mode)
        else:
            real_mkdir(path, mode, dir_fd=dir_fd)
        if path == mkdir_target:
            _enter_and_block(barrier, boundary)

    if boundary is _PublicationBoundary.ROOT_DIRECTORY_CREATED:
        os.__dict__["mkdir"] = wrapped_mkdir
    store = FileArtifactStore(root, trusted_base=root.parent)
    if boundary in {
        _PublicationBoundary.SHA256_DIRECTORY_CREATED,
        _PublicationBoundary.SHARD_DIRECTORY_CREATED,
    }:
        os.__dict__["mkdir"] = wrapped_mkdir
    real_rename = artifact_module._rename_noreplace

    if boundary is _PublicationBoundary.PRE_PUBLICATION:

        def wrapped_rename(source: Path, target: Path) -> bool:
            del source, target
            _enter_and_block(barrier, boundary)

        artifact_module._rename_noreplace = wrapped_rename
    elif boundary is _PublicationBoundary.POST_PUBLICATION:

        def wrapped_rename(source: Path, target: Path) -> bool:
            published = real_rename(source, target)
            if not published:
                raise AssertionError("unexpected competing artifact publisher")
            _enter_and_block(barrier, boundary)

        artifact_module._rename_noreplace = wrapped_rename
    else:
        real_fsync_directory = store._fsync_directory

        def wrapped_fsync_directory(directory: Path) -> None:
            if directory == target_parent:
                _enter_and_block(barrier, boundary)
            real_fsync_directory(directory)

        store.__dict__["_fsync_directory"] = wrapped_fsync_directory

    try:
        store.put_bytes(_DATA, media_type="application/octet-stream")
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(("error", boundary.value, type(error).__name__, str(error)))
        raise
    else:
        barrier.send(("error", boundary.value, "UnexpectedReturn", "publication completed"))
    finally:
        barrier.close()


def _kill_at_publication_boundary(root: Path, boundary: _PublicationBoundary) -> None:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_publish_at_boundary,
        args=(str(root), boundary.value, child_barrier),
        name=f"guildmind-artifact-crash-{boundary.value}",
    )
    process.start()
    child_barrier.close()
    try:
        ready = wait((parent_barrier, process.sentinel), timeout=30)
        if parent_barrier not in ready:
            process.join(timeout=1)
            pytest.fail(
                "child exited before the publication barrier: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        try:
            message = parent_barrier.recv()
        except EOFError:
            process.join(timeout=1)
            pytest.fail(
                "child closed the publication barrier: "
                f"boundary={boundary.value}, exitcode={process.exitcode}"
            )
        if message != ("entered", boundary.value):
            process.join(timeout=5)
            pytest.fail(f"child failed before the expected publication barrier: {message!r}")

        pid = process.pid
        assert pid is not None
        os.kill(pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        parent_barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def _audit_finding_kinds(root: Path) -> tuple[ArtifactFindingKind, ...]:
    reader = FileArtifactStore.open_existing_read_only(root, trusted_base=root.parent)
    audit = audit_artifact_store((), reader)
    assert audit.complete
    assert audit.quarantine_allowed
    return tuple(finding.kind for finding in audit.findings)


def _prepare_directory_creation_boundary(root: Path, boundary: _PublicationBoundary) -> None:
    root.parent.mkdir(parents=True)
    if boundary is _PublicationBoundary.ROOT_DIRECTORY_CREATED:
        return
    FileArtifactStore(root, trusted_base=root.parent)
    if boundary is _PublicationBoundary.SHARD_DIRECTORY_CREATED:
        os.mkdir(root / "sha256")
        FileArtifactStore._fsync_directory(root)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or (sys.platform != "darwin" and not sys.platform.startswith("linux")),
    reason="requires POSIX SIGKILL and a supported exclusive-rename host",
)
@pytest.mark.parametrize(
    "boundary",
    (
        _PublicationBoundary.ROOT_DIRECTORY_CREATED,
        _PublicationBoundary.SHA256_DIRECTORY_CREATED,
        _PublicationBoundary.SHARD_DIRECTORY_CREATED,
    ),
    ids=("root-mkdir", "sha256-mkdir", "shard-mkdir"),
)
def test_restart_repairs_mkdir_durability_before_artifact_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: _PublicationBoundary,
) -> None:
    root = tmp_path / boundary.value / "artifacts"
    digest = sha256_bytes(_DATA)
    sha256_directory = root / "sha256"
    shard = sha256_directory / digest[:2]
    canonical = shard / digest
    _prepare_directory_creation_boundary(root, boundary)

    _kill_at_publication_boundary(root, boundary)

    assert root.is_dir()
    if boundary is _PublicationBoundary.ROOT_DIRECTORY_CREATED:
        assert not sha256_directory.exists()
    elif boundary is _PublicationBoundary.SHA256_DIRECTORY_CREATED:
        assert sha256_directory.is_dir()
        assert not shard.exists()
    else:
        assert shard.is_dir()
    assert not canonical.exists()
    assert _audit_finding_kinds(root) == ()

    synced: list[Path] = []
    real_sync = FileArtifactStore._fsync_directory
    real_rename = artifact_module._rename_noreplace

    def record_sync(directory: Path) -> None:
        real_sync(directory)
        synced.append(directory)

    def require_repaired_directory_entries(source: Path, target: Path) -> bool:
        assert synced == [root.parent, root, sha256_directory]
        return real_rename(source, target)

    monkeypatch.setattr(FileArtifactStore, "_fsync_directory", staticmethod(record_sync))
    monkeypatch.setattr(
        artifact_module,
        "_rename_noreplace",
        require_repaired_directory_entries,
    )

    retry_store = FileArtifactStore(root, trusted_base=root.parent)
    reference = retry_store.put_bytes(_DATA, media_type="application/octet-stream")

    assert synced == [root.parent, root, sha256_directory, shard]
    assert retry_store.get_bytes(reference) == _DATA
    assert os.lstat(canonical).st_nlink == 1
    assert _audit_finding_kinds(root) == (ArtifactFindingKind.VALID_FINALIZED_ORPHAN,)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or (sys.platform != "darwin" and not sys.platform.startswith("linux")),
    reason="requires POSIX SIGKILL and a supported exclusive-rename host",
)
@pytest.mark.parametrize(
    ("boundary", "expected_before_retry"),
    (
        (
            _PublicationBoundary.PRE_PUBLICATION,
            (ArtifactFindingKind.TEMP_ORPHAN,),
        ),
        (
            _PublicationBoundary.POST_PUBLICATION,
            (ArtifactFindingKind.VALID_FINALIZED_ORPHAN,),
        ),
        (
            _PublicationBoundary.PRE_DIRECTORY_FSYNC,
            (ArtifactFindingKind.VALID_FINALIZED_ORPHAN,),
        ),
    ),
    ids=("pre-publication", "post-publication", "pre-directory-fsync"),
)
def test_artifact_publication_recovers_from_real_process_kill(
    tmp_path: Path,
    boundary: _PublicationBoundary,
    expected_before_retry: tuple[ArtifactFindingKind, ...],
) -> None:
    root = tmp_path / boundary.value / "artifacts"
    digest = sha256_bytes(_DATA)
    preparer = FileArtifactStore(root, trusted_base=root.parent)
    target_parent = preparer._artifact_parent(digest, create=True)
    canonical = target_parent / digest

    _kill_at_publication_boundary(root, boundary)

    temporaries = tuple(target_parent.glob(".artifact-*"))
    if boundary is _PublicationBoundary.PRE_PUBLICATION:
        assert not canonical.exists()
        assert len(temporaries) == 1
        assert temporaries[0].read_bytes() == _DATA
        identity_before_retry: tuple[int, int] | None = None
    else:
        assert canonical.read_bytes() == _DATA
        assert os.lstat(canonical).st_nlink == 1
        assert temporaries == ()
        metadata = os.lstat(canonical)
        identity_before_retry = (metadata.st_dev, metadata.st_ino)
    assert _audit_finding_kinds(root) == expected_before_retry

    retry_store = FileArtifactStore(root, trusted_base=root.parent)
    first_retry = retry_store.put_bytes(_DATA, media_type="application/octet-stream")
    retry_identity = (os.lstat(canonical).st_dev, os.lstat(canonical).st_ino)
    second_retry = retry_store.put_bytes(_DATA, media_type="application/octet-stream")

    assert first_retry == second_retry
    assert first_retry.sha256 == digest
    assert retry_store.get_bytes(second_retry) == _DATA
    assert (os.lstat(canonical).st_dev, os.lstat(canonical).st_ino) == retry_identity
    if identity_before_retry is not None:
        assert retry_identity == identity_before_retry

    expected_after_retry = (
        (
            ArtifactFindingKind.TEMP_ORPHAN,
            ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
        )
        if boundary is _PublicationBoundary.PRE_PUBLICATION
        else (ArtifactFindingKind.VALID_FINALIZED_ORPHAN,)
    )
    assert set(_audit_finding_kinds(root)) == set(expected_after_retry)
