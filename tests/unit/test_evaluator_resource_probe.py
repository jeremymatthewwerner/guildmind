from __future__ import annotations

import errno
import hashlib
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import guildmind.sandbox.resource_probe as resource_probe_contract

_REPOSITORY_ROOT = Path(__file__).parents[2]
_IMAGE_ROOT = _REPOSITORY_ROOT / "containers" / "evaluator"
_PROBE_PATH = _IMAGE_ROOT / "resource_probe.py"


def _load_probe() -> dict[str, Any]:
    return runpy.run_path(str(_PROBE_PATH))


def test_limits_probe_reads_cgroup_v2_and_binds_its_program(tmp_path: Path) -> None:
    readings = {
        "cgroup.controllers": "cpu memory pids\n",
        "cpu.max": "100000 100000\n",
        "memory.max": "134217728\n",
        "memory.swap.max": "0\n",
        "pids.current": "2\n",
        "pids.events": "max 3\n",
        "pids.events.local": "max 2\n",
        "pids.max": "32\n",
    }
    for name, value in readings.items():
        (tmp_path / name).write_text(value, encoding="ascii")
    namespace = _load_probe()
    probe = cast(Callable[[], dict[str, object]], namespace["_probe_limits"])
    probe.__globals__["_CGROUP_ROOT"] = tmp_path

    result = probe()

    assert result == {
        "cpu_max": "100000 100000",
        "memory_max": "134217728",
        "memory_swap_max": "0",
        "pids_current": 2,
        "pids_events": {"max": 3},
        "pids_events_local": {"max": 2},
        "pids_max": "32",
        "probe": "limits",
        "program_sha256": hashlib.sha256(_PROBE_PATH.read_bytes()).hexdigest(),
        "schema_version": "guildmind.resource-probe/v1",
    }


def test_command_surface_rejects_values_and_extra_arguments_without_echoing_them(
    capsys: Any,
) -> None:
    main = cast(Callable[[list[str]], int], _load_probe()["main"])

    assert main(["resource_probe.py", "not-a-command"]) == 2
    assert main(["resource_probe.py", "limits", "arbitrary-code-or-path"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("usage: resource_probe.py {limits|memory|pids|disk}\n") == 2
    assert "not-a-command" not in captured.err
    assert "arbitrary-code-or-path" not in captured.err


def test_canonical_probe_output_is_sorted_ascii_and_bounded() -> None:
    canonical = cast(Callable[[dict[str, object]], str], _load_probe()["_canonical_json"])

    encoded = canonical({"z": "snowman: \u2603", "a": 1})

    assert encoded == '{"a":1,"z":"snowman: \\u2603"}'
    assert encoded.isascii()


def test_disk_mount_probe_stops_on_enospc_and_removes_its_file(
    tmp_path: Path,
) -> None:
    namespace = _load_probe()
    writes = 0

    def bounded_write(file_descriptor: int, chunk: bytes) -> int:
        nonlocal writes
        del file_descriptor
        writes += 1
        if writes == 3:
            raise OSError(errno.ENOSPC, "simulated full filesystem")
        return len(chunk)

    probe = cast(Callable[..., dict[str, object]], namespace["_probe_disk_mount"])
    probe.__globals__["_write_chunk"] = bounded_write

    result = probe(tmp_path, configured_bytes=4_194_304, filename="probe")

    assert result["bytes_written"] == 2_097_152
    assert result["configured_bytes"] == 4_194_304
    assert result["failure_errno"] == errno.ENOSPC
    assert result["path"] == str(tmp_path)
    assert not (tmp_path / "probe").exists()


def test_evaluator_image_includes_the_fixed_probe_program() -> None:
    dockerfile = (_IMAGE_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (_IMAGE_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY --chmod=0555 resource_probe.py /opt/guildmind/resource_probe.py" in dockerfile
    assert "!resource_probe.py" in dockerignore.splitlines()
    assert hashlib.sha256(_PROBE_PATH.read_bytes()).hexdigest() == (
        resource_probe_contract._EXPECTED_PROGRAM_SHA256
    )
