"""Filesystem content-addressed storage with atomic writes and verification."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from guildmind.domain import ArtifactRef, sha256_bytes


class ArtifactCorruptionError(RuntimeError):
    """Raised when bytes do not match their content-addressed identity."""


class FileArtifactStore:
    """Store immutable blobs below ``sha256/<prefix>/<digest>``.

    A blob is flushed and atomically renamed before its reference is returned. The
    caller can therefore commit the reference to SQLite only after the bytes exist.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        digest = sha256_bytes(data)
        relative_path = Path("sha256") / digest[:2] / digest
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            self._verify_path(target, digest=digest, size_bytes=len(data))
        else:
            self._write_atomic(target, data)

        return ArtifactRef(
            media_type=media_type,
            size_bytes=len(data),
            sha256=digest,
            storage_ref=relative_path.as_posix(),
        )

    def put_text(self, text: str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        return self.put_bytes(text.encode("utf-8"), media_type=media_type)

    def get_bytes(self, reference: ArtifactRef) -> bytes:
        path = self.path_for(reference)
        data = path.read_bytes()
        if len(data) != reference.size_bytes or sha256_bytes(data) != reference.sha256:
            raise ArtifactCorruptionError(f"artifact {reference.sha256} failed verification")
        return data

    def verify(self, reference: ArtifactRef) -> None:
        self._verify_path(
            self.path_for(reference),
            digest=reference.sha256,
            size_bytes=reference.size_bytes,
        )

    def path_for(self, reference: ArtifactRef) -> Path:
        expected = Path("sha256") / reference.sha256[:2] / reference.sha256
        if reference.storage_ref != expected.as_posix():
            raise ArtifactCorruptionError("artifact storage reference does not match its digest")
        path = (self.root / expected).resolve()
        if not path.is_relative_to(self.root):
            raise ArtifactCorruptionError("artifact reference escapes the store root")
        return path

    @staticmethod
    def _verify_path(path: Path, *, digest: str, size_bytes: int) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise ArtifactCorruptionError(f"missing artifact {digest}") from error
        if len(data) != size_bytes or sha256_bytes(data) != digest:
            raise ArtifactCorruptionError(f"artifact {digest} failed verification")

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)
