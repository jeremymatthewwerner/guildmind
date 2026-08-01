from __future__ import annotations

import json

from guildmind.sandbox import (
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    run_sandbox_self_test,
)

_IMAGE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"


def passing_probe() -> dict[str, object]:
    return {
        "cap_eff": "0000000000000000",
        "cpu_max": "100000 100000",
        "docker_host": None,
        "docker_socket": False,
        "gid": 65_532,
        "memory_max": "134217728",
        "memory_swap_max": "0",
        "network": False,
        "no_new_privs": "1",
        "pids_max": "32",
        "proxy_environment": [],
        "root_write": False,
        "seccomp": "2",
        "temporary_write": True,
        "uid": 65_532,
        "workspace_write": True,
    }


class FakeSandbox:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.request: SandboxRequest | None = None

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.request = request
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.EXITED,
            exit_code=0,
            stdout=self.payload,
            image_id=f"sha256:{'b' * 64}",
        )


def test_active_sandbox_probe_requires_every_observed_control() -> None:
    sandbox = FakeSandbox(json.dumps(passing_probe()).encode())

    report = run_sandbox_self_test(sandbox, image=_IMAGE)

    assert report.passed
    assert all(report.checks.values())
    assert report.diagnostic is None
    assert sandbox.request is not None
    assert sandbox.request.image == _IMAGE
    assert sandbox.request.limits.memory_bytes == 134_217_728
    assert sandbox.request.limits.pids == 32


def test_active_sandbox_probe_fails_closed_on_one_weak_control() -> None:
    payload = passing_probe()
    payload["network"] = True
    sandbox = FakeSandbox(json.dumps(payload).encode())

    report = run_sandbox_self_test(sandbox, image=_IMAGE)

    assert not report.passed
    assert report.checks["network_disabled"] is False
    assert report.diagnostic == "failed active controls: network_disabled"


def test_active_sandbox_probe_rejects_malformed_evidence() -> None:
    report = run_sandbox_self_test(FakeSandbox(b"not json"), image=_IMAGE)

    assert not report.passed
    assert report.checks == {"probe_output_valid": False}
    assert report.diagnostic is not None
