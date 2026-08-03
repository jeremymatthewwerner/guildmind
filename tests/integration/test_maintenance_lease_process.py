"""Real-process checks for the cooperative state-wide maintenance lease."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import threading
from contextlib import suppress
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

import guildmind.storage.maintenance as maintenance_module
from guildmind.storage.maintenance import (
    MAINTENANCE_LOCK_FILENAME,
    MaintenanceBusyError,
    MaintenanceBusyReason,
    MaintenanceIntegrityError,
    MaintenanceIntegrityReason,
    MaintenanceLease,
    MaintenanceLeaseMode,
)


def _child_hold_lease(state_text: str, mode_text: str, barrier: Connection) -> None:
    mode = MaintenanceLeaseMode(mode_text)
    acquire = (
        MaintenanceLease.acquire_shared
        if mode is MaintenanceLeaseMode.SHARED
        else MaintenanceLease.acquire_exclusive
    )
    try:
        with acquire(Path(state_text)):
            barrier.send(("acquired", mode.value))
            message = barrier.recv()
            if message != ("release", mode.value):
                raise AssertionError(f"unexpected lease command: {message!r}")
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(("error", mode.value, type(error).__name__, str(error)))
        raise
    else:
        barrier.send(("released", mode.value))
    finally:
        barrier.close()


def _fork_inheritor_then_exit_abruptly(state_text: str, write_descriptor: int) -> None:
    lease = MaintenanceLease.acquire_shared(Path(state_text))
    inherited_pid = os.fork()
    if inherited_pid == 0:
        signal.pause()
        os._exit(98)
    os.write(write_descriptor, str(inherited_pid).encode("ascii"))
    assert lease is not None
    os._exit(0)


def _start_holder(
    state: Path,
    mode: MaintenanceLeaseMode,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=_child_hold_lease,
        args=(str(state), mode.value, child_barrier),
        name=f"guildmind-maintenance-{mode.value}",
    )
    process.start()
    child_barrier.close()
    ready = wait((parent_barrier, process.sentinel), timeout=20)
    if parent_barrier not in ready:
        process.join(timeout=1)
        parent_barrier.close()
        process.close()
        pytest.fail(f"lease child exited before acquisition: exitcode={process.exitcode}")
    try:
        message = parent_barrier.recv()
    except EOFError:
        process.join(timeout=1)
        parent_barrier.close()
        process.close()
        pytest.fail(f"lease child closed its barrier: exitcode={process.exitcode}")
    if message != ("acquired", mode.value):
        process.join(timeout=5)
        parent_barrier.close()
        process.close()
        pytest.fail(f"lease child failed before acquisition: {message!r}")
    return process, parent_barrier


def _release_holder(
    process: BaseProcess,
    barrier: Connection,
    mode: MaintenanceLeaseMode,
) -> None:
    try:
        barrier.send(("release", mode.value))
        ready = wait((barrier, process.sentinel), timeout=20)
        if barrier not in ready:
            pytest.fail(f"lease child did not acknowledge release: exitcode={process.exitcode}")
        assert barrier.recv() == ("released", mode.value)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()


def test_shared_leases_coexist_across_spawned_processes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    first_process, first_barrier = _start_holder(state, MaintenanceLeaseMode.SHARED)
    try:
        second_process, second_barrier = _start_holder(state, MaintenanceLeaseMode.SHARED)
        try:
            with pytest.raises(MaintenanceBusyError) as raised:
                MaintenanceLease.acquire_exclusive(state)
            assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD
        finally:
            _release_holder(second_process, second_barrier, MaintenanceLeaseMode.SHARED)
    finally:
        _release_holder(first_process, first_barrier, MaintenanceLeaseMode.SHARED)

    with MaintenanceLease.acquire_exclusive(state):
        pass


def test_exclusive_lease_excludes_shared_and_exclusive_processes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    process, barrier = _start_holder(state, MaintenanceLeaseMode.EXCLUSIVE)
    try:
        for acquire in (
            MaintenanceLease.acquire_shared,
            MaintenanceLease.acquire_exclusive,
        ):
            with pytest.raises(MaintenanceBusyError) as raised:
                acquire(state)
            assert raised.value.reason is MaintenanceBusyReason.LEASE_HELD
    finally:
        _release_holder(process, barrier, MaintenanceLeaseMode.EXCLUSIVE)

    with MaintenanceLease.acquire_shared(state):
        pass


def test_sigkill_releases_kernel_lease_without_replacing_persistent_lock(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    process, barrier = _start_holder(state, MaintenanceLeaseMode.EXCLUSIVE)
    lock = state / MAINTENANCE_LOCK_FILENAME
    identity_before = (lock.stat().st_dev, lock.stat().st_ino)
    try:
        assert process.pid is not None
        os.kill(process.pid, signal.SIGKILL)
        process.join(timeout=10)
        assert not process.is_alive()
        assert process.exitcode == -signal.SIGKILL
    finally:
        barrier.close()
        if process.is_alive():
            process.kill()
            process.join(timeout=5)
        process.close()

    with MaintenanceLease.acquire_exclusive(state):
        assert (lock.stat().st_dev, lock.stat().st_ino) == identity_before
        assert lock.stat().st_nlink == 1


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_multithreaded_fork_child_does_not_inherit_a_vanished_registry_lock(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    registry_held = threading.Event()
    release_registry = threading.Event()
    fork_started = threading.Event()
    child_pids: list[int] = []

    def hold_registry() -> None:
        with maintenance_module._registry_lock:
            registry_held.set()
            assert release_registry.wait(timeout=10)

    def fork_while_other_thread_owns_registry() -> None:
        fork_started.set()
        pid = os.fork()
        if pid == 0:
            signal.alarm(5)
            try:
                with MaintenanceLease.acquire_shared(state):
                    pass
            except BaseException:
                os._exit(2)
            os._exit(0)
        child_pids.append(pid)

    holder = threading.Thread(target=hold_registry, name="guildmind-registry-holder")
    forker = threading.Thread(target=fork_while_other_thread_owns_registry, name="guildmind-forker")
    holder.start()
    assert registry_held.wait(timeout=10)
    forker.start()
    assert fork_started.wait(timeout=10)
    release_registry.set()
    holder.join(timeout=10)
    forker.join(timeout=10)
    assert not holder.is_alive()
    assert not forker.is_alive()
    assert len(child_pids) == 1
    _, status = os.waitpid(child_pids[0], 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_fork_inheritor_cannot_retain_lease_after_abrupt_parent_exit(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    read_descriptor, write_descriptor = os.pipe()
    holder_pid = os.fork()
    if holder_pid == 0:
        os.close(read_descriptor)
        _fork_inheritor_then_exit_abruptly(str(state), write_descriptor)
        os._exit(97)
    os.close(write_descriptor)
    inherited_pid: int | None = None
    try:
        inherited_pid = int(os.read(read_descriptor, 32).decode("ascii"))
        _, status = os.waitpid(holder_pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        os.kill(inherited_pid, 0)

        with MaintenanceLease.acquire_exclusive(state):
            pass
    finally:
        os.close(read_descriptor)
        if inherited_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(inherited_pid, signal.SIGKILL)


def test_inherited_lease_object_cannot_enter_in_fork_child(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lease = MaintenanceLease.acquire_shared(state)
    read_descriptor, write_descriptor = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_descriptor)
        body_entered = False
        try:
            with lease:
                body_entered = True
        except MaintenanceIntegrityError as error:
            child_message = f"{error.reason.value}:{int(body_entered)}".encode("ascii")
            os.write(write_descriptor, child_message)
            os._exit(0)
        except BaseException as error:
            os.write(write_descriptor, f"unexpected:{type(error).__name__}".encode("ascii"))
            os._exit(2)
        os.write(write_descriptor, b"entered")
        os._exit(3)

    os.close(write_descriptor)
    try:
        _, status = os.waitpid(child_pid, 0)
        parent_message = os.read(read_descriptor, 128).decode("ascii")
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
        assert parent_message == f"{MaintenanceIntegrityReason.PROCESS_CHANGED.value}:0"

        lock_descriptor = os.open(state / MAINTENANCE_LOCK_FILENAME, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(read_descriptor)
        lease.close()

    with MaintenanceLease.acquire_exclusive(state):
        pass
