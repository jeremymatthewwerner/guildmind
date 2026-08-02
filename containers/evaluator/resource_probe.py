"""Fixed, image-owned cgroup and writable-space enforcement probes.

The command surface is deliberately closed: callers choose one of four probe names,
but cannot provide code, paths, sizes, or process counts.  This file is copied into the
evaluator image so host-side evidence can bind a probe run to these exact bytes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Never

_SCHEMA_VERSION = "guildmind.resource-probe/v1"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_READ_LIMIT = 4_096
_OUTPUT_LIMIT = 8_192

_MEMORY_CHUNK_BYTES = 16_777_216
_MEMORY_PAGE_BYTES = 4_096
_MAX_SAFE_MEMORY_LIMIT_BYTES = 1_073_741_824

_PID_FORK_ATTEMPTS = 96

_DISK_CHUNK_BYTES = 1_048_576
_WORKSPACE_BYTES = 67_108_864
_TEMPORARY_BYTES = 16_777_216
_WORKSPACE_PATH = Path("/workspace")
_TEMPORARY_PATH = Path("/tmp")

_CGROUP_FILES = frozenset(
    {
        "cgroup.controllers",
        "cpu.max",
        "memory.max",
        "memory.swap.max",
        "pids.current",
        "pids.events",
        "pids.events.local",
        "pids.max",
        "pids.peak",
    }
)


class ProbeCommand(StrEnum):
    LIMITS = "limits"
    MEMORY = "memory"
    PIDS = "pids"
    DISK = "disk"


def _read_cgroup(name: str, *, required: bool = True) -> str | None:
    if name not in _CGROUP_FILES:
        raise ValueError("resource probe attempted an unknown cgroup read")
    try:
        with (_CGROUP_ROOT / name).open("rb") as stream:
            raw = stream.read(_CGROUP_READ_LIMIT + 1)
    except OSError:
        if required:
            raise
        return None
    if len(raw) > _CGROUP_READ_LIMIT:
        raise RuntimeError("cgroup evidence exceeded its fixed read bound")
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise RuntimeError("cgroup evidence was not ASCII") from error


def _require_cgroup_v2() -> None:
    _read_cgroup("cgroup.controllers")


def _required_int(name: str) -> int:
    raw = _read_cgroup(name)
    if raw is None:
        raise RuntimeError("required cgroup integer was unavailable")
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("cgroup evidence was not an integer") from error
    if value < 0:
        raise RuntimeError("cgroup counter was negative")
    return value


def _optional_int(name: str) -> int | None:
    raw = _read_cgroup(name, required=False)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("cgroup evidence was not an integer") from error
    if value < 0:
        raise RuntimeError("cgroup counter was negative")
    return value


def _parse_counter_map(raw: str) -> dict[str, int]:
    counters: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[0] in counters:
            raise RuntimeError("cgroup counter map was malformed")
        try:
            value = int(fields[1])
        except ValueError as error:
            raise RuntimeError("cgroup counter map contained a non-integer") from error
        if value < 0:
            raise RuntimeError("cgroup counter map contained a negative value")
        counters[fields[0]] = value
    return counters


def _counter_map(name: str) -> dict[str, int]:
    raw = _read_cgroup(name)
    if raw is None:
        raise RuntimeError("required cgroup counter map was unavailable")
    return _parse_counter_map(raw)


def _optional_counter_map(name: str) -> dict[str, int] | None:
    raw = _read_cgroup(name, required=False)
    return None if raw is None else _parse_counter_map(raw)


def _program_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _canonical_json(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("ascii")) + 1 > _OUTPUT_LIMIT:
        raise RuntimeError("resource probe output exceeded its fixed bound")
    return encoded


def _emit(payload: dict[str, object]) -> None:
    encoded = _canonical_json(payload)
    stream = sys.__stdout__ or sys.stdout
    stream.write(f"{encoded}\n")
    stream.flush()


def _probe_limits() -> dict[str, object]:
    _require_cgroup_v2()
    cpu_max = _read_cgroup("cpu.max")
    memory_max = _read_cgroup("memory.max")
    memory_swap_max = _read_cgroup("memory.swap.max")
    pids_max = _read_cgroup("pids.max")
    if None in {cpu_max, memory_max, memory_swap_max, pids_max}:
        raise RuntimeError("required cgroup limit was unavailable")
    return {
        "cpu_max": cpu_max,
        "memory_max": memory_max,
        "memory_swap_max": memory_swap_max,
        "pids_current": _required_int("pids.current"),
        "pids_events": _counter_map("pids.events"),
        "pids_events_local": _optional_counter_map("pids.events.local"),
        "pids_max": pids_max,
        "probe": ProbeCommand.LIMITS.value,
        "program_sha256": _program_sha256(),
        "schema_version": _SCHEMA_VERSION,
    }


def _probe_memory() -> Never:
    """Retain and touch fixed 16 MiB chunks until the cgroup kills this process."""

    _require_cgroup_v2()
    maximum = _read_cgroup("memory.max")
    if maximum is None or maximum == "max":
        raise RuntimeError("memory probe requires a bounded cgroup limit")
    try:
        maximum_bytes = int(maximum)
    except ValueError as error:
        raise RuntimeError("memory probe found a malformed cgroup limit") from error
    if maximum_bytes <= 0 or maximum_bytes > _MAX_SAFE_MEMORY_LIMIT_BYTES:
        raise RuntimeError("memory probe requires a safe positive cgroup limit")

    retained: list[bytearray] = []
    while True:
        chunk = bytearray(_MEMORY_CHUNK_BYTES)
        retained.append(chunk)
        for offset in range(0, len(chunk), _MEMORY_PAGE_BYTES):
            chunk[offset] = 1


def _park_child() -> Never:
    while True:
        signal.pause()


def _terminate_and_reap(children: list[int]) -> None:
    for child in children:
        with suppress(ProcessLookupError):
            os.kill(child, signal.SIGKILL)
    for child in children:
        while True:
            try:
                os.waitpid(child, 0)
            except InterruptedError:
                continue
            except ChildProcessError:
                break
            else:
                break


def _probe_pids() -> dict[str, object]:
    _require_cgroup_v2()
    baseline_current = _required_int("pids.current")
    pids_max = _read_cgroup("pids.max")
    if pids_max is None:
        raise RuntimeError("required PID limit was unavailable")
    events_before = _counter_map("pids.events")
    events_local_before = _optional_counter_map("pids.events.local")

    children: list[int] = []
    fork_errno: int | None = None
    fork_error: str | None = None
    try:
        for _ in range(_PID_FORK_ATTEMPTS):
            try:
                child = os.fork()
            except OSError as error:
                fork_errno = error.errno
                fork_error = (
                    None if error.errno is None else errno.errorcode.get(error.errno, "UNKNOWN")
                )
                break
            if child == 0:
                _park_child()
            children.append(child)

        pressure_current = _required_int("pids.current")
        pids_peak = _optional_int("pids.peak")
        events_after = _counter_map("pids.events")
        events_local_after = _optional_counter_map("pids.events.local")
    finally:
        _terminate_and_reap(children)

    return {
        "baseline_current": baseline_current,
        "children_started": len(children),
        "events_after": events_after,
        "events_before": events_before,
        "events_local_after": events_local_after,
        "events_local_before": events_local_before,
        "final_current": _required_int("pids.current"),
        "fork_attempts": _PID_FORK_ATTEMPTS,
        "fork_errno": fork_errno,
        "fork_error": fork_error,
        "pids_max": pids_max,
        "pids_peak": pids_peak,
        "pressure_current": pressure_current,
        "probe": ProbeCommand.PIDS.value,
        "schema_version": _SCHEMA_VERSION,
    }


def _filesystem_bytes(path: Path) -> tuple[int, int]:
    stats = os.statvfs(path)
    fragment_bytes = stats.f_frsize or stats.f_bsize
    return stats.f_blocks * fragment_bytes, stats.f_bavail * fragment_bytes


def _write_chunk(file_descriptor: int, chunk: bytes) -> int:
    return os.write(file_descriptor, chunk)


def _probe_disk_mount(
    path: Path,
    *,
    configured_bytes: int,
    filename: str,
) -> dict[str, object]:
    stat_total_before, stat_free_before = _filesystem_bytes(path)
    probe_path = path / filename
    chunk = b"\xa5" * _DISK_CHUNK_BYTES
    bytes_written = 0
    failure_errno: int | None = None
    file_descriptor: int | None = None
    created = False
    try:
        try:
            file_descriptor = os.open(
                probe_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            created = True
        except OSError as error:
            failure_errno = error.errno

        if file_descriptor is not None:
            maximum_attempts = configured_bytes // _DISK_CHUNK_BYTES + 2
            for _ in range(maximum_attempts):
                try:
                    written = _write_chunk(file_descriptor, chunk)
                except OSError as error:
                    failure_errno = error.errno
                    break
                if written <= 0 or written > len(chunk):
                    raise RuntimeError("disk probe observed an invalid write count")
                bytes_written += written
            os.close(file_descriptor)
            file_descriptor = None
        _, stat_free_at_failure = _filesystem_bytes(path)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if created:
            probe_path.unlink()

    _, stat_free_after_cleanup = _filesystem_bytes(path)
    return {
        "bytes_written": bytes_written,
        "configured_bytes": configured_bytes,
        "failure_errno": failure_errno,
        "path": str(path),
        "stat_free_after_cleanup": stat_free_after_cleanup,
        "stat_free_at_failure": stat_free_at_failure,
        "stat_free_before": stat_free_before,
        "stat_total_before": stat_total_before,
    }


def _probe_disk() -> dict[str, object]:
    return {
        "mounts": [
            _probe_disk_mount(
                _WORKSPACE_PATH,
                configured_bytes=_WORKSPACE_BYTES,
                filename=".guildmind-resource-probe-workspace",
            ),
            _probe_disk_mount(
                _TEMPORARY_PATH,
                configured_bytes=_TEMPORARY_BYTES,
                filename=".guildmind-resource-probe-temporary",
            ),
        ],
        "probe": ProbeCommand.DISK.value,
        "schema_version": _SCHEMA_VERSION,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv if argv is None else argv)
    if len(arguments) != 2:
        sys.stderr.write("usage: resource_probe.py {limits|memory|pids|disk}\n")
        return 2
    try:
        command = ProbeCommand(arguments[1])
    except ValueError:
        sys.stderr.write("usage: resource_probe.py {limits|memory|pids|disk}\n")
        return 2

    if command is ProbeCommand.LIMITS:
        _emit(_probe_limits())
    elif command is ProbeCommand.MEMORY:
        _probe_memory()
    elif command is ProbeCommand.PIDS:
        _emit(_probe_pids())
    else:
        _emit(_probe_disk())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
