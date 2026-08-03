"""Filesystem content-addressed storage with atomic writes and verification."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Self

from guildmind.domain import ArtifactRef, sha256_bytes


class ArtifactCorruptionError(RuntimeError):
    """Raised when bytes do not match their content-addressed identity."""


class FileArtifactStore:
    """Store immutable blobs below ``sha256/<prefix>/<digest>``.

    A blob is flushed, verified, and atomically linked into its canonical path before
    its reference is returned. The caller can therefore commit the reference to SQLite
    only after the bytes exist, without any publisher replacing an existing blob.

    ``trusted_base`` defines the already-trusted path boundary. Symlinks in that base
    (including standard operating-system aliases) are resolved once; every component
    from the base to ``root`` is then created and inspected without following links.
    When omitted, the configured root's immediate parent is the trusted boundary.
    """

    def __init__(self, root: Path, *, trusted_base: Path | None = None) -> None:
        configured_root = Path(os.path.abspath(root))
        if configured_root == Path(configured_root.anchor):
            raise ValueError("artifact store root cannot be a filesystem root")
        configured_base = Path(
            os.path.abspath(trusted_base if trusted_base is not None else configured_root.parent)
        )
        try:
            relative_root = configured_root.relative_to(configured_base)
        except ValueError as error:
            raise ValueError("artifact store root must be below its trusted base") from error
        if not relative_root.parts:
            raise ValueError("artifact store root must be below its trusted base")
        self.root = configured_base.resolve(strict=False).joinpath(relative_root)
        self._read_only = False
        self._create_directory_chain(self.root)
        self._directory_identities = self._snapshot_directory_chain(self.root)

    @classmethod
    def open_existing_read_only(
        cls,
        root: Path,
        *,
        trusted_base: Path | None = None,
    ) -> Self:
        """Open an existing artifact tree without creating any directory.

        The trusted base may itself be an operating-system path alias. Every path
        component below its resolved identity, including the configured root, must
        already be a real directory. The captured no-follow identities are checked
        again before return and by every later public filesystem operation.
        """

        configured_root = Path(os.path.abspath(root))
        if configured_root == Path(configured_root.anchor):
            raise ValueError("artifact store root cannot be a filesystem root")
        configured_base = Path(
            os.path.abspath(trusted_base if trusted_base is not None else configured_root.parent)
        )
        try:
            relative_root = configured_root.relative_to(configured_base)
        except ValueError as error:
            raise ValueError("artifact store root must be below its trusted base") from error
        if not relative_root.parts:
            raise ValueError("artifact store root must be below its trusted base")
        try:
            resolved_base = configured_base.resolve(strict=True)
        except OSError as error:
            raise ArtifactCorruptionError(
                "existing artifact store trusted base is unavailable"
            ) from error

        instance = cls.__new__(cls)
        instance.root = resolved_base.joinpath(relative_root)
        instance._read_only = True
        instance._directory_identities = instance._snapshot_directory_chain(instance.root)
        instance._validate_directory_chain()
        return instance

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        if self._read_only:
            raise ArtifactCorruptionError("read-only artifact store cannot publish artifacts")
        digest = sha256_bytes(data)
        relative_path = Path("sha256") / digest[:2] / digest
        reference = ArtifactRef(
            media_type=media_type,
            size_bytes=len(data),
            sha256=digest,
            storage_ref=relative_path.as_posix(),
        )
        target = self._artifact_parent(digest, create=True) / digest

        self._write_atomic(target, data)
        return reference

    def put_text(self, text: str, *, media_type: str = "text/plain; charset=utf-8") -> ArtifactRef:
        return self.put_bytes(text.encode("utf-8"), media_type=media_type)

    def get_bytes(self, reference: ArtifactRef) -> bytes:
        return self._read_verified_path(
            self.path_for(reference),
            digest=reference.sha256,
            size_bytes=reference.size_bytes,
        )

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
        return self._artifact_parent(reference.sha256, create=False) / reference.sha256

    def verify_root_identity(self) -> None:
        """Fail if the resolved store or one of its ancestors was replaced."""

        self._validate_directory_chain()

    def _artifact_parent(self, digest: str, *, create: bool) -> Path:
        if create and self._read_only:
            raise ArtifactCorruptionError("read-only artifact store cannot create directories")
        expected = self.root / "sha256" / digest[:2]
        self._validate_directory_chain()
        current = self.root
        for component in ("sha256", digest[:2]):
            candidate = current / component
            if create:
                created = False
                try:
                    os.mkdir(candidate)
                    created = True
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ArtifactCorruptionError(
                        f"artifact directory {candidate} could not be created safely"
                    ) from error
                self._require_real_directory(candidate)
                if created:
                    self._fsync_directory(current)
            elif not self._require_real_directory(candidate, missing_ok=True):
                return expected
            current = candidate
        return expected

    @staticmethod
    def _create_directory_chain(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            candidate = current / component
            created = False
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                try:
                    os.mkdir(candidate)
                    created = True
                    metadata = os.lstat(candidate)
                except OSError as error:
                    raise ArtifactCorruptionError(
                        f"artifact directory {candidate} could not be created safely"
                    ) from error
            except OSError as error:
                raise ArtifactCorruptionError(
                    f"artifact directory {candidate} could not be inspected"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactCorruptionError(
                    f"artifact directory {candidate} is not a real directory"
                )
            if created:
                FileArtifactStore._fsync_directory(current)
            current = candidate

    @staticmethod
    def _snapshot_directory_chain(path: Path) -> tuple[tuple[Path, int, int], ...]:
        current = Path(path.anchor)
        identities: list[tuple[Path, int, int]] = []
        for component in path.parts[1:]:
            current /= component
            try:
                metadata = os.lstat(current)
            except OSError as error:
                raise ArtifactCorruptionError(
                    f"artifact directory {current} could not be inspected"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArtifactCorruptionError(
                    f"artifact directory {current} is not a real directory"
                )
            identities.append((current, metadata.st_dev, metadata.st_ino))
        return tuple(identities)

    def _validate_directory_chain(self) -> None:
        for path, expected_device, expected_inode in self._directory_identities:
            try:
                metadata = os.lstat(path)
            except OSError as error:
                raise ArtifactCorruptionError(
                    f"artifact directory {path} could not be inspected"
                ) from error
            if not stat.S_ISDIR(metadata.st_mode) or (
                metadata.st_dev,
                metadata.st_ino,
            ) != (expected_device, expected_inode):
                raise ArtifactCorruptionError(
                    f"artifact directory {path} changed after store initialization"
                )

    @staticmethod
    def _require_real_directory(path: Path, *, missing_ok: bool = False) -> bool:
        try:
            path_metadata = os.lstat(path)
        except FileNotFoundError as error:
            if missing_ok:
                return False
            raise ArtifactCorruptionError(f"missing artifact directory {path}") from error
        except OSError as error:
            raise ArtifactCorruptionError(
                f"artifact directory {path} could not be inspected"
            ) from error
        if not stat.S_ISDIR(path_metadata.st_mode):
            raise ArtifactCorruptionError(f"artifact directory {path} is not a real directory")
        return True

    def _verify_path(self, path: Path, *, digest: str, size_bytes: int) -> None:
        self._read_verified_path(path, digest=digest, size_bytes=size_bytes)

    def _read_verified_path(self, path: Path, *, digest: str, size_bytes: int) -> bytes:
        expected_parent = self._artifact_parent(digest, create=False)
        if path.parent != expected_parent:
            raise ArtifactCorruptionError(f"artifact {digest} is outside its canonical directory")

        try:
            path_metadata = os.lstat(path)
        except FileNotFoundError as error:
            raise ArtifactCorruptionError(f"missing artifact {digest}") from error
        except OSError as error:
            raise ArtifactCorruptionError(f"artifact {digest} could not be inspected") from error

        if not stat.S_ISREG(path_metadata.st_mode):
            raise ArtifactCorruptionError(f"artifact {digest} is not a regular file")
        if path_metadata.st_nlink != 1:
            raise ArtifactCorruptionError(f"artifact {digest} has multiple hard links")

        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, open_flags)
        except FileNotFoundError as error:
            raise ArtifactCorruptionError(f"missing artifact {digest}") from error
        except OSError as error:
            raise ArtifactCorruptionError(
                f"artifact {digest} could not be opened safely"
            ) from error

        chunks: list[bytes] = []
        observed_digest = hashlib.sha256()
        observed_size = 0
        try:
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or (opened_metadata.st_dev, opened_metadata.st_ino)
                != (path_metadata.st_dev, path_metadata.st_ino)
                or opened_metadata.st_size != size_bytes
                or opened_metadata.st_nlink != 1
            ):
                raise ArtifactCorruptionError(f"artifact {digest} failed verification")

            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
                observed_digest.update(chunk)
                observed_size += len(chunk)
                if observed_size > size_bytes:
                    raise ArtifactCorruptionError(f"artifact {digest} failed verification")

            final_opened_metadata = os.fstat(descriptor)
            try:
                final_path_metadata = os.lstat(path)
            except FileNotFoundError as error:
                raise ArtifactCorruptionError(f"missing artifact {digest}") from error

            opened_snapshot = (
                opened_metadata.st_dev,
                opened_metadata.st_ino,
                opened_metadata.st_size,
                opened_metadata.st_mtime_ns,
                opened_metadata.st_ctime_ns,
                opened_metadata.st_nlink,
            )
            final_opened_snapshot = (
                final_opened_metadata.st_dev,
                final_opened_metadata.st_ino,
                final_opened_metadata.st_size,
                final_opened_metadata.st_mtime_ns,
                final_opened_metadata.st_ctime_ns,
                final_opened_metadata.st_nlink,
            )
            final_path_snapshot = (
                final_path_metadata.st_dev,
                final_path_metadata.st_ino,
                final_path_metadata.st_size,
                final_path_metadata.st_mtime_ns,
                final_path_metadata.st_ctime_ns,
                final_path_metadata.st_nlink,
            )
            if (
                not stat.S_ISREG(final_path_metadata.st_mode)
                or final_path_metadata.st_nlink != 1
                or opened_snapshot != final_opened_snapshot
                or opened_snapshot != final_path_snapshot
            ):
                raise ArtifactCorruptionError(f"artifact {digest} changed during verification")
        except ArtifactCorruptionError:
            raise
        except OSError as error:
            raise ArtifactCorruptionError(f"artifact {digest} failed verification") from error
        finally:
            os.close(descriptor)

        if observed_size != size_bytes or observed_digest.hexdigest() != digest:
            raise ArtifactCorruptionError(f"artifact {digest} failed verification")
        return b"".join(chunks)

    def _write_atomic(self, target: Path, data: bytes) -> None:
        digest = sha256_bytes(data)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

            self._verify_path(temporary, digest=digest, size_bytes=len(data))
            # Another publisher may win, or a canonical entry may already exist. Never
            # replace it; final verification decides whether it is a valid dedupe.
            with suppress(FileExistsError):
                os.link(temporary, target, follow_symlinks=False)
        finally:
            temporary.unlink(missing_ok=True)
            self._fsync_directory(target.parent)

        self._verify_path(target, digest=digest, size_bytes=len(data))

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, open_flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
