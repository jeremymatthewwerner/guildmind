"""Versioned active evidence for Docker resource-limit enforcement.

The probe deliberately keeps three claims separate: Docker accepted a configured
limit, an active workload observed enforcement, and the host is eligible to provide
reference evidence. A development daemon can establish the first two without ever
being promoted to the third.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from guildmind.domain import canonical_json, canonical_sha256, sha256_bytes
from guildmind.sandbox.base import SandboxLimits, SandboxRequest, SandboxStatus
from guildmind.sandbox.docker import (
    DockerCleanupEvidence,
    DockerExecutionEvidence,
    DockerHostAssessment,
    DockerHostMode,
    DockerSandbox,
    ObservedSandboxRun,
)

_SCHEMA_VERSION: Literal["guildmind.resource-probe-evidence/v1"] = (
    "guildmind.resource-probe-evidence/v1"
)
_PROFILE_VERSION: Literal["guildmind.candidate-resources/v1"] = "guildmind.candidate-resources/v1"
_PROGRAM = "/opt/guildmind/resource_probe.py"
_PYTHON = "/usr/local/bin/python"
_EXPECTED_PROGRAM_SHA256 = "6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7"
_LINUX_EAGAIN = 11
_LINUX_ENOSPC = 28
_PID_FORK_ATTEMPTS = 96
_CLEAN_CODE_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HASH = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
_NONEMPTY = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
_NONNEGATIVE = Annotated[int, Field(strict=True, ge=0)]
_POSITIVE = Annotated[int, Field(strict=True, gt=0)]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


_UTC_DATETIME = Annotated[datetime, AfterValidator(_aware_utc)]


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class EvidenceTier(StrEnum):
    DEVELOPMENT = "development"
    REFERENCE = "reference"


class ConfigurationVerdict(StrEnum):
    MATCHED = "matched"
    MISMATCHED = "mismatched"
    UNAVAILABLE = "unavailable"


class ProbeExecutionStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"


class ProbeErrorStage(StrEnum):
    DISPATCH = "dispatch"
    OBSERVATION = "observation"
    CLEANUP = "cleanup"


class EnforcementVerdict(StrEnum):
    ENFORCED = "enforced"
    NOT_ENFORCED = "not_enforced"
    INCONCLUSIVE = "inconclusive"


class ResourceProbeKind(StrEnum):
    MEMORY = "memory"
    PIDS = "pids"
    DISK = "disk"


class SandboxLimitsEvidence(_EvidenceModel):
    cpu_cores: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    memory_bytes: _POSITIVE
    pids: _POSITIVE
    workspace_bytes: _POSITIVE
    temporary_bytes: _POSITIVE
    output_bytes: _POSITIVE
    wall_time_seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class DockerHostEvidence(_EvidenceModel):
    tier: EvidenceTier
    host_policy: DockerHostMode
    accepted: bool
    reference_ready: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    image_reference: _NONEMPTY
    image_id: Annotated[
        str,
        StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
    ]

    @model_validator(mode="after")
    def _tier_never_promotes_development(self) -> Self:
        if not self.accepted:
            raise ValueError("resource evidence requires an accepted Docker host")
        if self.tier is EvidenceTier.REFERENCE:
            if self.host_policy is not DockerHostMode.REFERENCE:
                raise ValueError("reference evidence requires the reference host policy")
            if not self.reference_ready or self.failures:
                raise ValueError("reference evidence requires a fully ready host")
        elif self.host_policy is not DockerHostMode.DEVELOPMENT_ONLY:
            raise ValueError("development evidence requires the development-only host policy")
        return self


class NamedCheck(_EvidenceModel):
    name: _NONEMPTY
    passed: bool
    expected: bool | int | float | str | None
    observed: bool | int | float | str | None
    source: _NONEMPTY


class DockerStateEvidence(_EvidenceModel):
    exit_code: int
    oom_killed: bool
    running: bool
    error: str


class DockerKillRecord(_EvidenceModel):
    attempted: bool
    succeeded: bool | None
    diagnostic: str | None


class DockerCleanupRecord(_EvidenceModel):
    target: str | None
    removal_attempted: bool
    removal_succeeded: bool | None
    absence_checked: bool
    absent: bool | None
    diagnostic: str | None

    @property
    def confirmed(self) -> bool:
        return self.removal_succeeded is True and self.absent is True


class TranscriptEvidence(_EvidenceModel):
    stdout_sha256: _HASH
    stdout_bytes: _NONNEGATIVE
    stderr_sha256: _HASH
    stderr_bytes: _NONNEGATIVE


class ProbeExecutionEvidence(_EvidenceModel):
    status: ProbeExecutionStatus
    error_stage: ProbeErrorStage | None
    sandbox_status: SandboxStatus
    pre_cleanup_status: SandboxStatus
    exit_code: int | None
    output_truncated: bool
    state: DockerStateEvidence | None
    termination_trigger: SandboxStatus | None
    kill: DockerKillRecord
    cleanup: DockerCleanupRecord
    transcript: TranscriptEvidence
    diagnostic: str | None

    @model_validator(mode="after")
    def _error_stage_matches_status(self) -> Self:
        if (self.status is ProbeExecutionStatus.ERROR) != (self.error_stage is not None):
            raise ValueError("probe execution error status and stage must agree")
        if self.status is ProbeExecutionStatus.COMPLETED and not self.cleanup.confirmed:
            raise ValueError("completed probe execution requires verified cleanup")
        return self


class LimitsObservation(_EvidenceModel):
    schema_version: Literal["guildmind.resource-probe/v1"]
    probe: Literal["limits"]
    program_sha256: _HASH
    cpu_max: str | None
    memory_max: str | None
    memory_swap_max: str | None
    pids_current: _NONNEGATIVE | None
    pids_max: str | None
    pids_events: dict[str, _NONNEGATIVE]
    pids_events_local: dict[str, _NONNEGATIVE] | None

    @model_validator(mode="after")
    def _required_pid_event_is_present(self) -> Self:
        if "max" not in self.pids_events:
            raise ValueError("pids_events must contain the max counter")
        if self.pids_events_local is not None and "max" not in self.pids_events_local:
            raise ValueError("available pids_events_local must contain the max counter")
        return self


class PidsObservation(_EvidenceModel):
    schema_version: Literal["guildmind.resource-probe/v1"]
    probe: Literal["pids"]
    baseline_current: _NONNEGATIVE
    pids_max: str
    pressure_current: _NONNEGATIVE
    pids_peak: _NONNEGATIVE | None
    events_before: dict[str, _NONNEGATIVE]
    events_after: dict[str, _NONNEGATIVE]
    events_local_before: dict[str, _NONNEGATIVE] | None
    events_local_after: dict[str, _NONNEGATIVE] | None
    fork_attempts: _POSITIVE
    fork_errno: _NONNEGATIVE | None
    fork_error: str | None
    children_started: _NONNEGATIVE
    final_current: _NONNEGATIVE

    @model_validator(mode="after")
    def _pid_limit_is_a_positive_integer(self) -> Self:
        if not self.pids_max.isascii() or not self.pids_max.isdecimal():
            raise ValueError("pids_max must be a positive decimal integer")
        if int(self.pids_max) <= 0:
            raise ValueError("pids_max must be positive")
        if "max" not in self.events_before or "max" not in self.events_after:
            raise ValueError("PID events must contain the max counter")
        for events in (self.events_local_before, self.events_local_after):
            if events is not None and "max" not in events:
                raise ValueError("available local PID events must contain the max counter")
        return self

    @property
    def pids_max_value(self) -> int:
        return int(self.pids_max)


class DiskMountObservation(_EvidenceModel):
    path: Literal["/workspace", "/tmp"]
    configured_bytes: _POSITIVE
    stat_total_before: _POSITIVE
    stat_free_before: _NONNEGATIVE
    bytes_written: _NONNEGATIVE
    failure_errno: _NONNEGATIVE | None
    stat_free_at_failure: _NONNEGATIVE
    stat_free_after_cleanup: _NONNEGATIVE


class DiskObservation(_EvidenceModel):
    schema_version: Literal["guildmind.resource-probe/v1"]
    probe: Literal["disk"]
    mounts: tuple[DiskMountObservation, ...]

    @model_validator(mode="after")
    def _both_writable_mounts_are_unique(self) -> Self:
        paths = tuple(item.path for item in self.mounts)
        if paths != ("/workspace", "/tmp"):
            raise ValueError("disk observation must contain workspace then temporary mount")
        return self


class ConfigurationEvidence(_EvidenceModel):
    verdict: ConfigurationVerdict
    checks: tuple[NamedCheck, ...]
    observation: LimitsObservation | None
    execution: ProbeExecutionEvidence

    @model_validator(mode="after")
    def _verdict_matches_checks(self) -> Self:
        if self.verdict is ConfigurationVerdict.MATCHED:
            if (
                self.observation is None
                or not self.checks
                or not all(check.passed for check in self.checks)
            ):
                raise ValueError("matched configuration requires passing observed checks")
        elif self.verdict is ConfigurationVerdict.MISMATCHED:
            if self.observation is None or not any(not check.passed for check in self.checks):
                raise ValueError("mismatched configuration requires a failed observed check")
        elif self.observation is not None:
            raise ValueError("unavailable configuration cannot contain an observation")
        return self


class ResourceProbeEvidence(_EvidenceModel):
    resource: ResourceProbeKind
    execution: ProbeExecutionEvidence
    verdict: EnforcementVerdict
    checks: tuple[NamedCheck, ...]
    observation: PidsObservation | DiskObservation | None
    diagnostic: str | None

    @model_validator(mode="after")
    def _verdict_is_supported(self) -> Self:
        expected_probe = self.resource.value
        if self.observation is not None and self.observation.probe != expected_probe:
            raise ValueError("resource and observation kind do not match")
        if self.verdict is not EnforcementVerdict.INCONCLUSIVE:
            if self.execution.status is not ProbeExecutionStatus.COMPLETED:
                raise ValueError("conclusive enforcement requires completed execution evidence")
            if not self.checks:
                raise ValueError("conclusive enforcement requires named checks")
            passed = all(check.passed for check in self.checks)
            if (self.verdict is EnforcementVerdict.ENFORCED) != passed:
                raise ValueError("enforcement verdict and named checks do not agree")
        return self


class ResourceProbeSuiteEvidence(_EvidenceModel):
    schema_version: Literal["guildmind.resource-probe-evidence/v1"] = _SCHEMA_VERSION
    probe_id: _NONEMPTY
    profile_version: Literal["guildmind.candidate-resources/v1"] = _PROFILE_VERSION
    probe_spec_sha256: _HASH
    code_revision: _NONEMPTY
    occurred_at: _UTC_DATETIME
    environment: DockerHostEvidence
    requested_limits: SandboxLimitsEvidence
    configuration: ConfigurationEvidence
    probes: tuple[ResourceProbeEvidence, ...]
    all_enforced: bool
    reference_eligible: bool
    reference_passed: bool

    @model_validator(mode="after")
    def _suite_claims_are_derived(self) -> Self:
        if tuple(probe.resource for probe in self.probes) != (
            ResourceProbeKind.MEMORY,
            ResourceProbeKind.PIDS,
            ResourceProbeKind.DISK,
        ):
            raise ValueError("resource probes must use the fixed profile order")
        all_enforced = self.configuration.verdict is ConfigurationVerdict.MATCHED and all(
            probe.verdict is EnforcementVerdict.ENFORCED for probe in self.probes
        )
        reference_eligible = (
            self.environment.tier is EvidenceTier.REFERENCE
            and self.environment.accepted
            and self.environment.reference_ready
            and self.environment.host_policy is DockerHostMode.REFERENCE
            and _CLEAN_CODE_REVISION.fullmatch(self.code_revision) is not None
        )
        if self.all_enforced != all_enforced:
            raise ValueError("all_enforced must be derived from configuration and probe verdicts")
        if self.reference_eligible != reference_eligible:
            raise ValueError("reference_eligible must be derived from host evidence")
        if self.reference_passed != (all_enforced and reference_eligible):
            raise ValueError("reference_passed must require enforcement and reference eligibility")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())


def candidate_resource_limits() -> SandboxLimits:
    """Return the fixed resource profile currently used by both evaluator phases."""
    return SandboxLimits(
        cpu_cores=1.0,
        memory_bytes=268_435_456,
        pids=64,
        workspace_bytes=67_108_864,
        temporary_bytes=16_777_216,
        output_bytes=16_384,
        wall_time_seconds=5.0,
    )


def resource_probe_spec_sha256() -> str:
    """Bind evidence to the fixed command set and requested candidate profile."""
    limits = candidate_resource_limits()
    return canonical_sha256(
        {
            "commands": ["limits", "memory", "pids", "disk"],
            "profile_version": _PROFILE_VERSION,
            "program": _PROGRAM,
            "program_sha256": _EXPECTED_PROGRAM_SHA256,
            "requested_limits": _limits_dict(limits),
            "schema_version": _SCHEMA_VERSION,
        }
    )


def run_resource_probe_suite(
    sandbox: DockerSandbox,
    *,
    image: str,
    code_revision: str,
    probe_id: str,
    occurred_at: datetime | None = None,
) -> ResourceProbeSuiteEvidence:
    """Run the fixed resource suite and return canonical, fail-closed evidence."""
    assessment = sandbox.assess_host()
    assessment.require_accepted()
    image_id = sandbox.verify_image(image)
    environment = _environment_evidence(sandbox, assessment, image, image_id)
    limits = candidate_resource_limits()

    configuration_run = _run_probe(sandbox, image=image, limits=limits, command="limits")
    configuration = _configuration_evidence(configuration_run, limits=limits)
    probes = (
        _memory_evidence(
            _run_probe(sandbox, image=image, limits=limits, command="memory"),
            configuration=configuration,
        ),
        _pids_evidence(
            _run_probe(sandbox, image=image, limits=limits, command="pids"),
            configuration=configuration,
        ),
        _disk_evidence(
            _run_probe(sandbox, image=image, limits=limits, command="disk"),
            configuration=configuration,
            limits=limits,
        ),
    )
    all_enforced = configuration.verdict is ConfigurationVerdict.MATCHED and all(
        probe.verdict is EnforcementVerdict.ENFORCED for probe in probes
    )
    reference_eligible = (
        environment.tier is EvidenceTier.REFERENCE
        and environment.accepted
        and environment.reference_ready
        and environment.host_policy is DockerHostMode.REFERENCE
        and _CLEAN_CODE_REVISION.fullmatch(code_revision) is not None
    )
    return ResourceProbeSuiteEvidence(
        probe_id=probe_id,
        probe_spec_sha256=resource_probe_spec_sha256(),
        code_revision=code_revision,
        occurred_at=occurred_at or datetime.now(UTC),
        environment=environment,
        requested_limits=_limits_evidence(limits),
        configuration=configuration,
        probes=probes,
        all_enforced=all_enforced,
        reference_eligible=reference_eligible,
        reference_passed=all_enforced and reference_eligible,
    )


def _run_probe(
    sandbox: DockerSandbox,
    *,
    image: str,
    limits: SandboxLimits,
    command: str,
) -> ObservedSandboxRun:
    return sandbox.run_observed(
        SandboxRequest(
            execution_id=f"resource-{command}",
            image=image,
            argv=(_PYTHON, "-I", _PROGRAM, command),
            limits=limits,
        )
    )


def _environment_evidence(
    sandbox: DockerSandbox,
    assessment: DockerHostAssessment,
    image_reference: str,
    image_id: str,
) -> DockerHostEvidence:
    mode = sandbox.host_policy.mode
    tier = EvidenceTier.REFERENCE if mode is DockerHostMode.REFERENCE else EvidenceTier.DEVELOPMENT
    return DockerHostEvidence(
        tier=tier,
        host_policy=mode,
        accepted=assessment.accepted,
        reference_ready=assessment.reference_ready,
        failures=assessment.failures,
        warnings=assessment.warnings,
        image_reference=image_reference,
        image_id=image_id,
    )


def _configuration_evidence(
    run: ObservedSandboxRun,
    *,
    limits: SandboxLimits,
) -> ConfigurationEvidence:
    execution = _execution_evidence(run)
    if execution.status is not ProbeExecutionStatus.COMPLETED:
        return ConfigurationEvidence(
            verdict=ConfigurationVerdict.UNAVAILABLE,
            checks=(),
            observation=None,
            execution=execution,
        )
    try:
        observation = LimitsObservation.model_validate(_single_json_object(run.result.stdout))
    except (ValueError, json.JSONDecodeError) as error:
        return ConfigurationEvidence(
            verdict=ConfigurationVerdict.UNAVAILABLE,
            checks=(),
            observation=None,
            execution=_observation_error(execution, error),
        )
    checks = (
        _check(
            "probe_exited_cleanly",
            execution.pre_cleanup_status is SandboxStatus.EXITED and execution.exit_code == 0,
            0,
            execution.exit_code,
            "docker:.State.ExitCode",
        ),
        _check(
            "probe_program_identity",
            observation.program_sha256 == _EXPECTED_PROGRAM_SHA256,
            _EXPECTED_PROGRAM_SHA256,
            observation.program_sha256,
            "probe:sha256(__file__)",
        ),
        _check(
            "cpu_quota",
            _cpu_max_matches(observation.cpu_max, limits.cpu_cores),
            f"{limits.cpu_cores:g} cores",
            observation.cpu_max,
            "cgroup:cpu.max",
        ),
        _check(
            "memory_limit",
            observation.memory_max == str(limits.memory_bytes),
            limits.memory_bytes,
            observation.memory_max,
            "cgroup:memory.max",
        ),
        _check(
            "swap_disabled",
            observation.memory_swap_max == "0",
            0,
            observation.memory_swap_max,
            "cgroup:memory.swap.max",
        ),
        _check(
            "pid_limit",
            observation.pids_max == str(limits.pids),
            limits.pids,
            observation.pids_max,
            "cgroup:pids.max",
        ),
    )
    verdict = (
        ConfigurationVerdict.MATCHED
        if all(check.passed for check in checks)
        else ConfigurationVerdict.MISMATCHED
    )
    return ConfigurationEvidence(
        verdict=verdict,
        checks=checks,
        observation=observation,
        execution=execution,
    )


def _memory_evidence(
    run: ObservedSandboxRun,
    *,
    configuration: ConfigurationEvidence,
) -> ResourceProbeEvidence:
    execution = _execution_evidence(run)
    state = execution.state
    checks = (
        _check(
            "docker_oom_killed",
            state is not None and state.oom_killed,
            True,
            None if state is None else state.oom_killed,
            "docker:.State.OOMKilled",
        ),
        _check(
            "sandbox_oom_classification",
            execution.pre_cleanup_status is SandboxStatus.OOM_KILLED,
            SandboxStatus.OOM_KILLED.value,
            execution.pre_cleanup_status.value,
            "guildmind:DockerSandbox",
        ),
        _check(
            "exit_137",
            execution.exit_code == 137,
            137,
            execution.exit_code,
            "docker:.State.ExitCode",
        ),
    )
    if configuration.verdict is not ConfigurationVerdict.MATCHED:
        verdict = EnforcementVerdict.INCONCLUSIVE
        diagnostic = "requested memory configuration was not established"
    elif execution.status is not ProbeExecutionStatus.COMPLETED:
        verdict = EnforcementVerdict.INCONCLUSIVE
        diagnostic = execution.diagnostic or "memory probe execution was incomplete"
    elif all(check.passed for check in checks):
        verdict = EnforcementVerdict.ENFORCED
        diagnostic = None
    elif execution.pre_cleanup_status is SandboxStatus.EXITED and execution.exit_code == 0:
        verdict = EnforcementVerdict.NOT_ENFORCED
        diagnostic = "memory workload completed without an observed cgroup OOM kill"
    else:
        verdict = EnforcementVerdict.INCONCLUSIVE
        diagnostic = "memory workload ended without conclusive OOM enforcement evidence"
    return ResourceProbeEvidence(
        resource=ResourceProbeKind.MEMORY,
        execution=execution,
        verdict=verdict,
        checks=checks,
        observation=None,
        diagnostic=diagnostic,
    )


def _pids_evidence(
    run: ObservedSandboxRun,
    *,
    configuration: ConfigurationEvidence,
) -> ResourceProbeEvidence:
    execution = _execution_evidence(run)
    observation: PidsObservation | None = None
    parse_error: Exception | None = None
    if execution.status is ProbeExecutionStatus.COMPLETED:
        try:
            observation = PidsObservation.model_validate(_single_json_object(run.result.stdout))
        except (ValueError, json.JSONDecodeError) as error:
            parse_error = error
            execution = _observation_error(execution, error)
    if observation is None:
        return ResourceProbeEvidence(
            resource=ResourceProbeKind.PIDS,
            execution=execution,
            verdict=EnforcementVerdict.INCONCLUSIVE,
            checks=(),
            observation=None,
            diagnostic=str(parse_error or execution.diagnostic or "PID observation unavailable"),
        )
    event_delta = observation.events_after.get("max", 0) - observation.events_before.get("max", 0)
    integrity_checks: list[NamedCheck] = [
        _check(
            "probe_exited_cleanly",
            execution.pre_cleanup_status is SandboxStatus.EXITED and execution.exit_code == 0,
            0,
            execution.exit_code,
            "docker:.State.ExitCode",
        ),
        _check(
            "children_started",
            observation.children_started > 0,
            ">0",
            observation.children_started,
            "probe:os.fork",
        ),
        _check(
            "pid_event_counter_monotonic",
            event_delta >= 0,
            ">=0",
            event_delta,
            "cgroup:pids.events:max",
        ),
        _check(
            "children_reaped",
            observation.final_current == observation.baseline_current,
            observation.baseline_current,
            observation.final_current,
            "cgroup:pids.current",
        ),
    ]
    optional_enforcement_checks: list[NamedCheck] = []
    if observation.pids_peak is not None:
        optional_enforcement_checks.append(
            _check(
                "pid_peak_corroborates_ceiling",
                observation.pids_peak >= observation.pids_max_value,
                observation.pids_max_value,
                observation.pids_peak,
                "cgroup:pids.peak",
            )
        )
    local_before = observation.events_local_before
    local_after = observation.events_local_after
    if (local_before is None) != (local_after is None):
        integrity_checks.append(
            _check(
                "pid_local_observation_consistent",
                False,
                "both present or both absent",
                "partially available",
                "cgroup:pids.events.local",
            )
        )
    elif local_before is not None and local_after is not None:
        local_delta = local_after.get("max", 0) - local_before.get("max", 0)
        optional_enforcement_checks.append(
            _check(
                "pid_local_event_corroborates_limit",
                local_delta >= 1,
                ">=1",
                local_delta,
                "cgroup:pids.events.local:max",
            )
        )
    enforcement_checks = (
        _check(
            "fork_eagain",
            observation.fork_errno == _LINUX_EAGAIN,
            _LINUX_EAGAIN,
            observation.fork_errno,
            "probe:os.fork",
        ),
        _check(
            "pid_event_incremented",
            event_delta >= 1,
            ">=1",
            event_delta,
            "cgroup:pids.events:max",
        ),
        _check(
            "pid_ceiling_reached",
            observation.pressure_current == observation.pids_max_value,
            observation.pids_max_value,
            observation.pressure_current,
            "cgroup:pids.current",
        ),
        *optional_enforcement_checks,
    )
    frozen_integrity = tuple(integrity_checks)
    checks = (*frozen_integrity, *enforcement_checks)
    verdict, diagnostic = _active_verdict(
        configuration=configuration,
        execution=execution,
        integrity_checks=frozen_integrity,
        enforcement_checks=enforcement_checks,
        explicit_counterexample=(
            observation.fork_attempts == _PID_FORK_ATTEMPTS
            and observation.children_started == observation.fork_attempts
            and observation.fork_errno is None
            and event_delta == 0
            and observation.pressure_current > observation.pids_max_value
        ),
        failure="PID workload did not observe the configured aggregate process ceiling",
    )
    return ResourceProbeEvidence(
        resource=ResourceProbeKind.PIDS,
        execution=execution,
        verdict=verdict,
        checks=checks,
        observation=observation,
        diagnostic=diagnostic,
    )


def _disk_evidence(
    run: ObservedSandboxRun,
    *,
    configuration: ConfigurationEvidence,
    limits: SandboxLimits,
) -> ResourceProbeEvidence:
    execution = _execution_evidence(run)
    observation: DiskObservation | None = None
    parse_error: Exception | None = None
    if execution.status is ProbeExecutionStatus.COMPLETED:
        try:
            observation = DiskObservation.model_validate(_single_json_object(run.result.stdout))
        except (ValueError, json.JSONDecodeError) as error:
            parse_error = error
            execution = _observation_error(execution, error)
    if observation is None:
        return ResourceProbeEvidence(
            resource=ResourceProbeKind.DISK,
            execution=execution,
            verdict=EnforcementVerdict.INCONCLUSIVE,
            checks=(),
            observation=None,
            diagnostic=str(parse_error or execution.diagnostic or "disk observation unavailable"),
        )
    expected = {"/workspace": limits.workspace_bytes, "/tmp": limits.temporary_bytes}
    integrity_checks: list[NamedCheck] = []
    integrity_checks.append(
        _check(
            "probe_exited_cleanly",
            execution.pre_cleanup_status is SandboxStatus.EXITED and execution.exit_code == 0,
            0,
            execution.exit_code,
            "docker:.State.ExitCode",
        )
    )
    enforcement_checks: list[NamedCheck] = []
    for mount in observation.mounts:
        configured = expected[mount.path]
        integrity_checks.extend(
            (
                _check(
                    f"{mount.path}_configured_size",
                    mount.configured_bytes == configured and mount.stat_total_before == configured,
                    configured,
                    mount.stat_total_before,
                    f"statvfs:{mount.path}",
                ),
                _check(
                    f"{mount.path}_initial_space_consistent",
                    mount.stat_free_before <= mount.stat_total_before,
                    f"<= {mount.stat_total_before}",
                    mount.stat_free_before,
                    f"statvfs:{mount.path}",
                ),
                _check(
                    f"{mount.path}_space_recovered",
                    mount.stat_free_after_cleanup == mount.stat_free_before,
                    mount.stat_free_before,
                    mount.stat_free_after_cleanup,
                    f"statvfs:{mount.path}",
                ),
            )
        )
        enforcement_checks.extend(
            (
                _check(
                    f"{mount.path}_enospc",
                    mount.failure_errno == _LINUX_ENOSPC,
                    _LINUX_ENOSPC,
                    mount.failure_errno,
                    f"probe:write:{mount.path}",
                ),
                _check(
                    f"{mount.path}_hard_byte_ceiling",
                    mount.bytes_written == configured and mount.stat_free_at_failure == 0,
                    configured,
                    mount.bytes_written,
                    f"probe+statvfs:{mount.path}",
                ),
            )
        )
    frozen_integrity = tuple(integrity_checks)
    frozen_enforcement = tuple(enforcement_checks)
    frozen_checks = (*frozen_integrity, *frozen_enforcement)
    verdict, diagnostic = _active_verdict(
        configuration=configuration,
        execution=execution,
        integrity_checks=frozen_integrity,
        enforcement_checks=frozen_enforcement,
        explicit_counterexample=any(
            mount.failure_errno is None and mount.bytes_written > expected[mount.path]
            for mount in observation.mounts
        ),
        failure="writable mount workload did not observe exact ENOSPC enforcement",
    )
    return ResourceProbeEvidence(
        resource=ResourceProbeKind.DISK,
        execution=execution,
        verdict=verdict,
        checks=frozen_checks,
        observation=observation,
        diagnostic=diagnostic,
    )


def _active_verdict(
    *,
    configuration: ConfigurationEvidence,
    execution: ProbeExecutionEvidence,
    integrity_checks: tuple[NamedCheck, ...],
    enforcement_checks: tuple[NamedCheck, ...],
    explicit_counterexample: bool,
    failure: str,
) -> tuple[EnforcementVerdict, str | None]:
    if configuration.verdict is not ConfigurationVerdict.MATCHED:
        return (
            EnforcementVerdict.INCONCLUSIVE,
            "requested resource configuration was not established",
        )
    if execution.status is not ProbeExecutionStatus.COMPLETED:
        return EnforcementVerdict.INCONCLUSIVE, execution.diagnostic or "probe execution incomplete"
    failed_integrity = [check.name for check in integrity_checks if not check.passed]
    if failed_integrity:
        return (
            EnforcementVerdict.INCONCLUSIVE,
            f"probe integrity checks failed: {', '.join(failed_integrity)}",
        )
    if all(check.passed for check in enforcement_checks):
        return EnforcementVerdict.ENFORCED, None
    if explicit_counterexample:
        return EnforcementVerdict.NOT_ENFORCED, failure
    return (
        EnforcementVerdict.INCONCLUSIVE,
        "active observations were neither complete enforcement nor an explicit counterexample",
    )


def _execution_evidence(run: ObservedSandboxRun) -> ProbeExecutionEvidence:
    result = run.result
    observed = run.evidence
    error_stage: ProbeErrorStage | None = None
    if not observed.cleanup.confirmed:
        error_stage = ProbeErrorStage.CLEANUP
    elif result.status is SandboxStatus.INFRASTRUCTURE_ERROR:
        error_stage = ProbeErrorStage.DISPATCH
    status = (
        ProbeExecutionStatus.ERROR if error_stage is not None else ProbeExecutionStatus.COMPLETED
    )
    state = observed.state
    return ProbeExecutionEvidence(
        status=status,
        error_stage=error_stage,
        sandbox_status=result.status,
        pre_cleanup_status=observed.pre_cleanup_status,
        exit_code=result.exit_code if result.exit_code is not None else _state_exit_code(observed),
        output_truncated=result.output_truncated,
        state=(
            None
            if state is None
            else DockerStateEvidence(
                exit_code=state.exit_code,
                oom_killed=state.oom_killed,
                running=state.running,
                error=state.error,
            )
        ),
        termination_trigger=observed.termination_trigger,
        kill=DockerKillRecord(
            attempted=observed.kill.attempted,
            succeeded=observed.kill.succeeded,
            diagnostic=observed.kill.diagnostic,
        ),
        cleanup=_cleanup_record(observed.cleanup),
        transcript=TranscriptEvidence(
            stdout_sha256=sha256_bytes(result.stdout),
            stdout_bytes=len(result.stdout),
            stderr_sha256=sha256_bytes(result.stderr),
            stderr_bytes=len(result.stderr),
        ),
        diagnostic=result.diagnostic,
    )


def _observation_error(
    execution: ProbeExecutionEvidence,
    error: Exception,
) -> ProbeExecutionEvidence:
    return execution.model_copy(
        update={
            "status": ProbeExecutionStatus.ERROR,
            "error_stage": ProbeErrorStage.OBSERVATION,
            "diagnostic": f"malformed probe observation: {error}",
        }
    )


def _cleanup_record(value: DockerCleanupEvidence) -> DockerCleanupRecord:
    return DockerCleanupRecord(
        target=value.target,
        removal_attempted=value.removal_attempted,
        removal_succeeded=value.removal_succeeded,
        absence_checked=value.absence_checked,
        absent=value.absent,
        diagnostic=value.diagnostic,
    )


def _state_exit_code(value: DockerExecutionEvidence) -> int | None:
    return None if value.state is None else value.state.exit_code


def _single_json_object(data: bytes) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("probe output is not UTF-8") from error
    if not text.endswith("\n") or text.count("\n") != 1:
        raise ValueError("probe output must be exactly one LF-terminated JSON record")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    value = json.loads(text, object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise ValueError("probe output must be a JSON object")
    return cast(dict[str, object], value)


def _check(
    name: str,
    passed: bool,
    expected: bool | int | float | str | None,
    observed: bool | int | float | str | None,
    source: str,
) -> NamedCheck:
    return NamedCheck(
        name=name,
        passed=passed,
        expected=expected,
        observed=observed,
        source=source,
    )


def _cpu_max_matches(value: str | None, cpu_cores: float) -> bool:
    if value is None:
        return False
    parts = value.split()
    if len(parts) != 2 or parts[0] == "max":
        return False
    try:
        quota, period = (int(part) for part in parts)
    except ValueError:
        return False
    return quota > 0 and period > 0 and abs(quota / period - cpu_cores) < 1e-12


def _limits_dict(limits: SandboxLimits) -> dict[str, int | float]:
    return {
        "cpu_cores": limits.cpu_cores,
        "memory_bytes": limits.memory_bytes,
        "pids": limits.pids,
        "workspace_bytes": limits.workspace_bytes,
        "temporary_bytes": limits.temporary_bytes,
        "output_bytes": limits.output_bytes,
        "wall_time_seconds": limits.wall_time_seconds,
    }


def _limits_evidence(limits: SandboxLimits) -> SandboxLimitsEvidence:
    return SandboxLimitsEvidence(
        cpu_cores=limits.cpu_cores,
        memory_bytes=limits.memory_bytes,
        pids=limits.pids,
        workspace_bytes=limits.workspace_bytes,
        temporary_bytes=limits.temporary_bytes,
        output_bytes=limits.output_bytes,
        wall_time_seconds=limits.wall_time_seconds,
    )


def limits_mapping(limits: SandboxLimitsEvidence) -> Mapping[str, int | float]:
    """Return a stable plain mapping for evidence consumers that do not use Pydantic."""
    return cast(Mapping[str, int | float], limits.model_dump(mode="python"))
