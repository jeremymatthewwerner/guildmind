"""Cooperative state-wide mutation and maintenance leases.

Supported high-level publishers and mutators hold a shared lease while an exclusive
maintenance operation holds the same persistent lock exclusively.  The protocol is
cooperative: direct :class:`EventStore` and :class:`FileArtifactStore` callers remain
trusted low-level boundaries, and a hostile same-UID process can still ignore or
replace paths outside this protocol.

The lock file is persistent so every process coordinates on one inode.  Opening and
creation are descriptor-relative and no-follow; the state directory and lock inode
are revalidated before a lease is returned.  A future quarantine implementation owns
``quarantine/v1/ACTIVE``.  A valid present marker blocks shared mutation, while an
invalid marker namespace is an integrity denial.  This module never creates that
namespace.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import threading
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Self

MAINTENANCE_LOCK_FILENAME = ".guildmind-maintenance.lock"
QUARANTINE_ACTIVE_RELATIVE_PATH = Path("quarantine/v1/ACTIVE")

_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_LOCK_OPEN_FLAGS = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_MARKER_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class MaintenanceLeaseMode(StrEnum):
    """The two cooperative state-wide lease modes."""

    SHARED = "shared"
    EXCLUSIVE = "exclusive"


class MaintenanceBusyReason(StrEnum):
    """Stable reasons why a nonblocking lease was not acquired."""

    LEASE_HELD = "lease_held"
    QUARANTINE_ACTIVE = "quarantine_active"


class MaintenanceIntegrityReason(StrEnum):
    """Stable fail-closed storage-shape and identity failures."""

    STATE_INVALID = "state_invalid"
    STATE_CHANGED = "state_changed"
    LOCK_INVALID = "lock_invalid"
    LOCK_CHANGED = "lock_changed"
    QUARANTINE_FENCE_INVALID = "quarantine_fence_invalid"
    LOCK_OPERATION_FAILED = "lock_operation_failed"
    PROCESS_CHANGED = "process_changed"
    LEASE_ALREADY_ENTERED = "lease_already_entered"
    LEASE_RELEASED = "lease_released"


class MaintenanceBusyError(RuntimeError):
    """Raised when a valid state cannot grant a nonblocking requested lease."""

    def __init__(
        self,
        reason: MaintenanceBusyReason,
        *,
        mode: MaintenanceLeaseMode,
        state_directory: Path,
    ) -> None:
        self.reason = reason
        self.mode = mode
        self.state_directory = state_directory
        super().__init__(
            f"{mode.value} maintenance lease busy for {state_directory}: {reason.value}"
        )


class MaintenanceIntegrityError(RuntimeError):
    """Raised when lease storage cannot be trusted without following or repairing it."""

    def __init__(
        self,
        reason: MaintenanceIntegrityReason,
        *,
        state_directory: Path,
        detail: str,
    ) -> None:
        self.reason = reason
        self.state_directory = state_directory
        self.detail = detail
        super().__init__(f"maintenance lease integrity failure for {state_directory}: {detail}")


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    file_type: int
    device: int
    inode: int
    link_count: int


@dataclass(slots=True)
class _HeldLease:
    owner_pid: int
    state_directory: Path
    state_descriptor: int
    state_identity: _FileIdentity
    lock_descriptor: int
    lock_identity: _FileIdentity
    mode: MaintenanceLeaseMode
    references: int = 1


_registry_lock = threading.RLock()
_registry_pid = os.getpid()
_registry: dict[Path, _HeldLease] = {}


class MaintenanceLease:
    """One acquired cooperative lease; use it as a context manager."""

    def __init__(self, held: _HeldLease) -> None:
        self._held = held
        # Ownership is activated only after the process registry/refcount transition
        # succeeds. A constructor interrupted after initialization must not release an
        # existing caller's shared reference from ``__del__``.
        self._released = True
        self._entered = False

    @classmethod
    def acquire_shared(cls, state_directory: Path) -> Self:
        """Acquire a nonblocking shared publisher/mutator lease."""

        return cls._acquire(state_directory, mode=MaintenanceLeaseMode.SHARED)

    @classmethod
    def acquire_exclusive(cls, state_directory: Path) -> Self:
        """Acquire a nonblocking exclusive maintenance lease."""

        return cls._acquire(state_directory, mode=MaintenanceLeaseMode.EXCLUSIVE)

    @classmethod
    def _acquire(cls, configured_state: Path, *, mode: MaintenanceLeaseMode) -> Self:
        _reset_registry_after_fork()
        with _registry_lock:
            _reset_registry_after_fork()
            state, state_descriptor, state_identity = _open_state_directory(configured_state)
            existing = _registry.get(state)
            if existing is not None:
                try:
                    _verify_held_lease(existing)
                    marker_present = _quarantine_marker_present(existing)
                except OSError as error:
                    raise _integrity_error(
                        MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                        state,
                        "nested maintenance lease validation failed",
                        error,
                    ) from error
                finally:
                    os.close(state_descriptor)
                if existing.mode is mode is MaintenanceLeaseMode.SHARED:
                    if marker_present:
                        raise MaintenanceBusyError(
                            MaintenanceBusyReason.QUARANTINE_ACTIVE,
                            mode=mode,
                            state_directory=state,
                        )
                    previous_references = existing.references
                    nested_lease: MaintenanceLease | None = None
                    try:
                        nested_lease = cls(existing)
                        existing.references = previous_references + 1
                        nested_lease._released = False
                        return nested_lease
                    except BaseException:
                        existing.references = previous_references
                        if nested_lease is not None:
                            nested_lease._released = True
                        raise
                raise MaintenanceBusyError(
                    MaintenanceBusyReason.LEASE_HELD,
                    mode=mode,
                    state_directory=state,
                )

            lock_descriptor = -1
            locked = False
            held: _HeldLease | None = None
            fresh_lease: MaintenanceLease | None = None
            try:
                lock_descriptor, lock_identity = _open_or_create_lock(
                    state,
                    state_descriptor,
                )
                operation = fcntl.LOCK_SH if mode is MaintenanceLeaseMode.SHARED else fcntl.LOCK_EX
                try:
                    fcntl.flock(lock_descriptor, operation | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN}:
                        raise MaintenanceBusyError(
                            MaintenanceBusyReason.LEASE_HELD,
                            mode=mode,
                            state_directory=state,
                        ) from error
                    raise _integrity_error(
                        MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                        state,
                        "kernel lease operation failed",
                        error,
                    ) from error
                locked = True
                held = _HeldLease(
                    owner_pid=os.getpid(),
                    state_directory=state,
                    state_descriptor=state_descriptor,
                    state_identity=state_identity,
                    lock_descriptor=lock_descriptor,
                    lock_identity=lock_identity,
                    mode=mode,
                )
                _verify_held_lease(held)
                marker_present = _quarantine_marker_present(held)
                if mode is MaintenanceLeaseMode.SHARED and marker_present:
                    raise MaintenanceBusyError(
                        MaintenanceBusyReason.QUARANTINE_ACTIVE,
                        mode=mode,
                        state_directory=state,
                    )
                fresh_lease = cls(held)
                _registry[state] = held
                fresh_lease._released = False
                return fresh_lease
            except BaseException as error:
                if fresh_lease is not None:
                    fresh_lease._released = True
                if held is not None and _registry.get(state) is held:
                    del _registry[state]
                if locked:
                    _unlock_without_raising(lock_descriptor)
                close_error = _close_descriptors(
                    *((lock_descriptor,) if lock_descriptor >= 0 else ()),
                    state_descriptor,
                )
                if close_error is not None:
                    close_failure = _integrity_error(
                        MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                        state,
                        "maintenance lease descriptors could not be closed after acquisition",
                        close_error,
                    )
                    close_failure.add_note(f"acquisition also failed: {error!r}")
                    raise close_failure from close_error
                if isinstance(error, OSError):
                    raise _integrity_error(
                        MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                        state,
                        "maintenance lease acquisition failed",
                        error,
                    ) from error
                raise

    def __enter__(self) -> Self:
        _reset_registry_after_fork()
        with _registry_lock:
            _reset_registry_after_fork()
            held = self._held
            if self._released:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.LEASE_RELEASED,
                    state_directory=held.state_directory,
                    detail="a released maintenance lease cannot be re-entered",
                )
            if held.owner_pid != os.getpid():
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.PROCESS_CHANGED,
                    state_directory=held.state_directory,
                    detail="a lease acquired by another process cannot be entered",
                )
            if _registry.get(held.state_directory) is not held:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.STATE_CHANGED,
                    state_directory=held.state_directory,
                    detail="the process-local lease registration changed before entry",
                )
            if self._entered:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.LEASE_ALREADY_ENTERED,
                    state_directory=held.state_directory,
                    detail="one lease object cannot be entered more than once",
                )
            try:
                _verify_held_lease(held)
                marker_present = _quarantine_marker_present(held)
            except OSError as error:
                entry_error = _integrity_error(
                    MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                    held.state_directory,
                    "maintenance lease validation failed before entry",
                    error,
                )
                self._release_after_failed_entry(entry_error)
                raise entry_error from error
            except BaseException as error:
                self._release_after_failed_entry(error)
                raise
            if held.mode is MaintenanceLeaseMode.SHARED and marker_present:
                entry_error = MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.STATE_CHANGED,
                    state_directory=held.state_directory,
                    detail="quarantine became active before shared lease entry",
                )
                self._release_after_failed_entry(entry_error)
                raise entry_error
            self._entered = True
        return self

    def _release_after_failed_entry(self, entry_error: BaseException) -> None:
        try:
            self.close()
        except BaseException as close_error:
            entry_error.add_note(
                f"maintenance lease release after failed entry also failed: {close_error!r}"
            )

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, traceback
        try:
            self.close()
        except BaseException as close_error:
            if exception is None:
                raise
            exception.add_note(f"maintenance lease release also failed: {close_error!r}")

    def __del__(self) -> None:
        if getattr(self, "_released", True):
            return
        with suppress(BaseException):
            self.close()

    def close(self) -> None:
        """Release this reference; the final shared reference releases the kernel lock."""

        if self._released:
            return
        _reset_registry_after_fork()
        with _registry_lock:
            _reset_registry_after_fork()
            held = self._held
            self._released = True
            if held.owner_pid != os.getpid():
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.PROCESS_CHANGED,
                    state_directory=held.state_directory,
                    detail="a lease cannot be released by a forked child",
                )
            registered = _registry.get(held.state_directory)
            if registered is not held:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.STATE_CHANGED,
                    state_directory=held.state_directory,
                    detail="the process-local lease registration changed",
                )

            release_error: BaseException | None = None
            try:
                _verify_held_lease(held)
                if held.mode is MaintenanceLeaseMode.SHARED and _quarantine_marker_present(held):
                    raise MaintenanceIntegrityError(
                        MaintenanceIntegrityReason.STATE_CHANGED,
                        state_directory=held.state_directory,
                        detail="quarantine became active while a shared lease was held",
                    )
            except BaseException as error:
                release_error = (
                    _integrity_error(
                        MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                        held.state_directory,
                        "maintenance lease validation failed during release",
                        error,
                    )
                    if isinstance(error, OSError)
                    else error
                )

            held.references -= 1
            if held.references == 0:
                del _registry[held.state_directory]
                try:
                    fcntl.flock(held.lock_descriptor, fcntl.LOCK_UN)
                except OSError as error:
                    if release_error is None:
                        release_error = _integrity_error(
                            MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                            held.state_directory,
                            "kernel lease release failed",
                            error,
                        )
                finally:
                    close_error = _close_descriptors(
                        held.lock_descriptor,
                        held.state_descriptor,
                    )
                    if close_error is not None and release_error is None:
                        release_error = _integrity_error(
                            MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                            held.state_directory,
                            "maintenance lease descriptors could not be closed",
                            close_error,
                        )

            if release_error is not None:
                raise release_error


def _open_state_directory(configured_state: Path) -> tuple[Path, int, _FileIdentity]:
    lexical = Path(os.path.abspath(configured_state))
    if lexical == Path(lexical.anchor):
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.STATE_INVALID,
            state_directory=lexical,
            detail="state directory cannot be a filesystem root",
        )
    try:
        parent = lexical.parent.resolve(strict=True)
    except OSError as error:
        raise _integrity_error(
            MaintenanceIntegrityReason.STATE_INVALID,
            lexical,
            "state directory parent is unavailable",
            error,
        ) from error
    state = parent / lexical.name
    try:
        path_metadata = os.lstat(state)
    except OSError as error:
        raise _integrity_error(
            MaintenanceIntegrityReason.STATE_INVALID,
            state,
            "state directory is unavailable",
            error,
        ) from error
    path_identity = _identity(path_metadata)
    if not stat.S_ISDIR(path_metadata.st_mode):
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.STATE_INVALID,
            state_directory=state,
            detail="state directory is not a real directory",
        )
    try:
        descriptor = os.open(state, _DIRECTORY_FLAGS)
    except OSError as error:
        raise _integrity_error(
            MaintenanceIntegrityReason.STATE_INVALID,
            state,
            "state directory could not be opened without following links",
            error,
        ) from error
    try:
        try:
            descriptor_identity = _identity(os.fstat(descriptor))
        except OSError as error:
            raise _integrity_error(
                MaintenanceIntegrityReason.STATE_CHANGED,
                state,
                "open state directory could not be inspected",
                error,
            ) from error
        if not _same_object(descriptor_identity, path_identity):
            raise MaintenanceIntegrityError(
                MaintenanceIntegrityReason.STATE_CHANGED,
                state_directory=state,
                detail="state directory changed while it was opened",
            )
        _require_state_path_identity(state, path_identity)
    except BaseException:
        os.close(descriptor)
        raise
    return state, descriptor, path_identity


def _open_or_create_lock(state: Path, state_descriptor: int) -> tuple[int, _FileIdentity]:
    while True:
        observed_identity: _FileIdentity | None = None
        try:
            metadata = os.stat(
                MAINTENANCE_LOCK_FILENAME,
                dir_fd=state_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    MAINTENANCE_LOCK_FILENAME,
                    _LOCK_OPEN_FLAGS | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=state_descriptor,
                )
            except FileExistsError:
                continue
            except OSError as error:
                raise _integrity_error(
                    MaintenanceIntegrityReason.LOCK_OPERATION_FAILED,
                    state,
                    "maintenance lock file could not be created exclusively",
                    error,
                ) from error
        except OSError as error:
            raise _integrity_error(
                MaintenanceIntegrityReason.LOCK_INVALID,
                state,
                "maintenance lock path could not be inspected without following links",
                error,
            ) from error
        else:
            identity = _identity(metadata)
            _require_regular_single_link(identity, state, "maintenance lock")
            observed_identity = identity
            try:
                descriptor = os.open(
                    MAINTENANCE_LOCK_FILENAME,
                    _LOCK_OPEN_FLAGS,
                    dir_fd=state_descriptor,
                )
            except OSError as error:
                raise _integrity_error(
                    MaintenanceIntegrityReason.LOCK_INVALID,
                    state,
                    "maintenance lock file could not be opened without following links",
                    error,
                ) from error

        try:
            descriptor_identity = _identity(os.fstat(descriptor))
            _require_regular_single_link(descriptor_identity, state, "maintenance lock")
            if observed_identity is not None and descriptor_identity != observed_identity:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.LOCK_CHANGED,
                    state_directory=state,
                    detail="maintenance lock changed while it was opened",
                )
            _require_lock_path_identity(state, state_descriptor, descriptor_identity)
            os.fsync(descriptor)
            os.fsync(state_descriptor)
            final_identity = _identity(os.fstat(descriptor))
            if final_identity != descriptor_identity:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.LOCK_CHANGED,
                    state_directory=state,
                    detail="maintenance lock inode changed while it was opened",
                )
            _require_lock_path_identity(state, state_descriptor, descriptor_identity)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, descriptor_identity


def _verify_held_lease(held: _HeldLease) -> None:
    if held.owner_pid != os.getpid():
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.PROCESS_CHANGED,
            state_directory=held.state_directory,
            detail="lease ownership does not survive fork as a child-owned lease",
        )
    state_identity = _identity(os.fstat(held.state_descriptor))
    if not _same_object(state_identity, held.state_identity):
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.STATE_CHANGED,
            state_directory=held.state_directory,
            detail="open state directory identity changed",
        )
    _require_state_path_identity(held.state_directory, held.state_identity)
    lock_identity = _identity(os.fstat(held.lock_descriptor))
    if lock_identity != held.lock_identity:
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.LOCK_CHANGED,
            state_directory=held.state_directory,
            detail="open maintenance lock identity changed",
        )
    _require_lock_path_identity(
        held.state_directory,
        held.state_descriptor,
        held.lock_identity,
    )


def _quarantine_marker_present(held: _HeldLease) -> bool:
    current_descriptor = os.dup(held.state_descriptor)
    try:
        for component in QUARANTINE_ACTIVE_RELATIVE_PATH.parts[:-1]:
            try:
                path_metadata = os.stat(
                    component,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            except OSError as error:
                raise _integrity_error(
                    MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                    held.state_directory,
                    "quarantine fence namespace could not be inspected",
                    error,
                ) from error
            path_identity = _identity(path_metadata)
            if not stat.S_ISDIR(path_metadata.st_mode):
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                    state_directory=held.state_directory,
                    detail="quarantine fence ancestor is not a real directory",
                )
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_descriptor)
            except OSError as error:
                raise _integrity_error(
                    MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                    held.state_directory,
                    "quarantine fence ancestor could not be opened without following links",
                    error,
                ) from error
            try:
                if not _same_object(_identity(os.fstat(next_descriptor)), path_identity):
                    raise MaintenanceIntegrityError(
                        MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                        state_directory=held.state_directory,
                        detail="quarantine fence ancestor changed while it was opened",
                    )
                final_path_identity = _identity(
                    os.stat(component, dir_fd=current_descriptor, follow_symlinks=False)
                )
                if not _same_object(final_path_identity, path_identity):
                    raise MaintenanceIntegrityError(
                        MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                        state_directory=held.state_directory,
                        detail="quarantine fence ancestor changed during inspection",
                    )
            except BaseException:
                os.close(next_descriptor)
                raise
            os.close(current_descriptor)
            current_descriptor = next_descriptor

        marker_name = QUARANTINE_ACTIVE_RELATIVE_PATH.name
        try:
            marker_metadata = os.stat(
                marker_name,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        except OSError as error:
            raise _integrity_error(
                MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                held.state_directory,
                "quarantine ACTIVE marker could not be inspected",
                error,
            ) from error
        marker_identity = _identity(marker_metadata)
        _require_regular_single_link(
            marker_identity,
            held.state_directory,
            "quarantine ACTIVE marker",
            reason=MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
        )
        try:
            marker_descriptor = os.open(
                marker_name,
                _MARKER_OPEN_FLAGS,
                dir_fd=current_descriptor,
            )
        except OSError as error:
            raise _integrity_error(
                MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                held.state_directory,
                "quarantine ACTIVE marker could not be opened without following links",
                error,
            ) from error
        try:
            if _identity(os.fstat(marker_descriptor)) != marker_identity:
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                    state_directory=held.state_directory,
                    detail="quarantine ACTIVE marker changed while it was opened",
                )
            if (
                _identity(os.stat(marker_name, dir_fd=current_descriptor, follow_symlinks=False))
                != marker_identity
            ):
                raise MaintenanceIntegrityError(
                    MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID,
                    state_directory=held.state_directory,
                    detail="quarantine ACTIVE marker changed during inspection",
                )
        finally:
            os.close(marker_descriptor)
        return True
    finally:
        os.close(current_descriptor)


def _require_state_path_identity(state: Path, expected: _FileIdentity) -> None:
    try:
        observed = _identity(os.lstat(state))
    except OSError as error:
        raise _integrity_error(
            MaintenanceIntegrityReason.STATE_CHANGED,
            state,
            "state directory path changed",
            error,
        ) from error
    if not _same_object(observed, expected):
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.STATE_CHANGED,
            state_directory=state,
            detail="state directory path no longer names the opened directory",
        )


def _require_lock_path_identity(
    state: Path,
    state_descriptor: int,
    expected: _FileIdentity,
) -> None:
    try:
        observed = _identity(
            os.stat(
                MAINTENANCE_LOCK_FILENAME,
                dir_fd=state_descriptor,
                follow_symlinks=False,
            )
        )
    except OSError as error:
        raise _integrity_error(
            MaintenanceIntegrityReason.LOCK_CHANGED,
            state,
            "maintenance lock path changed",
            error,
        ) from error
    if observed != expected:
        raise MaintenanceIntegrityError(
            MaintenanceIntegrityReason.LOCK_CHANGED,
            state_directory=state,
            detail="maintenance lock path no longer names the locked inode",
        )


def _require_regular_single_link(
    identity: _FileIdentity,
    state: Path,
    label: str,
    *,
    reason: MaintenanceIntegrityReason = MaintenanceIntegrityReason.LOCK_INVALID,
) -> None:
    if identity.file_type != stat.S_IFREG or identity.link_count != 1:
        raise MaintenanceIntegrityError(
            reason,
            state_directory=state,
            detail=f"{label} must be a single-link regular file",
        )


def _identity(metadata: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        file_type=stat.S_IFMT(metadata.st_mode),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        link_count=metadata.st_nlink,
    )


def _same_object(first: _FileIdentity, second: _FileIdentity) -> bool:
    return (
        first.file_type == second.file_type
        and first.device == second.device
        and first.inode == second.inode
    )


def _integrity_error(
    reason: MaintenanceIntegrityReason,
    state: Path,
    detail: str,
    error: OSError,
) -> MaintenanceIntegrityError:
    suffix = f" ({error.strerror})" if error.strerror else ""
    return MaintenanceIntegrityError(
        reason,
        state_directory=state,
        detail=f"{detail}{suffix}",
    )


def _unlock_without_raising(descriptor: int) -> None:
    with suppress(OSError):
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _close_descriptors(*descriptors: int) -> OSError | None:
    first_error: OSError | None = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as error:
            if first_error is None:
                first_error = error
    return first_error


def _reset_registry_after_fork() -> None:
    current_pid = os.getpid()
    if current_pid == _registry_pid:
        return
    _reset_registry_in_child()


def _reset_registry_in_child() -> None:
    global _registry, _registry_lock, _registry_pid
    inherited = tuple(_registry.values())
    # A lock owned by another parent thread remains permanently owned in the child.
    # Replace it without acquiring it, then close the child's copies of lease FDs so
    # they cannot prolong the parent's kernel lease after an abrupt parent exit.
    _registry_lock = threading.RLock()
    _registry = {}
    _registry_pid = os.getpid()
    for held in inherited:
        # Do not issue LOCK_UN: the inherited open-file description is also owned by
        # the parent. Closing only the child's copies leaves the parent's lease intact.
        with suppress(OSError):
            os.close(held.lock_descriptor)
        with suppress(OSError):
            os.close(held.state_descriptor)


def _prepare_registry_for_fork() -> None:
    _registry_lock.acquire()


def _release_registry_after_fork() -> None:
    _registry_lock.release()


os.register_at_fork(
    before=_prepare_registry_for_fork,
    after_in_parent=_release_registry_after_fork,
    after_in_child=_reset_registry_in_child,
)
