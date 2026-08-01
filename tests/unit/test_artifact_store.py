from pathlib import Path

import pytest

from guildmind.storage import ArtifactCorruptionError, FileArtifactStore


def test_artifact_store_deduplicates_and_verifies(tmp_path: Path) -> None:
    store = FileArtifactStore(tmp_path / "artifacts")

    first = store.put_text("evidence")
    second = store.put_text("evidence")

    assert first == second
    assert store.get_bytes(first) == b"evidence"
    assert store.path_for(first).is_file()
    store.verify(first)


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
