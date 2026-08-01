"""Active sandbox control probe used by ``guildmind doctor --production``."""

from __future__ import annotations

import json
from dataclasses import dataclass

from guildmind.sandbox.base import Sandbox, SandboxLimits, SandboxRequest, SandboxStatus

_MEMORY_BYTES = 134_217_728
_PIDS = 32
_CPU_CORES = 1.0
_PROBE = r"""
import json, os, pathlib, socket
status = pathlib.Path('/proc/self/status').read_text()
fields = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
def attempt_write(path):
    try:
        pathlib.Path(path).write_text('probe')
        return True
    except OSError:
        return False
network = True
try:
    connection = socket.socket()
    connection.settimeout(0.2)
    connection.connect(('1.1.1.1', 53))
except OSError:
    network = False
cgroup = pathlib.Path('/sys/fs/cgroup')
def read(name):
    try:
        return (cgroup / name).read_text().strip()
    except OSError:
        return None
print(json.dumps({
    'cap_eff': fields.get('CapEff', '').strip(),
    'cpu_max': read('cpu.max'),
    'docker_host': os.environ.get('DOCKER_HOST'),
    'docker_socket': pathlib.Path('/var/run/docker.sock').exists(),
    'gid': os.getgid(),
    'memory_max': read('memory.max'),
    'memory_swap_max': read('memory.swap.max'),
    'network': network,
    'no_new_privs': fields.get('NoNewPrivs', '').strip(),
    'pids_max': read('pids.max'),
    'proxy_environment': sorted(
        key for key in (
            'ALL_PROXY', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
            'all_proxy', 'http_proxy', 'https_proxy', 'no_proxy',
        ) if os.environ.get(key)
    ),
    'root_write': attempt_write('/guildmind-doctor-root-write'),
    'seccomp': fields.get('Seccomp', '').strip(),
    'temporary_write': attempt_write('/tmp/probe'),
    'uid': os.getuid(),
    'workspace_write': attempt_write('/workspace/probe'),
}, sort_keys=True))
"""


@dataclass(frozen=True, slots=True)
class SandboxSelfTestReport:
    passed: bool
    checks: dict[str, bool]
    diagnostic: str | None
    image_id: str | None


def run_sandbox_self_test(sandbox: Sandbox, *, image: str) -> SandboxSelfTestReport:
    """Run active controls in the exact configured image and fail closed on ambiguity."""
    result = sandbox.run(
        SandboxRequest(
            execution_id="doctor-reference-self-test",
            image=image,
            argv=("/usr/local/bin/python", "-I", "-c", _PROBE),
            limits=SandboxLimits(
                cpu_cores=_CPU_CORES,
                memory_bytes=_MEMORY_BYTES,
                pids=_PIDS,
                workspace_bytes=8_388_608,
                temporary_bytes=4_194_304,
                output_bytes=4_096,
                wall_time_seconds=3.0,
            ),
        )
    )
    if result.status is not SandboxStatus.EXITED or result.exit_code != 0:
        diagnostic = result.diagnostic or result.stderr.decode("utf-8", errors="replace")
        return SandboxSelfTestReport(
            passed=False,
            checks={"probe_exited_cleanly": False},
            diagnostic=diagnostic or f"sandbox probe ended as {result.status.value}",
            image_id=result.image_id,
        )
    try:
        raw = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return SandboxSelfTestReport(
            passed=False,
            checks={"probe_output_valid": False},
            diagnostic=f"sandbox probe returned malformed JSON: {error}",
            image_id=result.image_id,
        )
    if not isinstance(raw, dict):
        return SandboxSelfTestReport(
            passed=False,
            checks={"probe_output_valid": False},
            diagnostic="sandbox probe did not return an object",
            image_id=result.image_id,
        )
    checks = _evaluate_probe(raw)
    failures = [name for name, passed in checks.items() if not passed]
    return SandboxSelfTestReport(
        passed=not failures,
        checks=checks,
        diagnostic=None if not failures else f"failed active controls: {', '.join(failures)}",
        image_id=result.image_id,
    )


def _evaluate_probe(raw: dict[str, object]) -> dict[str, bool]:
    return {
        "capabilities_dropped": raw.get("cap_eff") == "0000000000000000",
        "cpu_quota": _cpu_quota_is_bounded(raw.get("cpu_max")),
        "docker_host_absent": raw.get("docker_host") is None,
        "docker_socket_absent": raw.get("docker_socket") is False,
        "memory_limit": raw.get("memory_max") == str(_MEMORY_BYTES),
        "network_disabled": raw.get("network") is False,
        "no_new_privileges": raw.get("no_new_privs") == "1",
        "non_root_gid": raw.get("gid") == 65_532,
        "non_root_uid": raw.get("uid") == 65_532,
        "pid_limit": raw.get("pids_max") == str(_PIDS),
        "proxy_environment_absent": raw.get("proxy_environment") == [],
        "root_filesystem_read_only": raw.get("root_write") is False,
        "seccomp_filter": raw.get("seccomp") == "2",
        "swap_disabled": raw.get("memory_swap_max") == "0",
        "temporary_tmpfs_writable": raw.get("temporary_write") is True,
        "workspace_tmpfs_writable": raw.get("workspace_write") is True,
    }


def _cpu_quota_is_bounded(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split()
    if len(parts) != 2 or parts[0] == "max":
        return False
    try:
        quota, period = (int(part) for part in parts)
    except ValueError:
        return False
    return quota > 0 and period > 0 and quota / period <= _CPU_CORES
