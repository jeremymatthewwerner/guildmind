import os
import stat
from pathlib import Path

import pytest

from guildmind.domain import sha256_bytes
from guildmind.storage import ArtifactCorruptionError, FileArtifactStore


def _canonical_path(store: FileArtifactStore, data: bytes) -> Path:
    digest = sha256_bytes(data)
    return store.root / "sha256" / digest[:2] / digest


def test_artifact_store_deduplicates_and_verifies(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")

    first = store.put_text("evidence")
    canonical = store.path_for(first)
    original_inode = os.lstat(canonical).st_ino
    second = store.put_text("evidence")

    assert first == second
    assert os.lstat(canonical).st_ino == original_inode
    assert store.get_bytes(first) == b"evidence"
    assert canonical.is_file()
    store.verify(first)


def test_artifact_store_validates_reference_before_writing(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="media_type"):
        store.put_bytes(b"would become orphaned", media_type="")

    assert list(store.root.iterdir()) == []


def test_artifact_store_syncs_each_new_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    synced: list[Path] = []
    monkeypatch.setattr(store, "_fsync_directory", synced.append)

    reference = store.put_bytes(b"durable hierarchy", media_type="application/octet-stream")

    shard = store.root / "sha256" / reference.sha256[:2]
    assert synced == [store.root, store.root / "sha256", shard]
    assert store.get_bytes(reference) == b"durable hierarchy"


def test_artifact_store_never_overwrites_mismatched_existing_target(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    canonical = _canonical_path(store, data)
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"altered")
    original_inode = os.lstat(canonical).st_ino

    with pytest.raises(ArtifactCorruptionError, match="failed verification"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert canonical.read_bytes() == b"altered"
    assert os.lstat(canonical).st_ino == original_inode
    assert list(canonical.parent.glob(".artifact-*")) == []


def test_artifact_store_deduplicates_after_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    real_link = os.link
    race_was_injected = False

    def competing_link(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        nonlocal race_was_injected
        race_was_injected = True
        real_link(source, target, follow_symlinks=follow_symlinks)
        real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", competing_link)

    reference = store.put_bytes(b"race winner", media_type="application/octet-stream")

    assert race_was_injected
    assert store.get_bytes(reference) == b"race winner"
    assert list(store.path_for(reference).parent.glob(".artifact-*")) == []


def test_artifact_store_rejects_canonical_symlink(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    outside = tmp_path / "outside-artifact"
    outside.write_bytes(data)
    canonical = _canonical_path(store, data)
    canonical.parent.mkdir(parents=True)
    canonical.symlink_to(outside)

    with pytest.raises(ArtifactCorruptionError, match="not a regular file"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert canonical.is_symlink()
    assert outside.read_bytes() == data


def test_artifact_store_rejects_sha256_directory_symlink_without_outside_mutation(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    digest = sha256_bytes(data)
    outside = tmp_path / "outside-sha256"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    (store.root / "sha256").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="not a real directory"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert sentinel.read_bytes() == b"unchanged"
    assert not (outside / digest[:2]).exists()
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_artifact_store_rejects_shard_directory_symlink_without_outside_mutation(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    digest = sha256_bytes(data)
    sha256_directory = store.root / "sha256"
    sha256_directory.mkdir()
    outside = tmp_path / "outside-shard"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    (sha256_directory / digest[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="not a real directory"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert sentinel.read_bytes() == b"unchanged"
    assert not (outside / digest).exists()
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_artifact_store_rejects_replaced_ancestor_without_outside_mutation(
    tmp_path: Path,
) -> None:
    original_parent = tmp_path / "state"
    store = FileArtifactStore(original_parent / "artifacts")
    moved_parent = tmp_path / "state-before-replacement"
    original_parent.rename(moved_parent)
    outside_parent = tmp_path / "outside"
    outside_artifacts = outside_parent / "artifacts"
    outside_artifacts.mkdir(parents=True)
    sentinel = outside_artifacts / "sentinel"
    sentinel.write_bytes(b"unchanged")
    original_parent.symlink_to(outside_parent, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="changed after store initialization"):
        store.put_bytes(b"trusted", media_type="application/octet-stream")

    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside_artifacts.iterdir()) == ["sentinel"]


def test_artifact_store_rejects_preexisting_root_symlink_without_outside_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    configured_root = tmp_path / "artifacts"
    configured_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="not a real directory"):
        FileArtifactStore(configured_root)

    assert configured_root.is_symlink()
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_artifact_store_rejects_preexisting_ancestor_symlink_without_outside_mutation(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    configured_parent = tmp_path / "state"
    configured_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="not a real directory"):
        FileArtifactStore(configured_parent / "artifacts", trusted_base=tmp_path)

    assert configured_parent.is_symlink()
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_artifact_store_accepts_a_trusted_parent_alias(tmp_path: Path) -> None:
    physical_parent = tmp_path / "physical"
    physical_parent.mkdir()
    trusted_alias = tmp_path / "trusted-alias"
    trusted_alias.symlink_to(physical_parent, target_is_directory=True)

    store = FileArtifactStore(trusted_alias / "artifacts")
    reference = store.put_text("trusted parent alias")

    assert store.root == physical_parent / "artifacts"
    assert store.get_bytes(reference) == b"trusted parent alias"


def test_artifact_store_requires_root_below_explicit_trusted_base(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="below its trusted base"):
        FileArtifactStore(tmp_path / "artifacts", trusted_base=tmp_path / "elsewhere")


def test_artifact_store_rejects_replaced_real_directory_chain(tmp_path: Path) -> None:
    original_parent = tmp_path / "state"
    store = FileArtifactStore(original_parent / "artifacts")
    original_parent.rename(tmp_path / "state-before-replacement")
    replacement = original_parent / "artifacts"
    replacement.mkdir(parents=True)

    with pytest.raises(ArtifactCorruptionError, match="changed after store initialization"):
        store.verify_root_identity()

    assert list(replacement.iterdir()) == []


def test_artifact_store_rejects_filesystem_root() -> None:
    filesystem_root = Path(Path.cwd().anchor)

    with pytest.raises(ValueError, match="filesystem root"):
        FileArtifactStore(filesystem_root)


def test_artifact_store_verifies_fsynced_temporary_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    canonical = _canonical_path(store, data)
    real_fsync = os.fsync
    temporary_was_corrupted = False
    link_was_called = False

    def corrupt_after_file_fsync(descriptor: int) -> None:
        nonlocal temporary_was_corrupted
        real_fsync(descriptor)
        if temporary_was_corrupted or not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.write(descriptor, b"altered") == len(data)
        temporary_was_corrupted = True

    def record_link(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        del source, target, follow_symlinks
        nonlocal link_was_called
        link_was_called = True

    monkeypatch.setattr(os, "fsync", corrupt_after_file_fsync)
    monkeypatch.setattr(os, "link", record_link)

    with pytest.raises(ArtifactCorruptionError, match="failed verification"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert temporary_was_corrupted
    assert not link_was_called
    assert not canonical.exists()
    assert list(canonical.parent.glob(".artifact-*")) == []


def test_artifact_store_detects_corruption(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"trusted", media_type="application/octet-stream")
    store.path_for(reference).write_bytes(b"tampered")

    with pytest.raises(ArtifactCorruptionError, match="failed verification"):
        store.get_bytes(reference)


def test_artifact_storage_ref_cannot_redirect_reads(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("trusted")
    redirected = reference.model_copy(update={"storage_ref": "../elsewhere"})

    with pytest.raises(ArtifactCorruptionError, match="does not match"):
        store.get_bytes(redirected)
