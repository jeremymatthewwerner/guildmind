from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

import pytest

from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxMount,
    SandboxRequest,
    SandboxStatus,
    SandboxUnavailableError,
    assess_docker_info,
)

IMAGE_REFERENCE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"
IMAGE_ID = f"sha256:{'b' * 64}"
CONTAINER_ID = "c" * 64


def reference_docker_info() -> dict[str, object]:
    return {
        "Architecture": "x86_64",
        "CgroupVersion": "2",
        "CpuCfsPeriod": True,
        "CpuCfsQuota": True,
        "CPUShares": True,
        "MemoryLimit": True,
        "OSType": "linux",
        "PidsLimit": True,
        "SecurityOptions": [
            "name=rootless",
            "name=seccomp,profile=builtin",
            "name=cgroupns",
        ],
        "SwapLimit": True,
    }


def inspected_image(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "Architecture": "amd64",
        "Config": {"Volumes": None},
        "Id": IMAGE_ID,
        "Os": "linux",
        "RepoDigests": [IMAGE_REFERENCE],
    }
    value.update(changes)
    return value


def limits(**changes: object) -> SandboxLimits:
    values: dict[str, object] = {
        "cpu_cores": 1.5,
        "memory_bytes": 268_435_456,
        "pids": 64,
        "workspace_bytes": 33_554_432,
        "temporary_bytes": 8_388_608,
        "output_bytes": 1_024,
        "wall_time_seconds": 10.0,
    }
    values.update(changes)
    return SandboxLimits(**values)  # type: ignore[arg-type]


def request(source: Path, *, selected_limits: SandboxLimits | None = None) -> SandboxRequest:
    return SandboxRequest(
        execution_id="evaluation-001",
        image=IMAGE_REFERENCE,
        argv=("/usr/bin/python3", "-I", "/opt/guildmind/evaluate.py"),
        limits=selected_limits or limits(),
        environment={"TZ": "UTC", "LANG": "C"},
        mounts=(SandboxMount(source=source, target="/input/source"),),
    )


class FakeAttachedProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        running: bool = False,
    ) -> None:
        stdout_read, self._stdout_write = os.pipe()
        stderr_read, self._stderr_write = os.pipe()
        self.stdout: BinaryIO | None = os.fdopen(stdout_read, "rb", buffering=0)
        self.stderr: BinaryIO | None = os.fdopen(stderr_read, "rb", buffering=0)
        if stdout:
            os.write(self._stdout_write, stdout)
        if stderr:
            os.write(self._stderr_write, stderr)
        self._exit_code = exit_code
        self._running = running
        if not running:
            self._close_writers()

    def poll(self) -> int | None:
        return None if self._running else self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self._running:
            raise subprocess.TimeoutExpired("fake docker attach", timeout or 0.0)
        return self._exit_code

    def kill(self) -> None:
        self._running = False
        self._exit_code = -9
        self._close_writers()

    def _close_writers(self) -> None:
        for descriptor_name in ("_stdout_write", "_stderr_write"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, descriptor_name, -1)


class FakeDockerRunner:
    def __init__(
        self,
        *,
        info: dict[str, object] | None = None,
        image: dict[str, object] | None = None,
        state: dict[str, object] | None = None,
        stdout: bytes = b"",
        stderr: bytes = b"",
        process_running: bool = False,
        create_returncode: int = 0,
        create_stdout: bytes = f"{CONTAINER_ID}\n".encode(),
        kill_returncode: int = 0,
        kill_stderr: bytes = b"",
        remove_returncode: int = 0,
    ) -> None:
        self.info = info or reference_docker_info()
        self.image = image or inspected_image()
        self.state = state or {
            "Error": "",
            "ExitCode": 0,
            "OOMKilled": False,
            "Running": False,
        }
        self.process_stdout = stdout
        self.process_stderr = stderr
        self.process_running = process_running
        self.create_returncode = create_returncode
        self.create_stdout = create_stdout
        self.kill_returncode = kill_returncode
        self.kill_stderr = kill_stderr
        self.remove_returncode = remove_returncode
        self.calls: list[tuple[str, ...]] = []
        self.process: FakeAttachedProcess | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        command = tuple(argv)
        self.calls.append(command)
        if command[1] == "info":
            return _completed(command, stdout=json.dumps(self.info).encode())
        if command[1:3] == ("image", "inspect"):
            return _completed(command, stdout=json.dumps(self.image).encode())
        if command[1] == "create":
            return _completed(
                command,
                returncode=self.create_returncode,
                stdout=self.create_stdout,
                stderr=b"create rejected" if self.create_returncode else b"",
            )
        if command[1] == "inspect":
            return _completed(command, stdout=json.dumps(self.state).encode())
        if command[1] == "kill":
            return _completed(
                command,
                returncode=self.kill_returncode,
                stderr=self.kill_stderr,
            )
        if command[1] == "rm":
            return _completed(
                command,
                returncode=self.remove_returncode,
                stderr=b"remove rejected" if self.remove_returncode else b"",
            )
        raise AssertionError(f"unexpected Docker command: {command}")

    def popen(self, argv: Sequence[str]) -> FakeAttachedProcess:
        command = tuple(argv)
        self.calls.append(command)
        assert command[1:3] == ("start", "--attach")
        exit_code = self.state.get("ExitCode", 0)
        assert isinstance(exit_code, int)
        self.process = FakeAttachedProcess(
            stdout=self.process_stdout,
            stderr=self.process_stderr,
            exit_code=exit_code,
            running=self.process_running,
        )
        return self.process


def _completed(
    argv: Sequence[str],
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def test_reference_host_assessment_accepts_every_required_control() -> None:
    assessment = assess_docker_info(reference_docker_info())

    assert assessment.accepted
    assert assessment.reference_ready
    assert assessment.failures == ()
    assert assessment.warnings == ()


@pytest.mark.parametrize(
    ("field", "value", "failure"),
    [
        ("OSType", "windows", "docker_os_not_linux"),
        ("Architecture", "aarch64", "architecture_not_x86_64"),
        ("CgroupVersion", "1", "cgroup_v2_required"),
        ("MemoryLimit", False, "memory_limit_unavailable"),
        ("SwapLimit", False, "swap_limit_unavailable"),
        ("CpuCfsPeriod", False, "cpu_period_limit_unavailable"),
        ("CpuCfsQuota", False, "cpu_quota_limit_unavailable"),
        ("CPUShares", False, "cpu_shares_unavailable"),
        ("PidsLimit", False, "pid_limit_unavailable"),
    ],
)
def test_reference_host_assessment_fails_closed(
    field: str,
    value: object,
    failure: str,
) -> None:
    info = reference_docker_info()
    info[field] = value

    assessment = assess_docker_info(info)

    assert not assessment.accepted
    assert not assessment.reference_ready
    assert failure in assessment.failures


@pytest.mark.parametrize(
    ("options", "failure"),
    [
        (
            ["name=seccomp,profile=builtin", "name=cgroupns"],
            "rootless_required",
        ),
        (["name=rootless", "name=cgroupns"], "builtin_seccomp_required"),
        (
            ["name=rootless", "name=seccomp,profile=builtin"],
            "private_cgroupns_unavailable",
        ),
    ],
)
def test_reference_host_requires_security_options(options: list[str], failure: str) -> None:
    info = reference_docker_info()
    info["SecurityOptions"] = options

    assessment = assess_docker_info(info)

    assert not assessment.accepted
    assert failure in assessment.failures


def test_development_policy_is_explicit_and_never_claims_reference_readiness() -> None:
    info = reference_docker_info()
    info["Architecture"] = "aarch64"
    info["SecurityOptions"] = [
        "name=seccomp,profile=builtin",
        "name=cgroupns",
    ]

    assessment = assess_docker_info(info, policy=DockerHostPolicy.development_only())

    assert assessment.accepted
    assert not assessment.reference_ready
    assert assessment.failures == ()
    assert assessment.warnings == ("architecture_not_x86_64", "rootless_required")


def test_development_policy_does_not_relax_resource_enforcement() -> None:
    info = reference_docker_info()
    info["MemoryLimit"] = False

    assessment = assess_docker_info(info, policy=DockerHostPolicy.development_only())

    assert not assessment.accepted
    assert assessment.failures == ("memory_limit_unavailable",)


def test_request_rejects_tags_relative_commands_and_writable_mount_overlap(tmp_path: Path) -> None:
    with pytest.raises(SandboxConfigurationError, match="pinned only by sha256"):
        SandboxRequest(
            execution_id="evaluation-001",
            image="registry.example/guildmind/evaluator:latest",
            argv=("/usr/bin/python3",),
            limits=limits(),
        )
    with pytest.raises(SandboxConfigurationError, match=r"argv\[0\]"):
        SandboxRequest(
            execution_id="evaluation-001",
            image=IMAGE_REFERENCE,
            argv=("python3",),
            limits=limits(),
        )
    with pytest.raises(SandboxConfigurationError, match="cannot overlap"):
        SandboxMount(source=tmp_path, target="/workspace/source")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cpu_cores", 0.0),
        ("memory_bytes", 0),
        ("pids", True),
        ("workspace_bytes", -1),
        ("temporary_bytes", 0),
        ("output_bytes", 0),
        ("wall_time_seconds", float("inf")),
    ],
)
def test_limits_are_always_positive_and_finite(field: str, value: object) -> None:
    with pytest.raises(SandboxConfigurationError):
        limits(**{field: value})


def test_docker_create_uses_the_exact_mandatory_security_contract(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(stdout=b"ok\n")
    sandbox = DockerSandbox(
        command_runner=runner,
        container_name_suffix_factory=lambda: "0" * 12,
    )

    result = sandbox.run(request(source))

    assert result.status is SandboxStatus.EXITED
    assert result.exit_code == 0
    assert result.stdout == b"ok\n"
    create = next(command for command in runner.calls if command[1] == "create")
    assert create == (
        "docker",
        "create",
        "--name=guildmind-evaluation-001-000000000000",
        "--pull=never",
        "--label=guildmind.managed=true",
        "--label=guildmind.execution_id=evaluation-001",
        "--log-driver=none",
        "--network=none",
        "--no-healthcheck",
        "--read-only",
        "--user=65532:65532",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--security-opt=seccomp=builtin",
        "--cgroupns=private",
        "--ipc=none",
        "--init",
        "--hostname=guildmind",
        "--cpus=1.5",
        "--memory=268435456",
        "--memory-swap=268435456",
        "--pids-limit=64",
        "--tmpfs=/workspace:rw,noexec,nosuid,nodev,size=33554432,uid=65532,gid=65532,mode=0700",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=8388608,uid=65532,gid=65532,mode=0700",
        "--workdir=/workspace",
        "--env=LANG=C",
        "--env=TZ=UTC",
        f"--mount=type=bind,source={source},target=/input/source,readonly,"
        "bind-recursive=readonly,bind-propagation=rprivate",
        "--entrypoint=/usr/bin/python3",
        IMAGE_REFERENCE,
        "-I",
        "/opt/guildmind/evaluate.py",
    )
    assert ("docker", "start", "--attach", CONTAINER_ID) in runner.calls
    assert ("docker", "inspect", "--format={{json .State}}", CONTAINER_ID) in runner.calls
    assert runner.calls[-1] == ("docker", "rm", "--force", CONTAINER_ID)


def test_repeated_execution_ids_receive_unique_container_names(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    suffixes = iter(("1" * 12, "2" * 12))
    runner = FakeDockerRunner()
    sandbox = DockerSandbox(
        command_runner=runner,
        container_name_suffix_factory=lambda: next(suffixes),
    )

    first = sandbox.run(request(source))
    second = sandbox.run(request(source))

    creates = [command for command in runner.calls if command[1] == "create"]
    assert creates[0][2] == "--name=guildmind-evaluation-001-111111111111"
    assert creates[1][2] == "--name=guildmind-evaluation-001-222222222222"
    assert first.execution_id == second.execution_id == "evaluation-001"


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (inspected_image(Architecture="arm64"), "architecture"),
        (inspected_image(Os="windows"), "OS"),
        (inspected_image(Config={"Volumes": {"/data": {}}}), "declare volumes"),
        (inspected_image(RepoDigests=[]), "requested repository digest"),
    ],
)
def test_image_inspection_rejects_non_reference_images(
    tmp_path: Path,
    image: dict[str, object],
    message: str,
) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(image=image)

    with pytest.raises(SandboxUnavailableError, match=message):
        DockerSandbox(command_runner=runner).run(request(source))

    assert not any(command[1] == "create" for command in runner.calls)


def test_verify_image_checks_local_identity_without_running_it() -> None:
    runner = FakeDockerRunner()
    sandbox = DockerSandbox(command_runner=runner)

    assert sandbox.verify_image(IMAGE_REFERENCE) == IMAGE_ID
    assert runner.calls == [
        (
            "docker",
            "image",
            "inspect",
            "--format={{json .}}",
            IMAGE_REFERENCE,
        )
    ]


def test_verify_image_rejects_mutable_tags_before_invoking_docker() -> None:
    runner = FakeDockerRunner()

    with pytest.raises(SandboxConfigurationError, match="pinned only by sha256"):
        DockerSandbox(command_runner=runner).verify_image("guildmind/evaluator:latest")

    assert runner.calls == []


def test_host_rejection_happens_before_image_or_create(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    info = reference_docker_info()
    info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=cgroupns"]
    runner = FakeDockerRunner(info=info)

    with pytest.raises(SandboxUnavailableError, match="rootless_required"):
        DockerSandbox(command_runner=runner).run(request(source))

    assert len(runner.calls) == 1
    assert runner.calls[0][1] == "info"


def test_combined_output_cap_kills_and_classifies_execution(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(
        stdout=b"12345678",
        stderr=b"abcdef",
        process_running=True,
        kill_returncode=1,
        kill_stderr=b"Error response from daemon: container is not running",
    )
    selected = limits(output_bytes=10)

    result = DockerSandbox(command_runner=runner).run(request(source, selected_limits=selected))

    assert result.status is SandboxStatus.OUTPUT_EXHAUSTED
    assert result.output_truncated
    assert result.diagnostic is None
    assert len(result.stdout) + len(result.stderr) == 10
    assert ("docker", "kill", CONTAINER_ID) in runner.calls
    assert runner.calls[-1] == ("docker", "rm", "--force", CONTAINER_ID)


def test_wall_timeout_kills_and_classifies_execution(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(
        process_running=True,
        state={"Error": "", "ExitCode": 137, "OOMKilled": False, "Running": False},
    )

    result = DockerSandbox(command_runner=runner).run(
        request(source, selected_limits=limits(wall_time_seconds=0.001))
    )

    assert result.status is SandboxStatus.TIMED_OUT
    assert result.exit_code == 137
    assert ("docker", "kill", CONTAINER_ID) in runner.calls
    assert runner.process is not None and runner.process.poll() == -9


def test_inspected_oom_state_overrides_process_exit_classification(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(
        state={"Error": "", "ExitCode": 137, "OOMKilled": True, "Running": False}
    )

    result = DockerSandbox(command_runner=runner).run(request(source))

    assert result.status is SandboxStatus.OOM_KILLED
    assert result.exit_code == 137


def test_create_failure_is_typed_and_does_not_attempt_container_cleanup(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(create_returncode=1)

    result = DockerSandbox(command_runner=runner).run(request(source))

    assert result.status is SandboxStatus.INFRASTRUCTURE_ERROR
    assert result.diagnostic is not None and "create rejected" in result.diagnostic
    assert not any(command[1] == "rm" for command in runner.calls)


def test_cleanup_failure_invalidates_an_otherwise_successful_result(tmp_path: Path) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    runner = FakeDockerRunner(remove_returncode=1)

    result = DockerSandbox(command_runner=runner).run(request(source))

    assert result.status is SandboxStatus.INFRASTRUCTURE_ERROR
    assert result.diagnostic is not None and "cleanup failed" in result.diagnostic
