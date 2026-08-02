"""Rootless-Docker implementation of the production sandbox contract."""

from __future__ import annotations

import json
import os
import re
import secrets
import selectors
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO, Protocol, cast

from guildmind.sandbox.base import (
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    SandboxUnavailableError,
    validate_image_reference,
)

_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_NAME_SUFFIX = re.compile(r"^[0-9a-f]{12}$")
_CONTROL_TIMEOUT_SECONDS = 5.0
_MAX_DIAGNOSTIC_BYTES = 4_096
_CONTAINER_UID = 65_532
_CONTAINER_GID = 65_532


class DockerHostMode(StrEnum):
    REFERENCE = "reference"
    DEVELOPMENT_ONLY = "development_only"


@dataclass(frozen=True, slots=True)
class DockerHostPolicy:
    """Host admission policy; strict reference-host checks are the default."""

    mode: DockerHostMode = DockerHostMode.REFERENCE

    @classmethod
    def development_only(cls) -> DockerHostPolicy:
        """Allow rootful or non-x86 development daemons, never reference evidence."""
        return cls(mode=DockerHostMode.DEVELOPMENT_ONLY)


@dataclass(frozen=True, slots=True)
class DockerHostAssessment:
    """Pure, inspectable result of evaluating Docker info JSON."""

    accepted: bool
    reference_ready: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    def require_accepted(self) -> None:
        if not self.accepted:
            detail = ", ".join(self.failures) or "unknown host-policy failure"
            raise SandboxUnavailableError(f"Docker host does not satisfy sandbox policy: {detail}")


def assess_docker_info(
    info: Mapping[str, object],
    *,
    policy: DockerHostPolicy | None = None,
) -> DockerHostAssessment:
    """Assess Docker's structured info without consulting ambient host state."""
    selected_policy = policy or DockerHostPolicy()
    mandatory_failures: list[str] = []
    reference_failures: list[str] = []

    if info.get("OSType") != "linux":
        mandatory_failures.append("docker_os_not_linux")
    architecture = info.get("Architecture")
    if architecture not in {"x86_64", "amd64"}:
        reference_failures.append("architecture_not_x86_64")
    if str(info.get("CgroupVersion")) != "2":
        mandatory_failures.append("cgroup_v2_required")

    security_options = info.get("SecurityOptions")
    if not isinstance(security_options, list) or not all(
        isinstance(option, str) for option in security_options
    ):
        mandatory_failures.append("security_options_unavailable")
        normalized_security: tuple[str, ...] = ()
    else:
        normalized_security = tuple(option.lower() for option in security_options)
    if not any(option.startswith("name=rootless") for option in normalized_security):
        reference_failures.append("rootless_required")
    if "name=seccomp,profile=builtin" not in normalized_security:
        mandatory_failures.append("builtin_seccomp_required")
    if not any(option.startswith("name=cgroupns") for option in normalized_security):
        mandatory_failures.append("private_cgroupns_unavailable")

    enforcement_fields = {
        "MemoryLimit": "memory_limit_unavailable",
        "SwapLimit": "swap_limit_unavailable",
        "CpuCfsPeriod": "cpu_period_limit_unavailable",
        "CpuCfsQuota": "cpu_quota_limit_unavailable",
        "CPUShares": "cpu_shares_unavailable",
        "PidsLimit": "pid_limit_unavailable",
    }
    for field, failure in enforcement_fields.items():
        if info.get(field) is not True:
            mandatory_failures.append(failure)

    all_failures = (*mandatory_failures, *reference_failures)
    reference_ready = not all_failures
    if selected_policy.mode is DockerHostMode.REFERENCE:
        return DockerHostAssessment(
            accepted=reference_ready,
            reference_ready=reference_ready,
            failures=tuple(all_failures),
            warnings=(),
        )
    return DockerHostAssessment(
        accepted=not mandatory_failures,
        reference_ready=reference_ready,
        failures=tuple(mandatory_failures),
        warnings=tuple(reference_failures),
    )


class _AttachedProcess(Protocol):
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


class _DockerCommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]: ...

    def popen(self, argv: Sequence[str]) -> _AttachedProcess: ...


class _SubprocessDockerCommandRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def popen(self, argv: Sequence[str]) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )


@dataclass(frozen=True, slots=True)
class _ImageIdentity:
    image_id: str


@dataclass(frozen=True, slots=True)
class _CaptureResult:
    stdout: bytes
    stderr: bytes
    termination: SandboxStatus | None
    kill: DockerKillEvidence


@dataclass(frozen=True, slots=True)
class DockerContainerState:
    """Normalized state inspected before a managed container is removed."""

    exit_code: int
    oom_killed: bool
    running: bool
    error: str


@dataclass(frozen=True, slots=True)
class DockerKillEvidence:
    """Host-side evidence for a controller-requested container kill."""

    attempted: bool
    succeeded: bool | None
    diagnostic: str | None = None


@dataclass(frozen=True, slots=True)
class DockerCleanupEvidence:
    """Host-side evidence that a managed container was removed and is absent."""

    target: str | None
    removal_attempted: bool
    removal_succeeded: bool | None
    absence_checked: bool
    absent: bool | None
    diagnostic: str | None = None

    @property
    def confirmed(self) -> bool:
        return self.removal_succeeded is True and self.absent is True


@dataclass(frozen=True, slots=True)
class DockerExecutionEvidence:
    """Lifecycle observations retained separately from the stable sandbox result."""

    container_id: str | None
    image_id: str | None
    state: DockerContainerState | None
    termination_trigger: SandboxStatus | None
    kill: DockerKillEvidence
    cleanup: DockerCleanupEvidence
    pre_cleanup_status: SandboxStatus


@dataclass(frozen=True, slots=True)
class ObservedSandboxRun:
    """A normal sandbox result paired with Docker-specific lifecycle evidence."""

    result: SandboxResult
    evidence: DockerExecutionEvidence


@dataclass(frozen=True, slots=True)
class _PreCleanupRun:
    result: SandboxResult
    state: DockerContainerState | None
    termination_trigger: SandboxStatus | None
    kill: DockerKillEvidence


class _CombinedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.exhausted = False

    def feed(self, stream_name: str, chunk: bytes) -> None:
        retained = len(self.stdout) + len(self.stderr)
        remaining = max(0, self.limit - retained)
        destination = self.stdout if stream_name == "stdout" else self.stderr
        destination.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.exhausted = True


class DockerSandbox:
    """Execute a request through a fixed, fail-closed Docker CLI lifecycle."""

    def __init__(
        self,
        *,
        host_policy: DockerHostPolicy | None = None,
        docker_executable: str = "docker",
        command_runner: _DockerCommandRunner | None = None,
        container_name_suffix_factory: Callable[[], str] | None = None,
    ) -> None:
        if not docker_executable or "\x00" in docker_executable:
            raise ValueError("docker_executable must be a non-empty command")
        self.host_policy = host_policy or DockerHostPolicy()
        self.docker_executable = docker_executable
        self._runner: _DockerCommandRunner = command_runner or cast(
            _DockerCommandRunner, _SubprocessDockerCommandRunner()
        )
        self._container_name_suffix_factory = (
            container_name_suffix_factory or _random_container_name_suffix
        )

    def assess_host(self) -> DockerHostAssessment:
        completed = self._control(
            [self.docker_executable, "info", "--format={{json .}}"],
            unavailable_context="cannot inspect Docker host",
        )
        info = _json_object(completed.stdout, context="Docker info")
        return assess_docker_info(info, policy=self.host_policy)

    def verify_image(self, reference: str) -> str:
        """Verify a local digest-pinned image and return its immutable image ID."""
        validate_image_reference(reference)
        return self._inspect_image(reference).image_id

    def run(self, request: SandboxRequest) -> SandboxResult:
        """Run one request and return the stable adapter-independent result."""
        return self.run_observed(request).result

    def run_observed(
        self,
        request: SandboxRequest,
        *,
        verify_cleanup: bool = True,
    ) -> ObservedSandboxRun:
        """Run one request while retaining typed Docker lifecycle observations."""
        assessment = self.assess_host()
        assessment.require_accepted()
        image = self._inspect_image(request.image)
        self._validate_mount_sources(request)

        suffix = self._container_name_suffix_factory()
        if _CONTAINER_NAME_SUFFIX.fullmatch(suffix) is None:
            raise SandboxUnavailableError("container name suffix factory returned an invalid value")
        container_name = _container_name(request.execution_id, suffix)
        create_argv = self._create_argv(request, container_name=container_name)
        try:
            created = self._runner.run(create_argv, timeout=_CONTROL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            result = _infrastructure_result(
                request,
                image_id=image.image_id,
                diagnostic=f"docker create failed: {error}",
            )
            return _observed_without_container(result, image_id=image.image_id)
        if created.returncode != 0:
            result = _infrastructure_result(
                request,
                image_id=image.image_id,
                diagnostic=f"docker create failed: {_diagnostic(created)}",
            )
            return _observed_without_container(result, image_id=image.image_id)

        container_id = created.stdout.decode("ascii", errors="replace").strip()
        cleanup_target = container_id if _CONTAINER_ID.fullmatch(container_id) else container_name
        if _CONTAINER_ID.fullmatch(container_id) is None:
            cleanup = self._cleanup_container(cleanup_target, verify_absence=verify_cleanup)
            diagnostic = "docker create returned a malformed container ID"
            if cleanup.diagnostic is not None:
                diagnostic = f"{diagnostic}; cleanup failed: {cleanup.diagnostic}"
            result = _infrastructure_result(
                request,
                image_id=image.image_id,
                diagnostic=diagnostic,
            )
            return ObservedSandboxRun(
                result=result,
                evidence=DockerExecutionEvidence(
                    container_id=None,
                    image_id=image.image_id,
                    state=None,
                    termination_trigger=None,
                    kill=_no_kill_evidence(),
                    cleanup=cleanup,
                    pre_cleanup_status=result.status,
                ),
            )

        try:
            observed = self._start_and_observe(request, container_id, image.image_id)
        finally:
            cleanup = self._cleanup_container(container_id, verify_absence=verify_cleanup)
        result = observed.result
        cleanup_succeeded = (
            cleanup.confirmed if verify_cleanup else cleanup.removal_succeeded is True
        )
        if not cleanup_succeeded:
            diagnostic = cleanup.diagnostic or "container cleanup was not verified"
            result = _infrastructure_result(
                request,
                container_id=container_id,
                image_id=image.image_id,
                stdout=observed.result.stdout,
                stderr=observed.result.stderr,
                diagnostic=f"container cleanup failed: {diagnostic}",
            )
        return ObservedSandboxRun(
            result=result,
            evidence=DockerExecutionEvidence(
                container_id=container_id,
                image_id=image.image_id,
                state=observed.state,
                termination_trigger=observed.termination_trigger,
                kill=observed.kill,
                cleanup=cleanup,
                pre_cleanup_status=observed.result.status,
            ),
        )

    def _inspect_image(self, reference: str) -> _ImageIdentity:
        validate_image_reference(reference)
        completed = self._control(
            [
                self.docker_executable,
                "image",
                "inspect",
                "--format={{json .}}",
                reference,
            ],
            unavailable_context="digest-pinned image is not present locally",
        )
        raw = _json_object(completed.stdout, context="Docker image inspect")
        image_id = raw.get("Id")
        if not isinstance(image_id, str) or _IMAGE_ID.fullmatch(image_id) is None:
            raise SandboxUnavailableError("Docker image inspect returned an invalid image ID")
        repo_digests = raw.get("RepoDigests")
        if not isinstance(repo_digests, list) or reference not in repo_digests:
            raise SandboxUnavailableError(
                "local image does not carry the requested repository digest"
            )
        if raw.get("Os") != "linux":
            raise SandboxUnavailableError("sandbox image OS must be linux")
        if raw.get("Architecture") not in {"amd64", "x86_64"}:
            raise SandboxUnavailableError("sandbox image architecture must be x86_64")
        config = raw.get("Config")
        if not isinstance(config, dict):
            raise SandboxUnavailableError("sandbox image has no inspectable configuration")
        volumes = config.get("Volumes")
        if volumes not in (None, {}):
            raise SandboxUnavailableError("sandbox images may not declare volumes")
        return _ImageIdentity(image_id=image_id)

    def _validate_mount_sources(self, request: SandboxRequest) -> None:
        for mount in request.mounts:
            try:
                mode = mount.source.lstat().st_mode
                resolved = mount.source.resolve(strict=True)
            except OSError as error:
                raise SandboxUnavailableError(
                    f"sandbox mount source is unavailable: {mount.source}"
                ) from error
            if mount.source.is_symlink() or resolved != mount.source:
                raise SandboxUnavailableError(
                    f"sandbox mount source must not traverse symlinks: {mount.source}"
                )
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise SandboxUnavailableError(
                    f"sandbox mount source is not a regular file or directory: {mount.source}"
                )

    def _create_argv(self, request: SandboxRequest, *, container_name: str) -> list[str]:
        limits = request.limits
        argv = [
            self.docker_executable,
            "create",
            f"--name={container_name}",
            "--pull=never",
            "--label=guildmind.managed=true",
            f"--label=guildmind.execution_id={request.execution_id}",
            "--log-driver=none",
            "--network=none",
            "--no-healthcheck",
            "--read-only",
            f"--user={_CONTAINER_UID}:{_CONTAINER_GID}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--security-opt=seccomp=builtin",
            "--cgroupns=private",
            "--ipc=none",
            "--init",
            "--hostname=guildmind",
            f"--cpus={_format_number(limits.cpu_cores)}",
            f"--memory={limits.memory_bytes}",
            f"--memory-swap={limits.memory_bytes}",
            f"--pids-limit={limits.pids}",
            "--tmpfs="
            f"/workspace:rw,noexec,nosuid,nodev,size={limits.workspace_bytes},"
            f"uid={_CONTAINER_UID},gid={_CONTAINER_GID},mode=0700",
            "--tmpfs="
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.temporary_bytes},"
            f"uid={_CONTAINER_UID},gid={_CONTAINER_GID},mode=0700",
            f"--workdir={request.working_directory}",
        ]
        for key, value in request.environment.items():
            argv.append(f"--env={key}={value}")
        for mount in request.mounts:
            argv.append(
                "--mount="
                f"type=bind,source={mount.source},target={mount.target},"
                "readonly,bind-recursive=readonly,bind-propagation=rprivate"
            )
        argv.extend(
            [
                f"--entrypoint={request.argv[0]}",
                request.image,
                *request.argv[1:],
            ]
        )
        return argv

    def _start_and_observe(
        self,
        request: SandboxRequest,
        container_id: str,
        image_id: str,
    ) -> _PreCleanupRun:
        try:
            process = self._runner.popen(
                [self.docker_executable, "start", "--attach", container_id]
            )
            capture = self._capture_attached(process, request, container_id)
        except (OSError, subprocess.TimeoutExpired) as error:
            return _pre_cleanup_error(
                _infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    diagnostic=f"docker start failed: {error}",
                )
            )

        try:
            inspected = self._runner.run(
                [
                    self.docker_executable,
                    "inspect",
                    "--format={{json .State}}",
                    container_id,
                ],
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return _pre_cleanup_error(
                _infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                    diagnostic=f"cannot inspect completed container: {error}",
                ),
                capture=capture,
            )
        if inspected.returncode != 0:
            return _pre_cleanup_error(
                _infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                    diagnostic=f"cannot inspect completed container: {_diagnostic(inspected)}",
                ),
                capture=capture,
            )
        try:
            state = _json_object(inspected.stdout, context="Docker container state")
        except SandboxUnavailableError as error:
            return _pre_cleanup_error(
                _infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                    diagnostic=str(error),
                ),
                capture=capture,
            )
        exit_code = state.get("ExitCode")
        oom_killed = state.get("OOMKilled")
        running = state.get("Running")
        state_error = state.get("Error", "")
        if (
            isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not isinstance(oom_killed, bool)
            or not isinstance(running, bool)
            or not isinstance(state_error, str)
        ):
            return _pre_cleanup_error(
                _infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                    diagnostic="Docker returned malformed container state",
                ),
                capture=capture,
            )
        normalized_state = DockerContainerState(
            exit_code=exit_code,
            oom_killed=oom_killed,
            running=running,
            error=state_error,
        )
        if running or state_error:
            state_diagnostic = state_error or "container remained running after attached execution"
            return _PreCleanupRun(
                result=_infrastructure_result(
                    request,
                    container_id=container_id,
                    image_id=image_id,
                    stdout=capture.stdout,
                    stderr=capture.stderr,
                    diagnostic=state_diagnostic,
                ),
                state=normalized_state,
                termination_trigger=capture.termination,
                kill=capture.kill,
            )

        if capture.termination is not None:
            status = capture.termination
        elif oom_killed:
            status = SandboxStatus.OOM_KILLED
        else:
            status = SandboxStatus.EXITED
        return _PreCleanupRun(
            result=SandboxResult(
                execution_id=request.execution_id,
                status=status,
                exit_code=exit_code,
                stdout=capture.stdout,
                stderr=capture.stderr,
                output_truncated=capture.termination is SandboxStatus.OUTPUT_EXHAUSTED,
                container_id=container_id,
                image_id=image_id,
                diagnostic=capture.kill.diagnostic,
            ),
            state=normalized_state,
            termination_trigger=capture.termination,
            kill=capture.kill,
        )

    def _capture_attached(
        self,
        process: _AttachedProcess,
        request: SandboxRequest,
        container_id: str,
    ) -> _CaptureResult:
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise OSError("docker attach did not provide output pipes")

        selector = selectors.DefaultSelector()
        streams = (("stdout", process.stdout), ("stderr", process.stderr))
        capture = _CombinedCapture(request.limits.output_bytes)
        for stream_name, stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, stream_name)

        deadline = time.monotonic() + request.limits.wall_time_seconds
        termination: SandboxStatus | None = None
        kill = _no_kill_evidence()
        try:
            while selector.get_map() or process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    termination = SandboxStatus.TIMED_OUT
                    kill = self._kill_container(container_id)
                    break
                for key, _ in selector.select(timeout=min(remaining, 0.05)):
                    try:
                        chunk = os.read(key.fd, 8_192)
                    except BlockingIOError:
                        continue
                    if chunk:
                        capture.feed(cast(str, key.data), chunk)
                        if capture.exhausted:
                            termination = SandboxStatus.OUTPUT_EXHAUSTED
                            if process.poll() is None:
                                kill = self._kill_container(container_id)
                            break
                    else:
                        selector.unregister(key.fileobj)
                        cast(BinaryIO, key.fileobj).close()
                if termination is not None:
                    break
        finally:
            if termination is not None and process.poll() is None:
                process.kill()
            for key in tuple(selector.get_map().values()):
                selector.unregister(key.fileobj)
                cast(BinaryIO, key.fileobj).close()
            selector.close()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        return _CaptureResult(
            stdout=bytes(capture.stdout),
            stderr=bytes(capture.stderr),
            termination=termination,
            kill=kill,
        )

    def _kill_container(self, container_id: str) -> DockerKillEvidence:
        try:
            completed = self._runner.run(
                [self.docker_executable, "kill", container_id],
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return DockerKillEvidence(attempted=True, succeeded=False, diagnostic=str(error))
        if completed.returncode != 0:
            diagnostic = _diagnostic(completed)
            if "is not running" in diagnostic.lower():
                return DockerKillEvidence(attempted=True, succeeded=True)
            return DockerKillEvidence(
                attempted=True,
                succeeded=False,
                diagnostic=diagnostic,
            )
        return DockerKillEvidence(attempted=True, succeeded=True)

    def _cleanup_container(
        self,
        target: str,
        *,
        verify_absence: bool,
    ) -> DockerCleanupEvidence:
        try:
            completed = self._runner.run(
                [self.docker_executable, "rm", "--force", target],
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return DockerCleanupEvidence(
                target=target,
                removal_attempted=True,
                removal_succeeded=False,
                absence_checked=False,
                absent=None,
                diagnostic=str(error),
            )
        removal_succeeded = completed.returncode == 0
        removal_diagnostic = None if removal_succeeded else _diagnostic(completed)
        if not verify_absence:
            return DockerCleanupEvidence(
                target=target,
                removal_attempted=True,
                removal_succeeded=removal_succeeded,
                absence_checked=False,
                absent=None,
                diagnostic=removal_diagnostic,
            )
        absent, absence_diagnostic = self._container_is_absent(target)
        diagnostic = removal_diagnostic or absence_diagnostic
        return DockerCleanupEvidence(
            target=target,
            removal_attempted=True,
            removal_succeeded=removal_succeeded,
            absence_checked=True,
            absent=absent,
            diagnostic=diagnostic,
        )

    def _container_is_absent(self, target: str) -> tuple[bool | None, str | None]:
        try:
            completed = self._runner.run(
                [self.docker_executable, "container", "inspect", target],
                timeout=_CONTROL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, str(error)
        if completed.returncode == 0:
            return False, "container remained present after removal"
        diagnostic = _diagnostic(completed)
        lowered = diagnostic.lower()
        if "no such container" in lowered or "no such object" in lowered:
            return True, None
        return None, f"cannot verify container absence: {diagnostic}"

    def _control(
        self,
        argv: Sequence[str],
        *,
        unavailable_context: str,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = self._runner.run(argv, timeout=_CONTROL_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxUnavailableError(f"{unavailable_context}: {error}") from error
        if completed.returncode != 0:
            raise SandboxUnavailableError(f"{unavailable_context}: {_diagnostic(completed)}")
        return completed


def _json_object(data: bytes, *, context: str) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SandboxUnavailableError(f"{context} did not return valid JSON") from error
    if not isinstance(value, dict):
        raise SandboxUnavailableError(f"{context} did not return a JSON object")
    return cast(dict[str, object], value)


def _diagnostic(completed: subprocess.CompletedProcess[bytes]) -> str:
    raw = completed.stderr or completed.stdout
    rendered = raw[:_MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace").strip()
    return rendered or f"Docker command exited {completed.returncode}"


def _random_container_name_suffix() -> str:
    return secrets.token_hex(6)


def _container_name(execution_id: str, suffix: str) -> str:
    return f"guildmind-{execution_id}-{suffix}"


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _no_kill_evidence() -> DockerKillEvidence:
    return DockerKillEvidence(attempted=False, succeeded=None)


def _no_cleanup_evidence() -> DockerCleanupEvidence:
    return DockerCleanupEvidence(
        target=None,
        removal_attempted=False,
        removal_succeeded=None,
        absence_checked=False,
        absent=None,
    )


def _observed_without_container(
    result: SandboxResult,
    *,
    image_id: str | None,
) -> ObservedSandboxRun:
    return ObservedSandboxRun(
        result=result,
        evidence=DockerExecutionEvidence(
            container_id=None,
            image_id=image_id,
            state=None,
            termination_trigger=None,
            kill=_no_kill_evidence(),
            cleanup=_no_cleanup_evidence(),
            pre_cleanup_status=result.status,
        ),
    )


def _pre_cleanup_error(
    result: SandboxResult,
    *,
    capture: _CaptureResult | None = None,
) -> _PreCleanupRun:
    return _PreCleanupRun(
        result=result,
        state=None,
        termination_trigger=None if capture is None else capture.termination,
        kill=_no_kill_evidence() if capture is None else capture.kill,
    )


def _infrastructure_result(
    request: SandboxRequest,
    *,
    diagnostic: str,
    container_id: str | None = None,
    image_id: str | None = None,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> SandboxResult:
    return SandboxResult(
        execution_id=request.execution_id,
        status=SandboxStatus.INFRASTRUCTURE_ERROR,
        exit_code=None,
        stdout=stdout,
        stderr=stderr,
        container_id=container_id,
        image_id=image_id,
        diagnostic=diagnostic,
    )
