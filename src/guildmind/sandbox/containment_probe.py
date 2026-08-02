"""Typed active evidence for the evaluator container containment boundary.

The image-owned probe reports only bounded inventories and hashes.  The host owns
all expectations, plants fresh high-entropy canaries, and derives every verdict.
Development evidence can exercise the same controls as reference evidence, but it
can never be promoted to a reference pass.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
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
from guildmind.sandbox.base import SandboxLimits, SandboxMount, SandboxRequest, SandboxStatus
from guildmind.sandbox.docker import (
    DockerHostAssessment,
    DockerHostMode,
    DockerSandbox,
    ObservedSandboxRun,
)
from guildmind.sandbox.resource_probe import candidate_resource_limits

_EVIDENCE_SCHEMA: Literal["guildmind.containment-probe-evidence/v1"] = (
    "guildmind.containment-probe-evidence/v1"
)
_OBSERVATION_SCHEMA: Literal["guildmind.containment-probe/v1"] = "guildmind.containment-probe/v1"
_PROFILE_VERSION: Literal["guildmind.evaluator-containment/v1"] = (
    "guildmind.evaluator-containment/v1"
)
_PROGRAM = "/opt/guildmind/containment_probe.py"
_PYTHON = "/usr/local/bin/python"
_EXPECTED_PROGRAM_SHA256 = "97004c3494e8a1f1336c0c5034988f0fb11862d192021194e10de060885b858a"
_SENTINEL_PREFIX = "guildmind-containment-v1:"
_HOST_ENV_SENTINEL = "GUILDMIND_CONTAINMENT_SENTINEL"
_CLEAN_CODE_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_HASH = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
_NONEMPTY = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]
_NONNEGATIVE = Annotated[int, Field(strict=True, ge=0)]

_MOUNT_ORDER = ("workspace", "challenge", "grader", "response")
_DNS_ORDER = (
    "example.com",
    "pypi.org",
    "host.docker.internal",
    "gateway.docker.internal",
    "kubernetes.default.svc",
)
_TCP_ORDER = (
    "external_ipv4",
    "external_ipv6",
    "metadata_aws",
    "metadata_ecs",
    "metadata_alibaba",
    "host_docker_desktop",
    "host_default_bridge",
    "loopback_ssh",
    "loopback_http",
    "loopback_docker",
    "loopback_docker_tls",
)
_UNIX_ORDER = (
    "docker_var_run",
    "docker_run",
    "containerd",
    "podman",
    "crio",
    "ssh_auth_sock",
)
_SOCKET_ROOT_ORDER = ("run", "var_run", "tmp", "workspace", "inputs")
_CREDENTIAL_ORDER = (
    "run_secrets",
    "kubernetes_serviceaccount",
    "root_aws",
    "root_gcloud",
    "root_azure",
    "home_nonroot_aws",
    "home_nonroot_gcloud",
    "home_nonroot_azure",
)
_NONLOOPBACK_BLOCK_ERRNOS = frozenset({101, 113})  # ENETUNREACH, EHOSTUNREACH
_LOOPBACK_CLOSED_ERRNOS = frozenset({111})  # ECONNREFUSED
_WRITE_DENIED_ERRNOS = frozenset({1, 13, 30})  # EPERM, EACCES, EROFS
_PRIVILEGED_SOCKET_DENIED_ERRNOS = frozenset({1, 13})  # EPERM, EACCES
_ENVIRONMENT_LOCK = threading.Lock()

# The base image is digest pinned. These values are public image configuration, not
# host state. Docker supplies HOME and the fixed request hostname at execution time.
_BASE_ENVIRONMENT: Mapping[str, str] = {
    "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",
    "HOME": "/",
    "HOSTNAME": "guildmind",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "PYTHON_SHA256": "c08bc65a81971c1dd5783182826503369466c7e67374d1646519adf05207b684",
    "PYTHON_VERSION": "3.12.13",
    "TZ": "UTC",
}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


_UTC_DATETIME = Annotated[datetime, AfterValidator(_aware_utc)]


class _EvidenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ContainmentProfile(StrEnum):
    CANDIDATE = "candidate"
    SCORER = "scorer"


# Phase is the public evaluator term; Profile remains a compatibility-friendly
# description of the production-shaped mount/environment record.
ContainmentPhase = ContainmentProfile


class EvidenceTier(StrEnum):
    DEVELOPMENT = "development"
    REFERENCE = "reference"


class ContainmentVerdict(StrEnum):
    CONTAINED = "contained"
    EXPOSED = "exposed"
    INCONCLUSIVE = "inconclusive"


class CheckOutcome(StrEnum):
    PASS = "pass"
    EXPOSURE = "exposure"
    INCONCLUSIVE = "inconclusive"


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    ERROR = "error"


class ExecutionErrorStage(StrEnum):
    DISPATCH = "dispatch"
    OBSERVATION = "observation"
    CLEANUP = "cleanup"


class SentinelDisposition(StrEnum):
    VISIBLE = "visible"
    FORBIDDEN = "forbidden"


class SentinelChannel(StrEnum):
    FILE = "file"
    ENVIRONMENT = "environment"


class MountTarget(StrEnum):
    WORKSPACE = "workspace"
    CHALLENGE = "challenge"
    GRADER = "grader"
    RESPONSE = "response"


class DnsOutcome(StrEnum):
    RESOLVED = "resolved"
    GAI_ERROR = "gai_error"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"


class ConnectOutcome(StrEnum):
    CONNECTED = "connected"
    OS_ERROR = "os_error"
    TIMEOUT = "timeout"


class UnixOutcome(StrEnum):
    ABSENT = "absent"
    CONNECTED = "connected"
    REFUSED = "refused"
    TIMEOUT = "timeout"
    OS_ERROR = "os_error"
    NOT_SOCKET = "not_socket"


class WriteOutcome(StrEnum):
    ABSENT = "absent"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    OS_ERROR = "os_error"


class PrivilegedSocketOutcome(StrEnum):
    CREATED = "created"
    OS_ERROR = "os_error"


class CredentialOutcome(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"
    INACCESSIBLE = "inaccessible"
    OS_ERROR = "os_error"


class DockerHostEvidence(_EvidenceModel):
    tier: EvidenceTier
    host_policy: DockerHostMode
    accepted: bool
    reference_ready: bool
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    image_reference: _NONEMPTY
    image_id: Annotated[str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def _tier_matches_policy(self) -> Self:
        if not self.accepted:
            raise ValueError("containment evidence requires an accepted Docker host")
        if self.tier is EvidenceTier.REFERENCE:
            if self.host_policy is not DockerHostMode.REFERENCE:
                raise ValueError("reference evidence requires the reference host policy")
            if not self.reference_ready or self.failures:
                raise ValueError("reference evidence requires a fully ready host")
        elif self.host_policy is not DockerHostMode.DEVELOPMENT_ONLY:
            raise ValueError("development evidence requires the development-only host policy")
        return self


class SandboxLimitsEvidence(_EvidenceModel):
    cpu_cores: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]
    memory_bytes: Annotated[int, Field(strict=True, gt=0)]
    pids: Annotated[int, Field(strict=True, gt=0)]
    workspace_bytes: Annotated[int, Field(strict=True, gt=0)]
    temporary_bytes: Annotated[int, Field(strict=True, gt=0)]
    output_bytes: Annotated[int, Field(strict=True, gt=0)]
    wall_time_seconds: Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class EnvironmentValue(_EvidenceModel):
    name: _NONEMPTY
    value_sha256: _HASH

    @model_validator(mode="after")
    def _safe_name(self) -> Self:
        if _ENVIRONMENT_NAME.fullmatch(self.name) is None:
            raise ValueError("environment name is invalid")
        return self


class SentinelInventory(_EvidenceModel):
    file_sha256: tuple[_HASH, ...]
    environment: tuple[EnvironmentValue, ...]
    files_examined: _NONNEGATIVE
    scan_errors: _NONNEGATIVE
    scan_truncated: bool

    @model_validator(mode="after")
    def _inventory_is_canonical(self) -> Self:
        if self.file_sha256 != tuple(sorted(set(self.file_sha256))):
            raise ValueError("file sentinel hashes must be sorted and unique")
        names = tuple(item.name for item in self.environment)
        if names != tuple(sorted(set(names))):
            raise ValueError("environment sentinels must be sorted by unique name")
        return self


class MountObservation(_EvidenceModel):
    target: MountTarget
    present: bool
    read_only: bool | None
    write_outcome: WriteOutcome
    write_errno: int | None

    @model_validator(mode="after")
    def _fields_are_coherent(self) -> Self:
        if not self.present:
            if self.read_only is not None or self.write_outcome is not WriteOutcome.ABSENT:
                raise ValueError("absent mount cannot have access observations")
            if self.write_errno is not None:
                raise ValueError("absent mount cannot have a write errno")
        else:
            if self.read_only is None or self.write_outcome is WriteOutcome.ABSENT:
                raise ValueError("present mount requires mount and write observations")
            if self.write_outcome in {WriteOutcome.DENIED, WriteOutcome.OS_ERROR}:
                if self.write_errno is None:
                    raise ValueError("failed write attempt requires errno")
            elif self.write_errno is not None:
                raise ValueError("successful write attempt cannot have errno")
        return self


class DnsObservation(_EvidenceModel):
    target: Literal[
        "example.com",
        "pypi.org",
        "host.docker.internal",
        "gateway.docker.internal",
        "kubernetes.default.svc",
    ]
    outcome: DnsOutcome
    error_code: int | None

    @model_validator(mode="after")
    def _error_matches_outcome(self) -> Self:
        needs_error = self.outcome in {DnsOutcome.GAI_ERROR, DnsOutcome.OS_ERROR}
        if needs_error != (self.error_code is not None):
            raise ValueError("DNS error outcome and code must agree")
        return self


class TcpObservation(_EvidenceModel):
    target: Literal[
        "external_ipv4",
        "external_ipv6",
        "metadata_aws",
        "metadata_ecs",
        "metadata_alibaba",
        "host_docker_desktop",
        "host_default_bridge",
        "loopback_ssh",
        "loopback_http",
        "loopback_docker",
        "loopback_docker_tls",
    ]
    outcome: ConnectOutcome
    errno: int | None

    @model_validator(mode="after")
    def _errno_matches_outcome(self) -> Self:
        if (self.outcome is ConnectOutcome.OS_ERROR) != (self.errno is not None):
            raise ValueError("TCP OS error outcome and errno must agree")
        return self


class UnixObservation(_EvidenceModel):
    target: Literal[
        "docker_var_run",
        "docker_run",
        "containerd",
        "podman",
        "crio",
        "ssh_auth_sock",
    ]
    present: bool
    is_socket: bool
    outcome: UnixOutcome
    errno: int | None

    @model_validator(mode="after")
    def _fields_are_coherent(self) -> Self:
        if not self.present:
            if self.is_socket or self.outcome is not UnixOutcome.ABSENT or self.errno is not None:
                raise ValueError("absent Unix target has contradictory observations")
        elif not self.is_socket:
            if self.outcome is not UnixOutcome.NOT_SOCKET or self.errno is not None:
                raise ValueError("non-socket Unix target has contradictory observations")
        else:
            if self.outcome in {UnixOutcome.ABSENT, UnixOutcome.NOT_SOCKET}:
                raise ValueError("present Unix socket requires a connection outcome")
            if self.outcome is UnixOutcome.OS_ERROR:
                if self.errno is None:
                    raise ValueError("Unix OS error requires errno")
            elif self.errno is not None:
                raise ValueError("only Unix OS errors carry errno")
        return self


class SocketInventoryObservation(_EvidenceModel):
    root: Literal["run", "var_run", "tmp", "workspace", "inputs"]
    socket_count: _NONNEGATIVE
    scan_error: bool


class PrivilegedSocketObservation(_EvidenceModel):
    outcome: PrivilegedSocketOutcome
    errno: int | None

    @model_validator(mode="after")
    def _errno_matches_outcome(self) -> Self:
        if (self.outcome is PrivilegedSocketOutcome.OS_ERROR) != (self.errno is not None):
            raise ValueError("privileged socket outcome and errno must agree")
        return self


class CredentialObservation(_EvidenceModel):
    target: Literal[
        "run_secrets",
        "kubernetes_serviceaccount",
        "root_aws",
        "root_gcloud",
        "root_azure",
        "home_nonroot_aws",
        "home_nonroot_gcloud",
        "home_nonroot_azure",
    ]
    outcome: CredentialOutcome
    errno: int | None
    readable: bool

    @model_validator(mode="after")
    def _fields_are_coherent(self) -> Self:
        needs_errno = self.outcome in {
            CredentialOutcome.INACCESSIBLE,
            CredentialOutcome.OS_ERROR,
        }
        if needs_errno != (self.errno is not None):
            raise ValueError("credential outcome and errno must agree")
        if self.readable and self.outcome is not CredentialOutcome.PRESENT:
            raise ValueError("only a present credential path can be readable")
        return self


class NetworkObservation(_EvidenceModel):
    usable_non_loopback_interfaces: tuple[str, ...]
    interface_scan_error: bool
    default_route_families: tuple[Literal["ipv4", "ipv6"], ...]
    route_scan_error: bool
    dns: tuple[DnsObservation, ...]
    tcp: tuple[TcpObservation, ...]
    unix: tuple[UnixObservation, ...]
    socket_inventory: tuple[SocketInventoryObservation, ...]
    proc_net_unix_entries: _NONNEGATIVE
    proc_net_unix_scan_error: bool
    raw_socket: PrivilegedSocketObservation
    packet_socket: PrivilegedSocketObservation

    @model_validator(mode="after")
    def _fixed_matrices_are_complete(self) -> Self:
        interfaces = self.usable_non_loopback_interfaces
        if interfaces != tuple(sorted(set(interfaces))):
            raise ValueError("usable interface names must be sorted and unique")
        if any(_INTERFACE_NAME.fullmatch(name) is None for name in interfaces):
            raise ValueError("usable interface name is invalid")
        route_order = tuple(
            family for family in ("ipv4", "ipv6") if family in self.default_route_families
        )
        if self.default_route_families != route_order:
            raise ValueError("default route families must be unique and in fixed order")
        if tuple(item.target for item in self.dns) != _DNS_ORDER:
            raise ValueError("DNS matrix must be complete and in fixed order")
        if tuple(item.target for item in self.tcp) != _TCP_ORDER:
            raise ValueError("TCP matrix must be complete and in fixed order")
        if tuple(item.target for item in self.unix) != _UNIX_ORDER:
            raise ValueError("Unix socket matrix must be complete and in fixed order")
        if tuple(item.root for item in self.socket_inventory) != _SOCKET_ROOT_ORDER:
            raise ValueError("socket inventory must be complete and in fixed order")
        return self


class ContainmentObservation(_EvidenceModel):
    schema_version: Literal["guildmind.containment-probe/v1"] = _OBSERVATION_SCHEMA
    profile: ContainmentProfile
    program_sha256: _HASH
    sentinels: SentinelInventory
    environment: tuple[EnvironmentValue, ...]
    mounts: tuple[MountObservation, ...]
    mountinfo_complete: bool
    unexpected_input_mounts: tuple[_HASH, ...]
    credentials: tuple[CredentialObservation, ...]
    network: NetworkObservation

    @model_validator(mode="after")
    def _inventories_are_canonical(self) -> Self:
        environment_names = tuple(item.name for item in self.environment)
        if environment_names != tuple(sorted(set(environment_names))):
            raise ValueError("environment inventory must be sorted by unique name")
        if tuple(item.target.value for item in self.mounts) != _MOUNT_ORDER:
            raise ValueError("mount inventory must be complete and in fixed order")
        if self.unexpected_input_mounts != tuple(sorted(set(self.unexpected_input_mounts))):
            raise ValueError("unexpected input mount hashes must be sorted and unique")
        if tuple(item.target for item in self.credentials) != _CREDENTIAL_ORDER:
            raise ValueError("credential inventory must be complete and in fixed order")
        return self


class CleanupEvidence(_EvidenceModel):
    target: str | None
    removal_attempted: bool
    removal_succeeded: bool | None
    absence_checked: bool
    absent: bool | None
    diagnostic_sha256: _HASH | None

    @property
    def confirmed(self) -> bool:
        return self.removal_succeeded is True and self.absent is True


class ContainerStateEvidence(_EvidenceModel):
    exit_code: int
    oom_killed: bool
    running: bool
    error_sha256: _HASH | None


class KillEvidence(_EvidenceModel):
    attempted: bool
    succeeded: bool | None
    diagnostic_sha256: _HASH | None


class TranscriptEvidence(_EvidenceModel):
    stdout_sha256: _HASH
    stdout_bytes: _NONNEGATIVE
    stderr_sha256: _HASH
    stderr_bytes: _NONNEGATIVE


class ContainmentExecutionEvidence(_EvidenceModel):
    status: ExecutionStatus
    error_stage: ExecutionErrorStage | None
    sandbox_status: SandboxStatus
    pre_cleanup_status: SandboxStatus
    exit_code: int | None
    output_truncated: bool
    state: ContainerStateEvidence | None
    kill: KillEvidence
    cleanup: CleanupEvidence
    transcript: TranscriptEvidence
    diagnostic: Literal["malformed containment observation"] | None
    adapter_diagnostic_sha256: _HASH | None

    @model_validator(mode="after")
    def _error_stage_matches_status(self) -> Self:
        if (self.status is ExecutionStatus.ERROR) != (self.error_stage is not None):
            raise ValueError("execution error status and stage must agree")
        if self.status is ExecutionStatus.COMPLETED and not self.cleanup.confirmed:
            raise ValueError("completed execution requires verified cleanup")
        return self


class SentinelExpectation(_EvidenceModel):
    name: _NONEMPTY
    channel: SentinelChannel
    disposition: SentinelDisposition
    value_sha256: _HASH


class ContainmentCheck(_EvidenceModel):
    name: _NONEMPTY
    outcome: CheckOutcome
    expected: bool | int | str | None
    observed: bool | int | str | None
    source: _NONEMPTY


class SourceIntegrityEvidence(_EvidenceModel):
    name: Literal[
        "workspace",
        "challenge",
        "grader",
        "response",
        "sibling",
        "control_plane",
    ]
    expected_sha256: _HASH
    observed_sha256: _HASH | None


class ContainmentProfileEvidence(_EvidenceModel):
    profile: ContainmentProfile
    execution: ContainmentExecutionEvidence
    expectations: tuple[SentinelExpectation, ...]
    expected_environment: tuple[EnvironmentValue, ...]
    observation: ContainmentObservation | None
    source_integrity: tuple[SourceIntegrityEvidence, ...]
    checks: tuple[ContainmentCheck, ...]
    verdict: ContainmentVerdict
    diagnostic: str | None

    @model_validator(mode="after")
    def _verdict_is_derived(self) -> Self:
        _validate_expectation_layout(self.profile, self.expectations)
        _validate_expected_environment_layout(self.profile, self.expected_environment)
        if tuple(item.name for item in self.source_integrity) != (
            "workspace",
            "challenge",
            "grader",
            "response",
            "sibling",
            "control_plane",
        ):
            raise ValueError("source integrity evidence must use the fixed canary order")
        expectations_by_name = {item.name: item for item in self.expectations}
        if any(
            item.expected_sha256 != expectations_by_name[item.name].value_sha256
            for item in self.source_integrity
        ):
            raise ValueError("source integrity identities must match canary expectations")
        _validate_profile_environment_bindings(
            self.profile,
            expectations=self.expectations,
            expected_environment=self.expected_environment,
        )
        expected_names = tuple(item.name for item in self.expected_environment)
        if expected_names != tuple(sorted(set(expected_names))):
            raise ValueError("expected environment must be sorted by unique name")
        derived_checks = _derived_profile_checks(
            self.profile,
            execution=self.execution,
            expectations=self.expectations,
            expected_environment=self.expected_environment,
            observation=self.observation,
            source_integrity=self.source_integrity,
        )
        if self.checks != derived_checks:
            raise ValueError("containment checks must be exactly derived from retained evidence")
        verdict = _verdict(derived_checks)
        if self.verdict is not verdict:
            raise ValueError("containment verdict must be derived from named checks")
        if verdict is ContainmentVerdict.CONTAINED:
            if self.observation is None:
                raise ValueError("contained evidence requires a retained observation")
            if self.execution.status is not ExecutionStatus.COMPLETED:
                raise ValueError("contained evidence requires completed execution")
        failures = tuple(
            check.name for check in derived_checks if check.outcome is not CheckOutcome.PASS
        )
        diagnostic = (
            None if not failures else f"non-passing containment checks: {', '.join(failures)}"
        )
        if self.diagnostic != diagnostic:
            raise ValueError("containment diagnostic must be derived from named checks")
        return self


class ContainmentProbeSuiteEvidence(_EvidenceModel):
    schema_version: Literal["guildmind.containment-probe-evidence/v1"] = _EVIDENCE_SCHEMA
    probe_id: _NONEMPTY
    profile_version: Literal["guildmind.evaluator-containment/v1"] = _PROFILE_VERSION
    probe_spec_sha256: _HASH
    code_revision: _NONEMPTY
    occurred_at: _UTC_DATETIME
    environment: DockerHostEvidence
    requested_limits: SandboxLimitsEvidence
    profiles: tuple[ContainmentProfileEvidence, ...]
    all_contained: bool
    reference_eligible: bool
    reference_passed: bool

    @model_validator(mode="after")
    def _suite_claims_are_derived(self) -> Self:
        if self.probe_spec_sha256 != containment_probe_spec_sha256():
            raise ValueError("probe_spec_sha256 must match the fixed containment contract")
        if self.requested_limits != _limits_evidence(candidate_resource_limits()):
            raise ValueError("requested limits must match the fixed containment profile")
        if tuple(item.profile for item in self.profiles) != (
            ContainmentProfile.CANDIDATE,
            ContainmentProfile.SCORER,
        ):
            raise ValueError("containment profiles must be candidate then scorer")
        scorer_environment = {
            item.name: item.value_sha256 for item in self.profiles[1].expected_environment
        }
        image_digest = self.environment.image_reference.rsplit("@", maxsplit=1)[1]
        if scorer_environment["GUILDMIND_IMAGE_DIGEST"] != sha256_bytes(
            image_digest.encode("utf-8")
        ):
            raise ValueError("scorer image binding must match the verified image reference")
        all_contained = all(item.verdict is ContainmentVerdict.CONTAINED for item in self.profiles)
        reference_eligible = (
            self.environment.tier is EvidenceTier.REFERENCE
            and self.environment.accepted
            and self.environment.reference_ready
            and self.environment.host_policy is DockerHostMode.REFERENCE
            and _CLEAN_CODE_REVISION.fullmatch(self.code_revision) is not None
        )
        if self.all_contained != all_contained:
            raise ValueError("all_contained must be derived from profile verdicts")
        if self.reference_eligible != reference_eligible:
            raise ValueError("reference_eligible must be derived from host evidence")
        if self.reference_passed != (all_contained and reference_eligible):
            raise ValueError("reference_passed requires containment and reference eligibility")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())


@dataclass(frozen=True, slots=True)
class _Canary:
    name: str
    path: Path | None
    channel: SentinelChannel
    value: bytes
    value_sha256: str


@dataclass(frozen=True, slots=True)
class _ProbeInputs:
    workspace: Path
    challenge: Path
    grader: Path
    response: Path
    canaries: Mapping[str, _Canary]


def containment_probe_spec_sha256() -> str:
    """Return the content identity of the fixed host/image containment contract."""
    return canonical_sha256(
        {
            "base_environment": _environment_hashes(_BASE_ENVIRONMENT),
            "credential_order": _CREDENTIAL_ORDER,
            "dns_order": _DNS_ORDER,
            "evidence_schema": _EVIDENCE_SCHEMA,
            "mount_order": _MOUNT_ORDER,
            "mount_profiles": {
                "candidate": ["workspace", "challenge"],
                "scorer": ["challenge", "grader", "response"],
            },
            "observation_schema": _OBSERVATION_SCHEMA,
            "profile_version": _PROFILE_VERSION,
            "profiles": [profile.value for profile in ContainmentProfile],
            "program": _PROGRAM,
            "program_sha256": _EXPECTED_PROGRAM_SHA256,
            "requested_limits": _limits_dict(candidate_resource_limits()),
            "sentinel_prefix": _SENTINEL_PREFIX,
            "strong_errno_policy": {
                "loopback_closed": sorted(_LOOPBACK_CLOSED_ERRNOS),
                "nonloopback_blocked": sorted(_NONLOOPBACK_BLOCK_ERRNOS),
                "privileged_socket_denied": sorted(_PRIVILEGED_SOCKET_DENIED_ERRNOS),
                "write_denied": sorted(_WRITE_DENIED_ERRNOS),
            },
            "socket_root_order": _SOCKET_ROOT_ORDER,
            "tcp_order": _TCP_ORDER,
            "unix_order": _UNIX_ORDER,
        }
    )


def run_containment_probe_suite(
    sandbox: DockerSandbox,
    *,
    image: str,
    code_revision: str,
    probe_id: str,
    occurred_at: datetime | None = None,
) -> ContainmentProbeSuiteEvidence:
    """Run candidate- and scorer-shaped active probes and derive typed evidence."""
    assessment = sandbox.assess_host()
    assessment.require_accepted()
    image_id = sandbox.verify_image(image)
    host = _host_evidence(sandbox, assessment, image=image, image_id=image_id)
    limits = candidate_resource_limits()

    with tempfile.TemporaryDirectory(prefix="guildmind-containment-") as temporary:
        inputs = _plant_canaries(Path(temporary))
        profiles = tuple(
            _run_profile(sandbox, image=image, limits=limits, inputs=inputs, profile=profile)
            for profile in (ContainmentProfile.CANDIDATE, ContainmentProfile.SCORER)
        )

    all_contained = all(item.verdict is ContainmentVerdict.CONTAINED for item in profiles)
    reference_eligible = (
        host.tier is EvidenceTier.REFERENCE
        and host.accepted
        and host.reference_ready
        and host.host_policy is DockerHostMode.REFERENCE
        and _CLEAN_CODE_REVISION.fullmatch(code_revision) is not None
    )
    return ContainmentProbeSuiteEvidence(
        probe_id=probe_id,
        probe_spec_sha256=containment_probe_spec_sha256(),
        code_revision=code_revision,
        occurred_at=occurred_at or datetime.now(UTC),
        environment=host,
        requested_limits=_limits_evidence(limits),
        profiles=profiles,
        all_contained=all_contained,
        reference_eligible=reference_eligible,
        reference_passed=all_contained and reference_eligible,
    )


def parse_containment_observation(data: bytes) -> ContainmentObservation:
    """Parse the image boundary: one duplicate-free LF-terminated JSON object."""
    _single_json_object(data)
    return ContainmentObservation.model_validate_json(data)


def _run_profile(
    sandbox: DockerSandbox,
    *,
    image: str,
    limits: SandboxLimits,
    inputs: _ProbeInputs,
    profile: ContainmentProfile,
) -> ContainmentProfileEvidence:
    environment = _profile_environment(profile, image=image, limits=limits, inputs=inputs)
    request = _profile_request(
        profile,
        image=image,
        limits=limits,
        inputs=inputs,
        environment=environment,
    )
    expectations = _profile_expectations(profile, inputs)
    env_canary = inputs.canaries[f"{profile.value}_environment"]
    with _planted_environment(_HOST_ENV_SENTINEL, env_canary.value.decode("utf-8")):
        run = sandbox.run_observed(request)

    execution = _execution_evidence(run)
    observation: ContainmentObservation | None = None
    can_parse = (
        execution.status is ExecutionStatus.COMPLETED
        and execution.pre_cleanup_status is SandboxStatus.EXITED
        and execution.exit_code == 0
        and not execution.output_truncated
    )
    if can_parse:
        try:
            observation = parse_containment_observation(run.result.stdout)
        except (ValueError, json.JSONDecodeError):
            execution = execution.model_copy(
                update={
                    "status": ExecutionStatus.ERROR,
                    "error_stage": ExecutionErrorStage.OBSERVATION,
                    "diagnostic": "malformed containment observation",
                }
            )
    source_integrity = _source_integrity_evidence(inputs)
    expected_environment = _expected_environment(environment)
    frozen_checks = _derived_profile_checks(
        profile,
        execution=execution,
        expectations=expectations,
        expected_environment=expected_environment,
        observation=observation,
        source_integrity=source_integrity,
    )
    verdict = _verdict(frozen_checks)
    failures = tuple(
        check.name for check in frozen_checks if check.outcome is not CheckOutcome.PASS
    )
    diagnostic = None if not failures else f"non-passing containment checks: {', '.join(failures)}"
    return ContainmentProfileEvidence(
        profile=profile,
        execution=execution,
        expectations=expectations,
        expected_environment=expected_environment,
        observation=observation,
        source_integrity=source_integrity,
        checks=frozen_checks,
        verdict=verdict,
        diagnostic=diagnostic,
    )


def _derived_profile_checks(
    profile: ContainmentProfile,
    *,
    execution: ContainmentExecutionEvidence,
    expectations: tuple[SentinelExpectation, ...],
    expected_environment: tuple[EnvironmentValue, ...],
    observation: ContainmentObservation | None,
    source_integrity: tuple[SourceIntegrityEvidence, ...],
) -> tuple[ContainmentCheck, ...]:
    checks: list[ContainmentCheck] = [
        _check(
            "lifecycle_completed",
            (
                CheckOutcome.PASS
                if execution.status is ExecutionStatus.COMPLETED
                else CheckOutcome.INCONCLUSIVE
            ),
            True,
            execution.status is ExecutionStatus.COMPLETED,
            "docker:lifecycle",
        ),
        _check(
            "probe_exited_cleanly",
            (
                CheckOutcome.PASS
                if execution.pre_cleanup_status is SandboxStatus.EXITED
                and execution.exit_code == 0
                and not execution.output_truncated
                else CheckOutcome.INCONCLUSIVE
            ),
            "exited:0:not-truncated",
            (
                f"{execution.pre_cleanup_status.value}:{execution.exit_code}:"
                f"{execution.output_truncated}"
            ),
            "docker:.State+capture",
        ),
        _check(
            "container_state_clean",
            (
                CheckOutcome.PASS
                if execution.state is not None
                and not execution.state.running
                and not execution.state.oom_killed
                and execution.state.error_sha256 is None
                and execution.state.exit_code == execution.exit_code
                else CheckOutcome.INCONCLUSIVE
            ),
            "stopped:no-oom:no-error:matching-exit",
            (
                "unavailable"
                if execution.state is None
                else (
                    f"running={execution.state.running}:oom={execution.state.oom_killed}:"
                    f"error={execution.state.error_sha256 is not None}:"
                    f"exit={execution.state.exit_code}"
                )
            ),
            "docker:.State",
        ),
        _check(
            "adapter_diagnostics_absent",
            (
                CheckOutcome.PASS
                if execution.adapter_diagnostic_sha256 is None
                and execution.state is not None
                and execution.state.error_sha256 is None
                and execution.kill.diagnostic_sha256 is None
                and execution.cleanup.diagnostic_sha256 is None
                else CheckOutcome.INCONCLUSIVE
            ),
            0,
            sum(
                value is not None
                for value in (
                    execution.adapter_diagnostic_sha256,
                    None if execution.state is None else execution.state.error_sha256,
                    execution.kill.diagnostic_sha256,
                    execution.cleanup.diagnostic_sha256,
                )
            ),
            "docker:diagnostic-fingerprints",
        ),
    ]
    if observation is None:
        checks.append(
            _check(
                "observation_valid",
                CheckOutcome.INCONCLUSIVE,
                True,
                False,
                "probe:stdout",
            )
        )
    else:
        checks.extend(
            _observation_checks(
                profile,
                observation=observation,
                expectations=expectations,
                expected_environment=expected_environment,
            )
        )
    checks.extend(_source_integrity_checks(source_integrity))
    return tuple(checks)


def _observation_checks(
    profile: ContainmentProfile,
    *,
    observation: ContainmentObservation,
    expectations: tuple[SentinelExpectation, ...],
    expected_environment: tuple[EnvironmentValue, ...],
) -> tuple[ContainmentCheck, ...]:
    checks: list[ContainmentCheck] = [
        _check(
            "observation_valid",
            CheckOutcome.PASS,
            True,
            True,
            "host:strict-parser",
        ),
        _check(
            "profile_identity",
            CheckOutcome.PASS if observation.profile is profile else CheckOutcome.INCONCLUSIVE,
            profile.value,
            observation.profile.value,
            "probe:profile",
        ),
        _check(
            "program_identity",
            (
                CheckOutcome.PASS
                if observation.program_sha256 == _EXPECTED_PROGRAM_SHA256
                else CheckOutcome.INCONCLUSIVE
            ),
            _EXPECTED_PROGRAM_SHA256,
            observation.program_sha256,
            "probe:sha256(__file__)",
        ),
        _check(
            "sentinel_scan_complete",
            (
                CheckOutcome.PASS
                if observation.sentinels.scan_errors == 0
                and not observation.sentinels.scan_truncated
                else CheckOutcome.INCONCLUSIVE
            ),
            "errors=0,truncated=false",
            (
                f"errors={observation.sentinels.scan_errors},"
                f"truncated={observation.sentinels.scan_truncated}"
            ),
            "probe:bounded-sentinel-scan",
        ),
    ]
    observed_files = set(observation.sentinels.file_sha256)
    observed_environment_sentinels = {
        item.value_sha256 for item in observation.sentinels.environment
    }
    known_file_hashes: set[str] = set()
    known_environment_hashes: set[str] = set()
    for expectation in expectations:
        if expectation.channel is SentinelChannel.FILE:
            observed = expectation.value_sha256 in observed_files
            known_file_hashes.add(expectation.value_sha256)
        else:
            observed = expectation.value_sha256 in observed_environment_sentinels
            known_environment_hashes.add(expectation.value_sha256)
        if expectation.disposition is SentinelDisposition.VISIBLE:
            outcome = CheckOutcome.PASS if observed else CheckOutcome.INCONCLUSIVE
            expected = True
        else:
            outcome = CheckOutcome.EXPOSURE if observed else CheckOutcome.PASS
            expected = False
        checks.append(
            _check(
                f"sentinel_{expectation.name}",
                outcome,
                expected,
                observed,
                f"probe:sentinels:{expectation.channel.value}",
            )
        )
    unknown_files = observed_files - known_file_hashes
    unknown_environment = observed_environment_sentinels - known_environment_hashes
    checks.extend(
        (
            _check(
                "unknown_file_sentinels",
                CheckOutcome.PASS if not unknown_files else CheckOutcome.EXPOSURE,
                0,
                len(unknown_files),
                "probe:sentinels:file",
            ),
            _check(
                "unknown_environment_sentinels",
                CheckOutcome.PASS if not unknown_environment else CheckOutcome.EXPOSURE,
                0,
                len(unknown_environment),
                "probe:sentinels:environment",
            ),
        )
    )
    checks.extend(_environment_checks(observation.environment, expected_environment))
    checks.extend(_mount_checks(profile, observation))
    checks.extend(_credential_checks(observation.credentials))
    checks.extend(_network_checks(observation.network))
    return tuple(checks)


def _environment_checks(
    observed: tuple[EnvironmentValue, ...],
    expected: tuple[EnvironmentValue, ...],
) -> tuple[ContainmentCheck, ...]:
    observed_by_name = {item.name: item.value_sha256 for item in observed}
    expected_by_name = {item.name: item.value_sha256 for item in expected}
    checks = [
        _check(
            f"environment_{name}",
            (
                CheckOutcome.PASS
                if observed_by_name.get(name) == expected_hash
                else CheckOutcome.INCONCLUSIVE
            ),
            expected_hash,
            observed_by_name.get(name),
            "probe:os.environ",
        )
        for name, expected_hash in expected_by_name.items()
    ]
    unexpected = sorted(set(observed_by_name) - set(expected_by_name))
    checks.append(
        _check(
            "environment_unexpected_names",
            CheckOutcome.PASS if not unexpected else CheckOutcome.EXPOSURE,
            0,
            len(unexpected),
            "probe:os.environ",
        )
    )
    return tuple(checks)


def _mount_checks(
    profile: ContainmentProfile,
    observation: ContainmentObservation,
) -> tuple[ContainmentCheck, ...]:
    expected_present = (
        {MountTarget.WORKSPACE, MountTarget.CHALLENGE}
        if profile is ContainmentProfile.CANDIDATE
        else {MountTarget.CHALLENGE, MountTarget.GRADER, MountTarget.RESPONSE}
    )
    checks: list[ContainmentCheck] = [
        _check(
            "mountinfo_complete",
            CheckOutcome.PASS if observation.mountinfo_complete else CheckOutcome.INCONCLUSIVE,
            True,
            observation.mountinfo_complete,
            "probe:/proc/self/mountinfo",
        ),
        _check(
            "unexpected_input_mounts",
            (
                CheckOutcome.PASS
                if not observation.unexpected_input_mounts
                else CheckOutcome.EXPOSURE
            ),
            0,
            len(observation.unexpected_input_mounts),
            "probe:/proc/self/mountinfo",
        ),
    ]
    for mount in observation.mounts:
        should_exist = mount.target in expected_present
        checks.append(
            _check(
                f"mount_{mount.target.value}_presence",
                (
                    CheckOutcome.PASS
                    if mount.present is should_exist
                    else (CheckOutcome.EXPOSURE if mount.present else CheckOutcome.INCONCLUSIVE)
                ),
                should_exist,
                mount.present,
                "probe:/proc/self/mountinfo",
            )
        )
        if not should_exist or not mount.present:
            continue
        checks.append(
            _check(
                f"mount_{mount.target.value}_read_only",
                (CheckOutcome.PASS if mount.read_only is True else CheckOutcome.EXPOSURE),
                True,
                mount.read_only,
                "probe:/proc/self/mountinfo",
            )
        )
        if mount.write_outcome is WriteOutcome.DENIED and mount.write_errno in _WRITE_DENIED_ERRNOS:
            write_outcome = CheckOutcome.PASS
        elif mount.write_outcome is WriteOutcome.SUCCEEDED:
            write_outcome = CheckOutcome.EXPOSURE
        else:
            write_outcome = CheckOutcome.INCONCLUSIVE
        checks.append(
            _check(
                f"mount_{mount.target.value}_active_write",
                write_outcome,
                "denied:EPERM|EACCES|EROFS",
                f"{mount.write_outcome.value}:{mount.write_errno}",
                "probe:active-write",
            )
        )
    return tuple(checks)


def _credential_checks(
    observations: tuple[CredentialObservation, ...],
) -> tuple[ContainmentCheck, ...]:
    checks: list[ContainmentCheck] = []
    for item in observations:
        if item.outcome is CredentialOutcome.ABSENT or (
            item.outcome is CredentialOutcome.INACCESSIBLE and item.errno in {1, 13}
        ):
            outcome = CheckOutcome.PASS
        elif item.outcome is CredentialOutcome.PRESENT:
            outcome = CheckOutcome.EXPOSURE
        else:
            outcome = CheckOutcome.INCONCLUSIVE
        checks.append(
            _check(
                f"credential_{item.target}",
                outcome,
                "absent-or-inaccessible",
                f"{item.outcome.value}:{item.errno}:readable={item.readable}",
                "probe:credential-path",
            )
        )
    return tuple(checks)


def _network_checks(observation: NetworkObservation) -> tuple[ContainmentCheck, ...]:
    checks: list[ContainmentCheck] = [
        _check(
            "usable_non_loopback_interfaces",
            (
                CheckOutcome.INCONCLUSIVE
                if observation.interface_scan_error
                else (
                    CheckOutcome.PASS
                    if not observation.usable_non_loopback_interfaces
                    else CheckOutcome.EXPOSURE
                )
            ),
            0,
            len(observation.usable_non_loopback_interfaces),
            "probe:interface-address-state",
        ),
        _check(
            "default_routes",
            (
                CheckOutcome.INCONCLUSIVE
                if observation.route_scan_error
                else (
                    CheckOutcome.PASS
                    if not observation.default_route_families
                    else CheckOutcome.EXPOSURE
                )
            ),
            0,
            len(observation.default_route_families),
            "probe:/proc/net/route",
        ),
    ]
    for dns_item in observation.dns:
        outcome = {
            DnsOutcome.GAI_ERROR: CheckOutcome.PASS,
            DnsOutcome.RESOLVED: CheckOutcome.EXPOSURE,
            DnsOutcome.TIMEOUT: CheckOutcome.INCONCLUSIVE,
            DnsOutcome.OS_ERROR: CheckOutcome.INCONCLUSIVE,
        }[dns_item.outcome]
        checks.append(
            _check(
                f"dns_{dns_item.target}",
                outcome,
                "gai_error",
                f"{dns_item.outcome.value}:{dns_item.error_code}",
                "probe:socket.getaddrinfo",
            )
        )
    for tcp_item in observation.tcp:
        loopback = tcp_item.target.startswith("loopback_")
        if tcp_item.outcome is ConnectOutcome.CONNECTED:
            outcome = CheckOutcome.EXPOSURE
        elif tcp_item.outcome is ConnectOutcome.OS_ERROR and (
            (loopback and tcp_item.errno in _LOOPBACK_CLOSED_ERRNOS)
            or (not loopback and tcp_item.errno in _NONLOOPBACK_BLOCK_ERRNOS)
        ):
            outcome = CheckOutcome.PASS
        else:
            outcome = CheckOutcome.INCONCLUSIVE
        checks.append(
            _check(
                f"tcp_{tcp_item.target}",
                outcome,
                "strongly-blocked",
                f"{tcp_item.outcome.value}:{tcp_item.errno}",
                "probe:socket.connect_ex",
            )
        )
    for unix_item in observation.unix:
        outcome = (
            CheckOutcome.PASS if unix_item.outcome is UnixOutcome.ABSENT else CheckOutcome.EXPOSURE
        )
        checks.append(
            _check(
                f"unix_{unix_item.target}",
                outcome,
                "absent",
                unix_item.outcome.value,
                "probe:AF_UNIX",
            )
        )
    for inventory_item in observation.socket_inventory:
        if inventory_item.scan_error:
            outcome = CheckOutcome.INCONCLUSIVE
        elif inventory_item.socket_count:
            outcome = CheckOutcome.EXPOSURE
        else:
            outcome = CheckOutcome.PASS
        checks.append(
            _check(
                f"socket_inventory_{inventory_item.root}",
                outcome,
                0,
                inventory_item.socket_count,
                "probe:bounded-socket-inventory",
            )
        )
    checks.append(
        _check(
            "proc_net_unix",
            (
                CheckOutcome.INCONCLUSIVE
                if observation.proc_net_unix_scan_error
                else (
                    CheckOutcome.PASS
                    if observation.proc_net_unix_entries == 0
                    else CheckOutcome.EXPOSURE
                )
            ),
            0,
            observation.proc_net_unix_entries,
            "probe:/proc/net/unix",
        )
    )
    for name, socket_item in (
        ("raw_socket", observation.raw_socket),
        ("packet_socket", observation.packet_socket),
    ):
        if socket_item.outcome is PrivilegedSocketOutcome.CREATED:
            outcome = CheckOutcome.EXPOSURE
        elif socket_item.errno in _PRIVILEGED_SOCKET_DENIED_ERRNOS:
            outcome = CheckOutcome.PASS
        else:
            outcome = CheckOutcome.INCONCLUSIVE
        checks.append(
            _check(
                name,
                outcome,
                "denied:EPERM|EACCES",
                f"{socket_item.outcome.value}:{socket_item.errno}",
                "probe:privileged-socket",
            )
        )
    return tuple(checks)


def _source_integrity_evidence(inputs: _ProbeInputs) -> tuple[SourceIntegrityEvidence, ...]:
    evidence: list[SourceIntegrityEvidence] = []
    for name in ("workspace", "challenge", "grader", "response", "sibling", "control_plane"):
        canary = inputs.canaries[name]
        if canary.path is None:
            raise AssertionError("file canary has no host path")
        try:
            observed_hash: str | None = sha256_bytes(canary.path.read_bytes())
        except OSError:
            observed_hash = None
        evidence.append(
            SourceIntegrityEvidence(
                name=name,
                expected_sha256=canary.value_sha256,
                observed_sha256=observed_hash,
            )
        )
    return tuple(evidence)


def _source_integrity_checks(
    evidence: tuple[SourceIntegrityEvidence, ...],
) -> tuple[ContainmentCheck, ...]:
    checks: list[ContainmentCheck] = []
    for item in evidence:
        if item.observed_sha256 == item.expected_sha256:
            outcome = CheckOutcome.PASS
        elif item.observed_sha256 is None:
            outcome = CheckOutcome.INCONCLUSIVE
        else:
            outcome = CheckOutcome.EXPOSURE
        checks.append(
            _check(
                f"host_source_{item.name}_unchanged",
                outcome,
                item.expected_sha256,
                item.observed_sha256,
                "host:post-run-sha256",
            )
        )
    return tuple(checks)


def _expected_environment(request_environment: Mapping[str, str]) -> tuple[EnvironmentValue, ...]:
    combined = {**_BASE_ENVIRONMENT, **request_environment}
    return _environment_hashes(combined)


def _execution_evidence(run: ObservedSandboxRun) -> ContainmentExecutionEvidence:
    result = run.result
    observed = run.evidence
    error_stage: ExecutionErrorStage | None = None
    dispatch_failed_before_create = (
        result.status is SandboxStatus.INFRASTRUCTURE_ERROR
        and observed.container_id is None
        and observed.cleanup.target is None
        and not observed.cleanup.removal_attempted
    )
    if dispatch_failed_before_create:
        error_stage = ExecutionErrorStage.DISPATCH
    elif not observed.cleanup.confirmed:
        error_stage = ExecutionErrorStage.CLEANUP
    elif result.status is SandboxStatus.INFRASTRUCTURE_ERROR:
        error_stage = ExecutionErrorStage.DISPATCH
    status = ExecutionStatus.ERROR if error_stage is not None else ExecutionStatus.COMPLETED
    state = observed.state
    return ContainmentExecutionEvidence(
        status=status,
        error_stage=error_stage,
        sandbox_status=result.status,
        pre_cleanup_status=observed.pre_cleanup_status,
        exit_code=(
            result.exit_code
            if result.exit_code is not None
            else (None if state is None else state.exit_code)
        ),
        output_truncated=result.output_truncated,
        state=(
            None
            if state is None
            else ContainerStateEvidence(
                exit_code=state.exit_code,
                oom_killed=state.oom_killed,
                running=state.running,
                error_sha256=_diagnostic_sha256(state.error),
            )
        ),
        kill=KillEvidence(
            attempted=observed.kill.attempted,
            succeeded=observed.kill.succeeded,
            diagnostic_sha256=_diagnostic_sha256(observed.kill.diagnostic),
        ),
        cleanup=CleanupEvidence(
            target=observed.cleanup.target,
            removal_attempted=observed.cleanup.removal_attempted,
            removal_succeeded=observed.cleanup.removal_succeeded,
            absence_checked=observed.cleanup.absence_checked,
            absent=observed.cleanup.absent,
            diagnostic_sha256=_diagnostic_sha256(observed.cleanup.diagnostic),
        ),
        transcript=TranscriptEvidence(
            stdout_sha256=sha256_bytes(result.stdout),
            stdout_bytes=len(result.stdout),
            stderr_sha256=sha256_bytes(result.stderr),
            stderr_bytes=len(result.stderr),
        ),
        diagnostic=None,
        adapter_diagnostic_sha256=_diagnostic_sha256(result.diagnostic),
    )


def _diagnostic_sha256(value: str | None) -> str | None:
    if not value:
        return None
    return sha256_bytes(value.encode("utf-8", errors="surrogatepass"))


def _check(
    name: str,
    outcome: CheckOutcome,
    expected: bool | int | str | None,
    observed: bool | int | str | None,
    source: str,
) -> ContainmentCheck:
    return ContainmentCheck(
        name=name,
        outcome=outcome,
        expected=expected,
        observed=observed,
        source=source,
    )


def _verdict(checks: tuple[ContainmentCheck, ...]) -> ContainmentVerdict:
    if any(check.outcome is CheckOutcome.EXPOSURE for check in checks):
        return ContainmentVerdict.EXPOSED
    if checks and all(check.outcome is CheckOutcome.PASS for check in checks):
        return ContainmentVerdict.CONTAINED
    return ContainmentVerdict.INCONCLUSIVE


def _profile_request(
    profile: ContainmentProfile,
    *,
    image: str,
    limits: SandboxLimits,
    inputs: _ProbeInputs,
    environment: Mapping[str, str],
) -> SandboxRequest:
    mounts: tuple[SandboxMount, ...]
    if profile is ContainmentProfile.CANDIDATE:
        mounts = (
            SandboxMount(source=inputs.workspace, target="/inputs/workspace"),
            SandboxMount(source=inputs.challenge, target="/inputs/challenge.json"),
        )
    else:
        mounts = (
            SandboxMount(source=inputs.challenge, target="/inputs/challenge.json"),
            SandboxMount(source=inputs.grader, target="/inputs/grader"),
            SandboxMount(source=inputs.response, target="/inputs/response.txt"),
        )
    return SandboxRequest(
        execution_id=f"containment-{profile.value}",
        image=image,
        argv=(_PYTHON, "-I", _PROGRAM, profile.value),
        limits=limits,
        environment=environment,
        mounts=mounts,
    )


def _profile_environment(
    profile: ContainmentProfile,
    *,
    image: str,
    limits: SandboxLimits,
    inputs: _ProbeInputs,
) -> Mapping[str, str]:
    if profile is ContainmentProfile.CANDIDATE:
        return {}
    image_digest = image.rsplit("@", maxsplit=1)[1]
    challenge_hash = inputs.canaries["challenge"].value_sha256
    oracle_hash = inputs.canaries["grader"].value_sha256
    response_hash = inputs.canaries["response"].value_sha256
    return {
        "GUILDMIND_CHALLENGE_SHA256": challenge_hash,
        "GUILDMIND_EVALUATOR_VERSION": "guildmind/container-python-call-v2",
        "GUILDMIND_EXPECTED_TESTS": "1",
        "GUILDMIND_IMAGE_DIGEST": image_digest,
        "GUILDMIND_LIMITS_SHA256": canonical_sha256(_limits_dict(limits)),
        "GUILDMIND_ORACLE_SHA256": oracle_hash,
        "GUILDMIND_PATCH_SHA256": sha256_bytes(b"containment-probe-patch"),
        "GUILDMIND_RESPONSE_SHA256": response_hash,
        "GUILDMIND_SOURCE_SHA256": sha256_bytes(b"containment-probe-source"),
        "GUILDMIND_TASK_CONTENT_HASH": sha256_bytes(b"containment-probe-task"),
        "GUILDMIND_TASK_ID": "containment-probe",
    }


def _plant_canaries(root: Path) -> _ProbeInputs:
    workspace = root / "workspace"
    grader = root / "grader"
    control = root / "control-plane"
    workspace.mkdir()
    grader.mkdir()
    control.mkdir()
    challenge = root / "challenge.json"
    response = root / "response.txt"
    sibling = root / "sibling-secret.txt"
    paths = {
        "workspace": workspace / "canary.txt",
        "challenge": challenge,
        "grader": grader / "oracle.json",
        "response": response,
        "sibling": sibling,
        "control_plane": control / "sentinel.txt",
    }
    canaries: dict[str, _Canary] = {}
    for name, path in paths.items():
        sentinel = _new_canary_value(None, name)
        if name in {"challenge", "grader"}:
            data = (
                canonical_json(
                    {
                        "schema_version": "guildmind.containment-canary/v1",
                        "sentinel": sentinel,
                    }
                )
                + "\n"
            ).encode("utf-8")
        else:
            data = (sentinel + "\n").encode("utf-8")
        path.write_bytes(data)
        canaries[name] = _Canary(
            name=name,
            path=path,
            channel=SentinelChannel.FILE,
            value=data,
            value_sha256=sha256_bytes(data),
        )
    for profile in ContainmentProfile:
        value = _new_canary_value(profile, "environment").encode("utf-8")
        canaries[f"{profile.value}_environment"] = _Canary(
            name=f"{profile.value}_environment",
            path=None,
            channel=SentinelChannel.ENVIRONMENT,
            value=value,
            value_sha256=sha256_bytes(value),
        )
    return _ProbeInputs(
        workspace=workspace.resolve(),
        challenge=challenge.resolve(),
        grader=grader.resolve(),
        response=response.resolve(),
        canaries=canaries,
    )


def _new_canary_value(profile: ContainmentProfile | None, name: str) -> str:
    scope = "shared" if profile is None else profile.value
    return f"{_SENTINEL_PREFIX}{scope}:{name}:{secrets.token_hex(32)}"


def _profile_expectations(
    profile: ContainmentProfile,
    inputs: _ProbeInputs,
) -> tuple[SentinelExpectation, ...]:
    visible = (
        ("workspace", "challenge")
        if profile is ContainmentProfile.CANDIDATE
        else ("challenge", "grader", "response")
    )
    forbidden = tuple(
        name
        for name in ("workspace", "challenge", "grader", "response", "sibling", "control_plane")
        if name not in visible
    )
    result = [
        SentinelExpectation(
            name=name,
            channel=SentinelChannel.FILE,
            disposition=SentinelDisposition.VISIBLE,
            value_sha256=inputs.canaries[name].value_sha256,
        )
        for name in visible
    ]
    result.extend(
        SentinelExpectation(
            name=name,
            channel=SentinelChannel.FILE,
            disposition=SentinelDisposition.FORBIDDEN,
            value_sha256=inputs.canaries[name].value_sha256,
        )
        for name in forbidden
    )
    env = inputs.canaries[f"{profile.value}_environment"]
    result.append(
        SentinelExpectation(
            name=env.name,
            channel=SentinelChannel.ENVIRONMENT,
            disposition=SentinelDisposition.FORBIDDEN,
            value_sha256=env.value_sha256,
        )
    )
    return tuple(result)


def _validate_expectation_layout(
    profile: ContainmentProfile,
    expectations: tuple[SentinelExpectation, ...],
) -> None:
    if profile is ContainmentProfile.CANDIDATE:
        expected_layout = (
            ("workspace", SentinelChannel.FILE, SentinelDisposition.VISIBLE),
            ("challenge", SentinelChannel.FILE, SentinelDisposition.VISIBLE),
            ("grader", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            ("response", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            ("sibling", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            ("control_plane", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            (
                "candidate_environment",
                SentinelChannel.ENVIRONMENT,
                SentinelDisposition.FORBIDDEN,
            ),
        )
    else:
        expected_layout = (
            ("challenge", SentinelChannel.FILE, SentinelDisposition.VISIBLE),
            ("grader", SentinelChannel.FILE, SentinelDisposition.VISIBLE),
            ("response", SentinelChannel.FILE, SentinelDisposition.VISIBLE),
            ("workspace", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            ("sibling", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            ("control_plane", SentinelChannel.FILE, SentinelDisposition.FORBIDDEN),
            (
                "scorer_environment",
                SentinelChannel.ENVIRONMENT,
                SentinelDisposition.FORBIDDEN,
            ),
        )
    actual_layout = tuple((item.name, item.channel, item.disposition) for item in expectations)
    if actual_layout != expected_layout:
        raise ValueError("containment canaries must use the fixed phase layout")
    hashes = tuple(item.value_sha256 for item in expectations)
    if len(hashes) != len(set(hashes)):
        raise ValueError("containment canary identities must be unique")


def _validate_expected_environment_layout(
    profile: ContainmentProfile,
    expected_environment: tuple[EnvironmentValue, ...],
) -> None:
    binding_names = (
        ()
        if profile is ContainmentProfile.CANDIDATE
        else (
            "GUILDMIND_CHALLENGE_SHA256",
            "GUILDMIND_EVALUATOR_VERSION",
            "GUILDMIND_EXPECTED_TESTS",
            "GUILDMIND_IMAGE_DIGEST",
            "GUILDMIND_LIMITS_SHA256",
            "GUILDMIND_ORACLE_SHA256",
            "GUILDMIND_PATCH_SHA256",
            "GUILDMIND_RESPONSE_SHA256",
            "GUILDMIND_SOURCE_SHA256",
            "GUILDMIND_TASK_CONTENT_HASH",
            "GUILDMIND_TASK_ID",
        )
    )
    expected_names = tuple(sorted((*_BASE_ENVIRONMENT, *binding_names)))
    actual_names = tuple(item.name for item in expected_environment)
    if actual_names != expected_names:
        raise ValueError("expected environment must use the fixed phase allowlist")
    actual = {item.name: item.value_sha256 for item in expected_environment}
    for item in _environment_hashes(_BASE_ENVIRONMENT):
        if actual[item.name] != item.value_sha256:
            raise ValueError("base image environment identity does not match the pinned profile")


def _validate_profile_environment_bindings(
    profile: ContainmentProfile,
    *,
    expectations: tuple[SentinelExpectation, ...],
    expected_environment: tuple[EnvironmentValue, ...],
) -> None:
    if profile is ContainmentProfile.CANDIDATE:
        return
    expected = {item.name: item.value_sha256 for item in expectations}
    environment = {item.name: item.value_sha256 for item in expected_environment}

    def environment_hash(value: str) -> str:
        return sha256_bytes(value.encode("utf-8"))

    bindings = {
        "GUILDMIND_CHALLENGE_SHA256": environment_hash(expected["challenge"]),
        "GUILDMIND_EVALUATOR_VERSION": environment_hash("guildmind/container-python-call-v2"),
        "GUILDMIND_EXPECTED_TESTS": environment_hash("1"),
        "GUILDMIND_LIMITS_SHA256": environment_hash(
            canonical_sha256(_limits_dict(candidate_resource_limits()))
        ),
        "GUILDMIND_ORACLE_SHA256": environment_hash(expected["grader"]),
        "GUILDMIND_PATCH_SHA256": environment_hash(sha256_bytes(b"containment-probe-patch")),
        "GUILDMIND_RESPONSE_SHA256": environment_hash(expected["response"]),
        "GUILDMIND_SOURCE_SHA256": environment_hash(sha256_bytes(b"containment-probe-source")),
        "GUILDMIND_TASK_CONTENT_HASH": environment_hash(sha256_bytes(b"containment-probe-task")),
        "GUILDMIND_TASK_ID": environment_hash("containment-probe"),
    }
    if any(environment[name] != value for name, value in bindings.items()):
        raise ValueError("scorer environment bindings do not match retained canary evidence")


@contextmanager
def _planted_environment(name: str, value: str) -> Iterator[None]:
    with _ENVIRONMENT_LOCK:
        prior = os.environ.get(name)
        existed = name in os.environ
        os.environ[name] = value
        try:
            yield
        finally:
            if existed and prior is not None:
                os.environ[name] = prior
            else:
                os.environ.pop(name, None)


def _host_evidence(
    sandbox: DockerSandbox,
    assessment: DockerHostAssessment,
    *,
    image: str,
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
        image_reference=image,
        image_id=image_id,
    )


def _environment_hashes(values: Mapping[str, str]) -> tuple[EnvironmentValue, ...]:
    return tuple(
        EnvironmentValue(name=name, value_sha256=sha256_bytes(value.encode("utf-8")))
        for name, value in sorted(values.items())
    )


def _limits_dict(limits: SandboxLimits) -> dict[str, int | float]:
    return {
        "cpu_cores": limits.cpu_cores,
        "memory_bytes": limits.memory_bytes,
        "output_bytes": limits.output_bytes,
        "pids": limits.pids,
        "temporary_bytes": limits.temporary_bytes,
        "wall_time_seconds": limits.wall_time_seconds,
        "workspace_bytes": limits.workspace_bytes,
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


def _single_json_object(data: bytes) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("containment probe output is not UTF-8") from error
    if not text.endswith("\n") or text.count("\n") != 1:
        raise ValueError("containment probe output must be exactly one LF-terminated JSON record")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate JSON field: {key}")
            value[key] = item
        return value

    raw = json.loads(text, object_pairs_hook=reject_duplicates)
    if not isinstance(raw, dict):
        raise ValueError("containment probe output must be a JSON object")
    return cast(dict[str, object], raw)
