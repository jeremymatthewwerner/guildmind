from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxLimits,
    SandboxMount,
    SandboxRequest,
    SandboxStatus,
)


def _configured_sandbox() -> tuple[DockerSandbox, str]:
    reference_image = os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE")
    if reference_image is not None:
        return DockerSandbox(), reference_image
    development_image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if development_image is not None:
        return (
            DockerSandbox(host_policy=DockerHostPolicy.development_only()),
            development_image,
        )
    pytest.skip("no digest-pinned Guildmind evaluator image is configured")


def _limits(*, output_bytes: int = 4_096, wall_time_seconds: float = 3.0) -> SandboxLimits:
    return SandboxLimits(
        cpu_cores=1.0,
        memory_bytes=134_217_728,
        pids=32,
        workspace_bytes=8_388_608,
        temporary_bytes=4_194_304,
        output_bytes=output_bytes,
        wall_time_seconds=wall_time_seconds,
    )


def _assert_container_removed(container_id: str | None) -> None:
    assert container_id is not None
    completed = subprocess.run(
        ["docker", "container", "inspect", container_id],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode != 0


@pytest.mark.container
def test_container_boundary_hides_host_and_enforces_process_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox, image = _configured_sandbox()
    host_sentinel = (tmp_path / "host-only-sentinel").resolve()
    host_sentinel.write_text("not mounted", encoding="utf-8")
    readonly_input = (tmp_path / "readonly-input").resolve()
    readonly_input.mkdir()
    (readonly_input / "sentinel").write_text("unchanged", encoding="utf-8")
    monkeypatch.setenv("GUILDMIND_HOST_SECRET_SENTINEL", "must-not-cross-boundary")
    probe = f"""
import json, os, pathlib, socket
status = pathlib.Path('/proc/self/status').read_text()
fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
root_write = True
try:
    pathlib.Path('/guildmind-root-write').write_text('no')
except OSError:
    root_write = False
workspace_write = True
try:
    pathlib.Path('/workspace/probe').write_text('yes')
except OSError:
    workspace_write = False
input_write = True
try:
    pathlib.Path('/inputs/probe/sentinel').write_text('changed')
except OSError:
    input_write = False
network = True
try:
    connection = socket.socket()
    connection.settimeout(0.2)
    connection.connect(('1.1.1.1', 53))
except OSError:
    network = False
print(json.dumps({{
    'cap_eff': fields['CapEff'].strip(),
    'docker_socket': pathlib.Path('/var/run/docker.sock').exists(),
    'host_path': pathlib.Path({str(host_sentinel)!r}).exists(),
    'host_secret': os.environ.get('GUILDMIND_HOST_SECRET_SENTINEL'),
    'input_write': input_write,
    'network': network,
    'no_new_privs': fields['NoNewPrivs'].strip(),
    'root_write': root_write,
    'uid': os.getuid(),
    'workspace_write': workspace_write,
}}, sort_keys=True))
"""
    result = sandbox.run(
        SandboxRequest(
            execution_id="boundary-security-probe",
            image=image,
            argv=("/usr/local/bin/python", "-I", "-c", probe),
            limits=_limits(),
            mounts=(SandboxMount(source=readonly_input, target="/inputs/probe"),),
        )
    )

    assert result.status is SandboxStatus.EXITED
    assert result.exit_code == 0
    evidence = json.loads(result.stdout)
    assert evidence == {
        "cap_eff": "0000000000000000",
        "docker_socket": False,
        "host_path": False,
        "host_secret": None,
        "input_write": False,
        "network": False,
        "no_new_privs": "1",
        "root_write": False,
        "uid": 65_532,
        "workspace_write": True,
    }
    assert (readonly_input / "sentinel").read_text(encoding="utf-8") == "unchanged"
    _assert_container_removed(result.container_id)


@pytest.mark.container
def test_container_boundary_terminates_output_bomb_and_removes_container() -> None:
    sandbox, image = _configured_sandbox()
    result = sandbox.run(
        SandboxRequest(
            execution_id="boundary-output-bomb",
            image=image,
            argv=("/usr/local/bin/python", "-I", "-c", "print('x' * 1000000)"),
            limits=_limits(output_bytes=1_024),
        )
    )

    assert result.status is SandboxStatus.OUTPUT_EXHAUSTED
    assert result.output_truncated
    assert len(result.stdout) + len(result.stderr) == 1_024
    _assert_container_removed(result.container_id)


@pytest.mark.container
def test_container_boundary_kills_wall_timeout_and_removes_container() -> None:
    sandbox, image = _configured_sandbox()
    result = sandbox.run(
        SandboxRequest(
            execution_id="boundary-wall-timeout",
            image=image,
            argv=("/usr/local/bin/python", "-I", "-c", "import time; time.sleep(30)"),
            limits=_limits(wall_time_seconds=0.2),
        )
    )

    assert result.status is SandboxStatus.TIMED_OUT
    _assert_container_removed(result.container_id)
