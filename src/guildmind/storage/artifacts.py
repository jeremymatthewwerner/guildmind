"""Filesystem content-addressed storage with atomic writes and verification."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Self

from guildmind.domain import ArtifactRef, sha256_bytes


class ArtifactCorruptionError(RuntimeError):
    """Raised when bytes do not match their content-addressed identity."""


_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_SCANDIR = os.scandir


def _invoke_noreplace_rename(source: Path, target: Path) -> None:
    """Atomically rename ``source`` to absent ``target`` using the host libc.

    There is intentionally no portable fallback. In particular, publishing with a
    hard link would expose a window in which the temporary and canonical names both
    point at the blob, violating the store's single-link ownership invariant.
    """

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise OSError(errno.ENOSYS, "host libc is unavailable") from error

    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    if sys.platform == "darwin":
        try:
            rename = libc.renamex_np
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "libc renamex_np is unavailable") from error
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        arguments = (source_bytes, target_bytes, _RENAME_EXCL)
    elif sys.platform.startswith("linux"):
        try:
            rename = libc.renameat2
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "libc renameat2 is unavailable") from error
        rename.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename.restype = ctypes.c_int
        arguments = (
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            target_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            f"atomic no-replace rename is unsupported on {sys.platform}",
        )

    ctypes.set_errno(0)
    result = rename(*arguments)
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number), str(target))


def _rename_noreplace(source: Path, target: Path) -> bool:
    """Publish ``source`` at ``target`` and report whether this publisher won."""

    try:
        _invoke_noreplace_rename(source, target)
    except OSError as error:
        if error.errno == errno.EEXIST:
            return False
        raise ArtifactCorruptionError(
            f"artifact {target.name} could not be published with atomic no-replace rename"
        ) from error
    return True


def _entry_name_exists_exact(directory: Path, name: str) -> bool:
    """Return whether ``directory`` contains exactly ``name`` as reported on disk."""

    with _SCANDIR(directory) as entries:
        return any(entry.name == name for entry in entries)


class FileArtifactStore:
    """Store immutable blobs below ``sha256/<prefix>/<digest>``.

    A blob is flushed, verified, and atomically renamed into its canonical path before
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
        resolved_base = configured_base.resolve(strict=False)
        self.root = resolved_base.joinpath(relative_root)
        self._read_only = False
        self._controlled_directories = self._paths_below(resolved_base, relative_root)
        self._create_directory_chain(
            self.root,
            controlled_directories=self._controlled_directories,
        )
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
        instance._controlled_directories = instance._paths_below(resolved_base, relative_root)
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
                try:
                    os.mkdir(candidate)
                except FileExistsError:
                    pass
                except OSError as error:
                    raise ArtifactCorruptionError(
                        f"artifact directory {candidate} could not be created safely"
                    ) from error
                self._require_real_directory(candidate)
                # An existing entry may be the residue of a process that died after
                # mkdir but before syncing its parent. Repair that durability gap
                # before any temporary or canonical artifact is published below it.
                self._fsync_directory(current)
            elif not self._require_real_directory(candidate, missing_ok=True):
                return expected
            current = candidate
        return expected

    @staticmethod
    def _paths_below(base: Path, relative: Path) -> tuple[Path, ...]:
        current = base
        paths: list[Path] = []
        for component in relative.parts:
            current /= component
            paths.append(current)
        return tuple(paths)

    @staticmethod
    def _create_directory_chain(
        path: Path,
        *,
        controlled_directories: tuple[Path, ...],
    ) -> None:
        controlled = set(controlled_directories)
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
            if candidate in controlled:
                FileArtifactStore._require_exact_entry_name(candidate)
                # Sync even an inherited directory entry: its creator may have died
                # immediately after mkdir and before the parent fsync.
                FileArtifactStore._fsync_directory(current)
            elif created:
                # Preserve the existing creation behavior above the explicitly trusted
                # boundary without adding fsyncs for every preexisting host ancestor.
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
        for path in self._controlled_directories:
            self._require_exact_entry_name(path)

    @staticmethod
    def _require_exact_entry_name(path: Path, *, missing_ok: bool = False) -> bool:
        try:
            exact = _entry_name_exists_exact(path.parent, path.name)
        except FileNotFoundError as error:
            if missing_ok:
                return False
            raise ArtifactCorruptionError(f"missing artifact path {path}") from error
        except OSError as error:
            raise ArtifactCorruptionError(
                f"artifact path {path} could not be inspected for its exact name"
            ) from error
        if exact:
            return True

        # On a case-sensitive filesystem this usually means the path is absent. On a
        # case-insensitive filesystem lstat may still resolve an entry with different
        # spelling; that alias must be rejected rather than blessed as canonical.
        try:
            os.lstat(path)
        except FileNotFoundError as error:
            if missing_ok:
                return False
            raise ArtifactCorruptionError(f"missing artifact path {path}") from error
        except OSError as error:
            raise ArtifactCorruptionError(
                f"artifact path {path} could not be inspected for its exact name"
            ) from error
        raise ArtifactCorruptionError(f"artifact path {path} does not have its exact on-disk name")

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
        FileArtifactStore._require_exact_entry_name(path)
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

        self._require_exact_entry_name(path)
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
            self._require_exact_entry_name(path)

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
        preserve_temporary = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())

            self._verify_path(temporary, digest=digest, size_bytes=len(data))
            # Another publisher may win, or a canonical entry may already exist. Never
            # replace it; final verification decides whether it is a valid dedupe.
            published = _rename_noreplace(temporary, target)
            if not published:
                try:
                    self._verify_path(target, digest=digest, size_bytes=len(data))
                except ArtifactCorruptionError:
                    # Keep the already-fsynced, verified contender as recoverable
                    # evidence when the canonical target is invalid. The final parent
                    # fsync makes that temporary directory entry durable before denial.
                    preserve_temporary = True
                    raise
        finally:
            if not preserve_temporary:
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
