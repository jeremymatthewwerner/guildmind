import errno
import os
import stat
import sys
from pathlib import Path

import pytest

import guildmind.storage.artifacts as artifacts_module
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


def test_artifact_store_repairs_existing_directory_durability_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"inherited directory entries"
    digest = sha256_bytes(data)
    root = tmp_path / "artifacts"
    sha256_directory = root / "sha256"
    shard = sha256_directory / digest[:2]
    shard.mkdir(parents=True)
    synced: list[Path] = []
    real_sync = FileArtifactStore._fsync_directory
    real_rename = artifacts_module._rename_noreplace

    def record_sync(directory: Path) -> None:
        real_sync(directory)
        synced.append(directory)

    def require_durable_parents(source: Path, target: Path) -> bool:
        assert synced == [tmp_path, root, sha256_directory]
        return real_rename(source, target)

    monkeypatch.setattr(FileArtifactStore, "_fsync_directory", staticmethod(record_sync))
    monkeypatch.setattr(artifacts_module, "_rename_noreplace", require_durable_parents)

    store = FileArtifactStore(root)
    reference = store.put_bytes(data, media_type="application/octet-stream")

    assert synced == [tmp_path, root, sha256_directory, shard]
    assert store.get_bytes(reference) == data


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
    preserved = list(canonical.parent.glob(".artifact-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == data
    assert os.lstat(preserved[0]).st_nlink == 1
    store._verify_path(preserved[0], digest=sha256_bytes(data), size_bytes=len(data))


def test_artifact_store_deduplicates_after_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    real_rename = artifacts_module._rename_noreplace
    race_was_injected = False

    def competing_rename(source: Path, target: Path) -> bool:
        nonlocal race_was_injected
        race_was_injected = True
        competitor = target.parent / ".competing-publisher"
        competitor.write_bytes(source.read_bytes())
        assert real_rename(competitor, target)
        return real_rename(source, target)

    monkeypatch.setattr(artifacts_module, "_rename_noreplace", competing_rename)

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


def test_exact_name_check_rejects_a_resolved_spelling_alias_on_every_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "canonical"
    path.write_bytes(b"bytes")

    def report_only_alias(directory: Path, name: str) -> bool:
        assert directory == tmp_path
        assert name == "canonical"
        return False

    monkeypatch.setattr(artifacts_module, "_entry_name_exists_exact", report_only_alias)

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        FileArtifactStore._require_exact_entry_name(path)


def test_artifact_store_rejects_case_alias_for_controlled_root_on_case_insensitive_fs(
    tmp_path: Path,
) -> None:
    alias = tmp_path / "ARTIFACTS"
    alias.mkdir()
    canonical = tmp_path / "artifacts"
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        FileArtifactStore(canonical)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["ARTIFACTS"]


def test_artifact_store_rejects_case_alias_for_sha256_directory_on_case_insensitive_fs(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    alias = store.root / "SHA256"
    alias.mkdir()
    canonical = store.root / "sha256"
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        store.put_bytes(b"trusted", media_type="application/octet-stream")

    assert sorted(path.name for path in store.root.iterdir()) == ["SHA256"]


def test_artifact_store_rejects_case_alias_for_shard_directory_on_case_insensitive_fs(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    digest = sha256_bytes(data)
    assert digest[:2].upper() != digest[:2]
    sha256_directory = store.root / "sha256"
    sha256_directory.mkdir()
    alias = sha256_directory / digest[:2].upper()
    alias.mkdir()
    canonical = sha256_directory / digest[:2]
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert sorted(path.name for path in sha256_directory.iterdir()) == [digest[:2].upper()]


def test_artifact_store_rejects_case_alias_for_digest_and_preserves_temp_on_case_insensitive_fs(
    tmp_path: Path,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"trusted"
    digest = sha256_bytes(data)
    shard = store._artifact_parent(digest, create=True)
    alias = shard / digest.upper()
    alias.write_bytes(data)
    canonical = shard / digest
    if not canonical.exists():
        pytest.skip("filesystem is case-sensitive")

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert alias.read_bytes() == data
    preserved = tuple(shard.glob(".artifact-*"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == data


def test_artifact_store_checks_temporary_entry_exact_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    real_entry_name_exists_exact = artifacts_module._entry_name_exists_exact

    def hide_exact_temporary(directory: Path, name: str) -> bool:
        if name.startswith(".artifact-"):
            return False
        return real_entry_name_exists_exact(directory, name)

    monkeypatch.setattr(
        artifacts_module,
        "_entry_name_exists_exact",
        hide_exact_temporary,
    )

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        store.put_bytes(b"temporary spelling", media_type="application/octet-stream")


def test_artifact_store_checks_canonical_entry_exact_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("canonical spelling")
    real_entry_name_exists_exact = artifacts_module._entry_name_exists_exact

    def hide_exact_digest(directory: Path, name: str) -> bool:
        if name == reference.sha256:
            return False
        return real_entry_name_exists_exact(directory, name)

    monkeypatch.setattr(
        artifacts_module,
        "_entry_name_exists_exact",
        hide_exact_digest,
    )

    with pytest.raises(ArtifactCorruptionError, match="exact on-disk name"):
        store.verify(reference)


def test_read_only_artifact_store_requires_existing_root_without_creating_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state" / "artifacts"

    with pytest.raises(ArtifactCorruptionError, match="could not be inspected"):
        FileArtifactStore.open_existing_read_only(root, trusted_base=tmp_path)

    assert not (tmp_path / "state").exists()
    assert not root.exists()


def test_read_only_artifact_store_reads_but_rejects_publication(
    tmp_path: Path,
) -> None:
    writer = FileArtifactStore(tmp_path / "state" / "artifacts", trusted_base=tmp_path)
    reference = writer.put_text("immutable evidence")
    before = sorted(
        (path.relative_to(tmp_path), path.stat().st_ino) for path in tmp_path.rglob("*")
    )

    reader = FileArtifactStore.open_existing_read_only(
        writer.root,
        trusted_base=tmp_path,
    )

    assert reader.get_bytes(reference) == b"immutable evidence"
    reader.verify(reference)
    assert reader.path_for(reference) == writer.path_for(reference)
    with pytest.raises(ArtifactCorruptionError, match="cannot publish"):
        reader.put_bytes(b"new bytes", media_type="application/octet-stream")
    with pytest.raises(ArtifactCorruptionError, match="cannot publish"):
        reader.put_text("new text")
    after = sorted((path.relative_to(tmp_path), path.stat().st_ino) for path in tmp_path.rglob("*"))
    assert after == before


def test_read_only_artifact_store_rejects_root_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"unchanged")
    root = state / "artifacts"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactCorruptionError, match="not a real directory"):
        FileArtifactStore.open_existing_read_only(root, trusted_base=tmp_path)

    assert root.is_symlink()
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in outside.iterdir()) == ["sentinel"]


def test_read_only_artifact_store_detects_replaced_captured_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state" / "artifacts"
    FileArtifactStore(root, trusted_base=tmp_path)
    reader = FileArtifactStore.open_existing_read_only(root, trusted_base=tmp_path)
    root.rename(tmp_path / "original-artifacts")
    root.mkdir()

    with pytest.raises(ArtifactCorruptionError, match="changed after store initialization"):
        reader.verify_root_identity()
    with pytest.raises(ArtifactCorruptionError, match="cannot publish"):
        reader.put_bytes(b"blocked", media_type="application/octet-stream")

    assert list(root.iterdir()) == []


def test_artifact_store_rejects_a_hard_linked_canonical_blob(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    reference = store.put_text("immutable evidence")
    canonical = store.path_for(reference)
    outside_alias = tmp_path / "outside-alias"
    os.link(canonical, outside_alias)

    with pytest.raises(ArtifactCorruptionError, match="multiple hard links"):
        store.verify(reference)
    with pytest.raises(ArtifactCorruptionError, match="multiple hard links"):
        store.get_bytes(reference)
    with pytest.raises(ArtifactCorruptionError, match="multiple hard links"):
        store.put_text("immutable evidence")

    assert canonical.stat().st_nlink == 2
    assert outside_alias.read_bytes() == b"immutable evidence"


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
    rename_was_called = False

    def corrupt_after_file_fsync(descriptor: int) -> None:
        nonlocal temporary_was_corrupted
        real_fsync(descriptor)
        if temporary_was_corrupted or not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return
        os.lseek(descriptor, 0, os.SEEK_SET)
        assert os.write(descriptor, b"altered") == len(data)
        temporary_was_corrupted = True

    def record_rename(source: Path, target: Path) -> bool:
        del source, target
        nonlocal rename_was_called
        rename_was_called = True
        return True

    monkeypatch.setattr(os, "fsync", corrupt_after_file_fsync)
    monkeypatch.setattr(artifacts_module, "_rename_noreplace", record_rename)

    with pytest.raises(ArtifactCorruptionError, match="failed verification"):
        store.put_bytes(data, media_type="application/octet-stream")

    assert temporary_was_corrupted
    assert not rename_was_called
    assert not canonical.exists()
    assert list(canonical.parent.glob(".artifact-*")) == []


@pytest.mark.skipif(
    sys.platform != "darwin" and not sys.platform.startswith("linux"),
    reason="exclusive rename implementation is limited to Darwin and Linux",
)
def test_platform_noreplace_rename_is_atomic_and_exclusive(tmp_path: Path) -> None:
    first_source = tmp_path / "first-source"
    target = tmp_path / "target"
    first_source.write_bytes(b"first")

    assert artifacts_module._rename_noreplace(first_source, target)
    assert not first_source.exists()
    assert target.read_bytes() == b"first"
    assert os.lstat(target).st_nlink == 1

    second_source = tmp_path / "second-source"
    second_source.write_bytes(b"second")
    assert not artifacts_module._rename_noreplace(second_source, target)
    assert second_source.read_bytes() == b"second"
    assert target.read_bytes() == b"first"
    assert os.lstat(target).st_nlink == 1


def test_artifact_publication_never_calls_hard_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")

    def reject_hard_link(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("CAS publication attempted to create a hard link")

    monkeypatch.setattr(os, "link", reject_hard_link)

    reference = store.put_text("single-link publication")
    canonical = store.path_for(reference)
    assert store.get_bytes(reference) == b"single-link publication"
    assert os.lstat(canonical).st_nlink == 1
    assert list(canonical.parent.glob(".artifact-*")) == []


def test_syscall_eexist_maps_to_verified_dedupe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    first = store.put_text("existing winner")
    canonical = store.path_for(first)
    original_identity = (os.lstat(canonical).st_dev, os.lstat(canonical).st_ino)

    def target_exists(source: Path, target: Path) -> None:
        del source, target
        raise FileExistsError(errno.EEXIST, "injected publication race")

    monkeypatch.setattr(artifacts_module, "_invoke_noreplace_rename", target_exists)

    second = store.put_text("existing winner")
    assert second == first
    assert (os.lstat(canonical).st_dev, os.lstat(canonical).st_ino) == original_identity
    assert store.get_bytes(second) == b"existing winner"
    assert list(canonical.parent.glob(".artifact-*")) == []


def test_syscall_failure_fails_closed_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")
    data = b"not published"
    canonical = _canonical_path(store, data)

    def publication_denied(source: Path, target: Path) -> None:
        del source, target
        raise PermissionError(errno.EACCES, "injected publication denial")

    monkeypatch.setattr(artifacts_module, "_invoke_noreplace_rename", publication_denied)

    with pytest.raises(
        ArtifactCorruptionError,
        match="could not be published with atomic no-replace rename",
    ) as raised:
        store.put_bytes(data, media_type="application/octet-stream")

    assert isinstance(raised.value.__cause__, PermissionError)
    assert not canonical.exists()
    assert list(canonical.parent.glob(".artifact-*")) == []


def test_unsupported_platform_fails_closed_without_moving_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.write_bytes(b"unpublished")
    monkeypatch.setattr(sys, "platform", "unsupported-test-platform")

    with pytest.raises(
        ArtifactCorruptionError,
        match="could not be published with atomic no-replace rename",
    ) as raised:
        artifacts_module._rename_noreplace(source, target)

    assert isinstance(raised.value.__cause__, OSError)
    assert raised.value.__cause__.errno == errno.ENOTSUP
    assert source.read_bytes() == b"unpublished"
    assert not target.exists()


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
