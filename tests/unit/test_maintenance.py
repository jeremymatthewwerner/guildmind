from __future__ import annotations

import errno
import fcntl
import gc
import os
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import guildmind.storage.maintenance as maintenance_module
from guildmind.storage.maintenance import (
    MAINTENANCE_LOCK_FILENAME,
    QUARANTINE_ACTIVE_RELATIVE_PATH,
    MaintenanceBusyError,
    MaintenanceBusyReason,
    MaintenanceIntegrityError,
    MaintenanceIntegrityReason,
    MaintenanceLease,
)


def _reuse_descriptor_from_thread(source_descriptor: int, target_descriptor: int) -> None:
    errors: list[BaseException] = []

    def reuse() -> None:
        try:
            os.dup2(source_descriptor, target_descriptor)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=reuse, name="guildmind-descriptor-reuser")
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    if errors:
        raise errors[0]


def test_shared_lease_is_nested_and_reference_counted_in_one_process(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()

    outer = MaintenanceLease.acquire_shared(state)
    inner = MaintenanceLease.acquire_shared(state)
    inner.close()

    lock_descriptor = os.open(state / MAINTENANCE_LOCK_FILENAME, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        outer.close()
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
    finally:
        os.close(lock_descriptor)


def test_fresh_handoff_failure_rolls_back_registry_and_kernel_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    real_init = MaintenanceLease.__init__

    def fail_handoff(
        self: MaintenanceLease,
        held: maintenance_module._HeldLease,
    ) -> None:
        real_init(self, held)
        raise RuntimeError("injected lease handoff failure")

    monkeypatch.setattr(MaintenanceLease, "__init__", fail_handoff)
    with pytest.raises(RuntimeError, match="handoff"):
        MaintenanceLease.acquire_shared(state)
    monkeypatch.setattr(MaintenanceLease, "__init__", real_init)

    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_nested_handoff_failure_does_not_leave_a_phantom_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    outer = MaintenanceLease.acquire_shared(state)
    real_init = MaintenanceLease.__init__

    def fail_handoff(
        self: MaintenanceLease,
        held: maintenance_module._HeldLease,
    ) -> None:
        real_init(self, held)
        raise RuntimeError("injected nested handoff failure")

    monkeypatch.setattr(MaintenanceLease, "__init__", fail_handoff)
    with pytest.raises(RuntimeError, match="nested handoff"):
        MaintenanceLease.acquire_shared(state)
    monkeypatch.setattr(MaintenanceLease, "__init__", real_init)

    lock_descriptor = os.open(state / MAINTENANCE_LOCK_FILENAME, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lock_descriptor)
    outer.close()

    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_abandoned_lease_is_released_by_best_effort_finalizer(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    del lease
    gc.collect()

    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_released_lease_cannot_be_reentered(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    lease.close()

    with pytest.raises(MaintenanceIntegrityError) as raised, lease:
        pytest.fail("released lease body must not run")

    assert raised.value.reason is MaintenanceIntegrityReason.LEASE_RELEASED


def test_same_lease_object_cannot_be_nested_and_leave_outer_body_unlocked(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)

    with lease:
        with pytest.raises(MaintenanceIntegrityError) as raised, lease:
            pytest.fail("the same lease object must not enter its body twice")
        assert raised.value.reason is MaintenanceIntegrityReason.LEASE_ALREADY_ENTERED

        lock_descriptor = os.open(state / MAINTENANCE_LOCK_FILENAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_descriptor)

    with MaintenanceLease.acquire_exclusive(state):
        pass


@pytest.mark.parametrize("failure", ["active-fence", "lock-replacement"])
def test_named_lease_first_entry_failure_releases_kernel_lock(
    tmp_path: Path,
    failure: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    lock = state / MAINTENANCE_LOCK_FILENAME
    if failure == "active-fence":
        marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"")
        expected_reason = MaintenanceIntegrityReason.STATE_CHANGED
    else:
        lock.rename(state / "displaced-maintenance-lock")
        lock.write_bytes(b"")
        expected_reason = MaintenanceIntegrityReason.LOCK_CHANGED

    with pytest.raises(MaintenanceIntegrityError) as raised, lease:
        pytest.fail("failed first entry must not run its body")

    assert raised.value.reason is expected_reason
    assert any(
        "release after failed entry also failed" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    lease.close()
    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_descriptor_close_failure_attempts_all_cleanup_and_clears_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    real_close = os.close
    closed: list[int] = []

    def close_first_then_raise(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)
        if len(closed) == 1:
            raise OSError(5, "injected close failure")

    monkeypatch.setattr(os, "close", close_first_then_raise)
    with pytest.raises(MaintenanceIntegrityError) as raised:
        lease.close()
    monkeypatch.setattr(os, "close", real_close)

    assert raised.value.reason is MaintenanceIntegrityReason.LOCK_OPERATION_FAILED
    assert len(closed) >= 2
    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_acquisition_cleanup_attempts_all_descriptor_closes_after_handoff_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    real_init = MaintenanceLease.__init__
    real_close = os.close
    closed: list[int] = []

    def fail_handoff(
        self: MaintenanceLease,
        held: maintenance_module._HeldLease,
    ) -> None:
        real_init(self, held)
        raise RuntimeError("injected handoff failure")

    def close_first_then_raise(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)
        if len(closed) == 1:
            raise OSError(5, "injected acquisition close failure")

    monkeypatch.setattr(MaintenanceLease, "__init__", fail_handoff)
    monkeypatch.setattr(os, "close", close_first_then_raise)
    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)
    monkeypatch.setattr(MaintenanceLease, "__init__", real_init)
    monkeypatch.setattr(os, "close", real_close)

    assert raised.value.reason is MaintenanceIntegrityReason.LOCK_OPERATION_FAILED
    assert len(closed) >= 2
    with MaintenanceLease.acquire_exclusive(state):
        pass


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin case-folded alias regression")
def test_case_folded_state_alias_cannot_bypass_cross_mode_exclusion(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    alias = tmp_path / "STATE"
    if not alias.exists() or not os.path.samefile(state, alias):
        pytest.skip("test filesystem is case-sensitive")

    with MaintenanceLease.acquire_shared(state), MaintenanceLease.acquire_shared(alias):
        with pytest.raises(MaintenanceBusyError) as raised:
            MaintenanceLease.acquire_exclusive(alias)
        assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD

    with MaintenanceLease.acquire_exclusive(state):
        with pytest.raises(MaintenanceBusyError) as raised:
            MaintenanceLease.acquire_shared(alias)
        assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD


def test_lock_creation_is_persistent_single_link_and_syncs_file_and_state_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    synced_types: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        synced_types.append(stat.S_IFMT(os.fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    with MaintenanceLease.acquire_shared(state):
        pass

    lock = state / MAINTENANCE_LOCK_FILENAME
    assert lock.is_file()
    assert lock.stat().st_nlink == 1
    assert stat.S_IFREG in synced_types
    assert stat.S_IFDIR in synced_types


@pytest.mark.parametrize("configured", [Path("/"), Path("//")])
def test_filesystem_root_cannot_be_a_state_directory(configured: Path) -> None:
    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(configured)

    assert raised.value.reason is MaintenanceIntegrityReason.STATE_INVALID


@pytest.mark.parametrize("shape", ["missing", "regular-file"])
def test_missing_or_nondirectory_state_is_rejected_without_creation(
    tmp_path: Path,
    shape: str,
) -> None:
    state = tmp_path / "state"
    if shape == "regular-file":
        state.write_bytes(b"unchanged")

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.STATE_INVALID
    if shape == "missing":
        assert not state.exists()
    else:
        assert state.read_bytes() == b"unchanged"


def test_state_directory_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"unchanged")
    state = tmp_path / "state"
    state.symlink_to(target, target_is_directory=True)

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.STATE_INVALID
    assert sentinel.read_bytes() == b"unchanged"
    assert not (target / MAINTENANCE_LOCK_FILENAME).exists()


def test_lock_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    (state / MAINTENANCE_LOCK_FILENAME).symlink_to(target)

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.LOCK_INVALID
    assert target.read_bytes() == b"unchanged"


def test_hard_linked_lock_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lock = state / MAINTENANCE_LOCK_FILENAME
    lock.write_bytes(b"")
    outside_link = tmp_path / "outside-link"
    os.link(lock, outside_link)

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.LOCK_INVALID
    assert lock.stat().st_nlink == 2


def test_state_replacement_during_acquisition_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    displaced = tmp_path / "displaced-state"
    real_open_lock = maintenance_module._open_or_create_lock

    def replace_state(path: Path, descriptor: int) -> tuple[int, object]:
        result = real_open_lock(path, descriptor)
        state.rename(displaced)
        state.mkdir()
        return result

    monkeypatch.setattr(maintenance_module, "_open_or_create_lock", replace_state)
    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.STATE_CHANGED
    assert (displaced / MAINTENANCE_LOCK_FILENAME).is_file()
    assert not (state / MAINTENANCE_LOCK_FILENAME).exists()


def test_lock_replacement_during_acquisition_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    real_open_lock = maintenance_module._open_or_create_lock

    def replace_lock(path: Path, descriptor: int) -> tuple[int, object]:
        result = real_open_lock(path, descriptor)
        lock = path / MAINTENANCE_LOCK_FILENAME
        lock.unlink()
        lock.write_bytes(b"replacement")
        return result

    monkeypatch.setattr(maintenance_module, "_open_or_create_lock", replace_lock)
    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.LOCK_CHANGED
    assert (state / MAINTENANCE_LOCK_FILENAME).read_bytes() == b"replacement"


def test_active_quarantine_marker_blocks_shared_but_not_exclusive_maintenance(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"future canonical JSON is intentionally not parsed here")

    with pytest.raises(MaintenanceBusyError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceBusyReason.QUARANTINE_ACTIVE
    with MaintenanceLease.acquire_exclusive(state):
        assert marker.is_file()


@pytest.mark.parametrize(
    "aliased_marker",
    [
        Path("Quarantine/v1/ACTIVE"),
        Path("quarantine/V1/ACTIVE"),
        Path("quarantine/v1/active"),
    ],
)
def test_case_aliased_quarantine_fence_component_is_an_integrity_denial(
    tmp_path: Path,
    aliased_marker: Path,
) -> None:
    state = tmp_path / "state"
    marker = state / aliased_marker
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"case-aliased fence")
    canonical_marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
    if not canonical_marker.exists():
        pytest.skip("filesystem is case-sensitive and permits distinct case aliases")

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID


def test_quarantine_fence_exact_name_scan_has_a_conservative_entry_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEntry:
        def __init__(self, name: str) -> None:
            self.name = name

    class FakeScandir:
        def __enter__(self) -> FakeScandir:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def __iter__(self) -> Iterator[FakeEntry]:
            return iter((FakeEntry("first"), FakeEntry("second"), FakeEntry("ACTIVE")))

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(maintenance_module, "_MAX_FENCE_DIRECTORY_ENTRIES", 2)
    monkeypatch.setattr(os, "scandir", lambda _descriptor: FakeScandir())

    with pytest.raises(MaintenanceIntegrityError) as raised:
        maintenance_module._require_exact_fence_entry_name(
            0,
            "ACTIVE",
            state,
            label="quarantine ACTIVE marker",
        )

    assert raised.value.reason is MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID
    assert "inspection limit" in raised.value.detail


@pytest.mark.parametrize("invalid_shape", ["ancestor_symlink", "marker_symlink", "marker_hardlink"])
def test_invalid_quarantine_fence_namespace_is_an_integrity_denial(
    tmp_path: Path,
    invalid_shape: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    quarantine = state / "quarantine"
    if invalid_shape == "ancestor_symlink":
        target = tmp_path / "outside-quarantine"
        target.mkdir()
        quarantine.symlink_to(target, target_is_directory=True)
    else:
        marker = state / QUARANTINE_ACTIVE_RELATIVE_PATH
        marker.parent.mkdir(parents=True)
        target = tmp_path / "outside-marker"
        target.write_bytes(b"marker")
        if invalid_shape == "marker_symlink":
            marker.symlink_to(target)
        else:
            os.link(target, marker)

    with pytest.raises(MaintenanceIntegrityError) as raised:
        MaintenanceLease.acquire_shared(state)

    assert raised.value.reason is MaintenanceIntegrityReason.QUARANTINE_FENCE_INVALID


def test_verified_state_descriptor_duplicates_identity_and_closes_on_exit(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    borrowed_descriptor = -1
    try:
        with lease.verified_state_descriptor() as descriptor:
            borrowed_descriptor = descriptor
            assert descriptor != lease._held.state_descriptor
            borrowed = os.fstat(descriptor)
            held = os.fstat(lease._held.state_descriptor)
            assert stat.S_ISDIR(borrowed.st_mode)
            assert (borrowed.st_dev, borrowed.st_ino) == (held.st_dev, held.st_ino)
            assert (
                os.stat(
                    MAINTENANCE_LOCK_FILENAME,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                ).st_ino
                == os.lstat(state / MAINTENANCE_LOCK_FILENAME).st_ino
            )

        with pytest.raises(OSError) as raised:
            os.fstat(borrowed_descriptor)
        assert raised.value.errno == errno.EBADF
        assert stat.S_ISDIR(os.fstat(lease._held.state_descriptor).st_mode)
    finally:
        lease.close()


def test_verified_state_descriptor_requires_exclusive_mode_when_requested(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()

    with (
        MaintenanceLease.acquire_shared(state) as lease,
        pytest.raises(MaintenanceIntegrityError) as raised,
        lease.verified_state_descriptor(require_exclusive=True),
    ):
        pytest.fail("shared lease must not lend an exclusive maintenance descriptor")
    assert raised.value.reason is MaintenanceIntegrityReason.LEASE_MODE_REQUIRED

    with (
        MaintenanceLease.acquire_exclusive(state) as lease,
        lease.verified_state_descriptor(require_exclusive=True) as descriptor,
    ):
        assert stat.S_ISDIR(os.fstat(descriptor).st_mode)


def test_verified_state_descriptor_rejects_wrong_process_ownership(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_exclusive(state)
    owner_pid = lease._held.owner_pid
    lease._held.owner_pid = owner_pid + 1
    try:
        with (
            pytest.raises(MaintenanceIntegrityError) as raised,
            lease.verified_state_descriptor(require_exclusive=True),
        ):
            pytest.fail("another process's lease descriptor must not be borrowed")
        assert raised.value.reason is MaintenanceIntegrityReason.PROCESS_CHANGED
    finally:
        lease._held.owner_pid = owner_pid
        lease.close()


def test_verified_state_descriptor_rejects_wrong_registry_ownership(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_exclusive(state)
    held = lease._held
    del maintenance_module._registry[held.state_directory]
    try:
        with (
            pytest.raises(MaintenanceIntegrityError) as raised,
            lease.verified_state_descriptor(require_exclusive=True),
        ):
            pytest.fail("an unregistered lease descriptor must not be borrowed")
        assert raised.value.reason is MaintenanceIntegrityReason.STATE_CHANGED
    finally:
        maintenance_module._registry[held.state_directory] = held
        lease.close()


def test_verified_state_descriptor_closes_duplicate_when_body_raises(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_exclusive(state)
    borrowed_descriptor = -1
    try:
        with (
            pytest.raises(RuntimeError, match="injected descriptor body failure"),
            lease.verified_state_descriptor(require_exclusive=True) as descriptor,
        ):
            borrowed_descriptor = descriptor
            raise RuntimeError("injected descriptor body failure")

        with pytest.raises(OSError) as raised:
            os.fstat(borrowed_descriptor)
        assert raised.value.errno == errno.EBADF
    finally:
        lease.close()


def test_verified_state_descriptor_does_not_close_reused_descriptor_number(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    collateral = tmp_path / "collateral"
    collateral.write_bytes(b"must remain open")
    collateral_source = os.open(collateral, os.O_RDONLY)
    lease = MaintenanceLease.acquire_exclusive(state)
    reused_descriptor = -1
    try:
        with (
            pytest.raises(MaintenanceIntegrityError) as raised,
            lease.verified_state_descriptor(require_exclusive=True) as descriptor,
        ):
            os.close(descriptor)
            _reuse_descriptor_from_thread(collateral_source, descriptor)
            reused_descriptor = descriptor

        assert raised.value.reason is MaintenanceIntegrityReason.STATE_CHANGED
        assert os.fstat(reused_descriptor).st_ino == os.fstat(collateral_source).st_ino
        assert os.read(reused_descriptor, 4) == b"must"
    finally:
        if reused_descriptor >= 0:
            os.close(reused_descriptor)
        os.close(collateral_source)
        lease.close()


def test_verified_state_descriptor_preserves_body_error_after_descriptor_reuse(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    collateral = tmp_path / "collateral"
    collateral.write_bytes(b"must remain open")
    collateral_source = os.open(collateral, os.O_RDONLY)
    lease = MaintenanceLease.acquire_exclusive(state)
    reused_descriptor = -1
    try:
        with (
            pytest.raises(RuntimeError, match="injected body failure") as raised,
            lease.verified_state_descriptor(require_exclusive=True) as descriptor,
        ):
            os.close(descriptor)
            _reuse_descriptor_from_thread(collateral_source, descriptor)
            reused_descriptor = descriptor
            raise RuntimeError("injected body failure")

        assert any(
            "borrowed state-directory descriptor release also failed" in note
            for note in raised.value.__notes__
        )
        assert os.fstat(reused_descriptor).st_ino == os.fstat(collateral_source).st_ino
    finally:
        if reused_descriptor >= 0:
            os.close(reused_descriptor)
        os.close(collateral_source)
        lease.close()


def test_verified_state_descriptor_keeps_owning_lease_open_until_context_exit(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_exclusive(state)
    with lease.verified_state_descriptor(require_exclusive=True):
        with pytest.raises(MaintenanceIntegrityError) as raised:
            lease.close()
        assert raised.value.reason is MaintenanceIntegrityReason.LEASE_DESCRIPTOR_BORROWED

        competing_descriptor = os.open(state / MAINTENANCE_LOCK_FILENAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competing_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(competing_descriptor)

    lease.close()
    with MaintenanceLease.acquire_shared(state):
        pass


def test_verified_state_descriptor_closes_duplicate_after_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_exclusive(state)
    real_dup = os.dup
    real_require_identity = maintenance_module._require_state_path_identity
    duplicated: list[int] = []
    identity_checks = 0

    def recording_dup(descriptor: int) -> int:
        duplicate = real_dup(descriptor)
        duplicated.append(duplicate)
        return duplicate

    def fail_after_duplication(
        path: Path,
        expected: maintenance_module._FileIdentity,
    ) -> None:
        nonlocal identity_checks
        identity_checks += 1
        if identity_checks == 2:
            raise MaintenanceIntegrityError(
                MaintenanceIntegrityReason.STATE_CHANGED,
                state_directory=path,
                detail="injected post-duplication identity failure",
            )
        real_require_identity(path, expected)

    monkeypatch.setattr(os, "dup", recording_dup)
    monkeypatch.setattr(
        maintenance_module,
        "_require_state_path_identity",
        fail_after_duplication,
    )
    try:
        with (
            pytest.raises(MaintenanceIntegrityError) as raised,
            lease.verified_state_descriptor(require_exclusive=True),
        ):
            pytest.fail("failed descriptor validation must not enter its body")
        assert raised.value.reason is MaintenanceIntegrityReason.STATE_CHANGED
        assert len(duplicated) >= 2
        for descriptor in set(duplicated):
            with pytest.raises(OSError) as closed:
                os.fstat(descriptor)
            assert closed.value.errno == errno.EBADF
    finally:
        monkeypatch.setattr(
            maintenance_module,
            "_require_state_path_identity",
            real_require_identity,
        )
        lease.close()
