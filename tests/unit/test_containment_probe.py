from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

import guildmind.sandbox.containment_probe as containment
from guildmind.domain import sha256_bytes
from guildmind.evaluation import ContainerEvaluator, load_fixture
from guildmind.sandbox.base import SandboxRequest, SandboxResult, SandboxStatus
from guildmind.sandbox.docker import (
    DockerCleanupEvidence,
    DockerContainerState,
    DockerExecutionEvidence,
    DockerHostAssessment,
    DockerHostPolicy,
    DockerKillEvidence,
    DockerSandbox,
    ObservedSandboxRun,
)

_IMAGE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"
_IMAGE_ID = f"sha256:{'b' * 64}"
_CONTAINER_ID = "c" * 64
_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _environment_payload(request: SandboxRequest) -> list[dict[str, str]]:
    values = {**containment._BASE_ENVIRONMENT, **request.environment}
    return [
        {"name": name, "value_sha256": sha256_bytes(value.encode())}
        for name, value in sorted(values.items())
    ]


def _visible_file_hashes(request: SandboxRequest) -> tuple[list[str], int]:
    hashes: list[str] = []
    files_examined = 0
    for mount in request.mounts:
        sources = sorted(mount.source.rglob("*")) if mount.source.is_dir() else [mount.source]
        for source in sources:
            if not source.is_file():
                continue
            files_examined += 1
            data = source.read_bytes()
            if containment._SENTINEL_PREFIX.encode() in data:
                hashes.append(sha256_bytes(data))
    return sorted(set(hashes)), files_examined


def _mount_payload(
    request: SandboxRequest, *, writable: str | None = None
) -> list[dict[str, object]]:
    target_by_path = {
        "/inputs/workspace": "workspace",
        "/inputs/challenge.json": "challenge",
        "/inputs/grader": "grader",
        "/inputs/response.txt": "response",
    }
    present = {target_by_path[mount.target] for mount in request.mounts}
    result: list[dict[str, object]] = []
    for target in containment._MOUNT_ORDER:
        is_present = target in present
        write_succeeded = target == writable
        result.append(
            {
                "present": is_present,
                "read_only": False if write_succeeded else (True if is_present else None),
                "target": target,
                "write_errno": None if write_succeeded or not is_present else 30,
                "write_outcome": (
                    "succeeded" if write_succeeded else ("denied" if is_present else "absent")
                ),
            }
        )
    return result


def _network_payload(
    *,
    weak_external: bool = False,
    interface_scan_error: bool = False,
    route_scan_error: bool = False,
) -> dict[str, object]:
    tcp: list[dict[str, object]] = []
    for target in containment._TCP_ORDER:
        loopback = target.startswith("loopback_")
        tcp.append(
            {
                "errno": 111 if loopback or weak_external else 101,
                "outcome": "os_error",
                "target": target,
            }
        )
    return {
        "default_route_families": [],
        "dns": [
            {"error_code": -3, "outcome": "gai_error", "target": target}
            for target in containment._DNS_ORDER
        ],
        "packet_socket": {"errno": 1, "outcome": "os_error"},
        "interface_scan_error": interface_scan_error,
        "proc_net_unix_entries": 0,
        "proc_net_unix_scan_error": False,
        "raw_socket": {"errno": 1, "outcome": "os_error"},
        "route_scan_error": route_scan_error,
        "socket_inventory": [
            {"root": root, "scan_error": False, "socket_count": 0}
            for root in containment._SOCKET_ROOT_ORDER
        ],
        "tcp": tcp,
        "unix": [
            {
                "errno": None,
                "is_socket": False,
                "outcome": "absent",
                "present": False,
                "target": target,
            }
            for target in containment._UNIX_ORDER
        ],
        "usable_non_loopback_interfaces": [],
    }


def _credential_payload() -> list[dict[str, object]]:
    return [
        {
            "errno": 13 if target.startswith("root_") else None,
            "outcome": "inaccessible" if target.startswith("root_") else "absent",
            "readable": False,
            "target": target,
        }
        for target in containment._CREDENTIAL_ORDER
    ]


def _observation_payload(
    request: SandboxRequest,
    *,
    leak_forbidden_file: bool = False,
    leak_environment: bool = False,
    weak_external: bool = False,
    writable_mount: str | None = None,
    interface_scan_error: bool = False,
    route_scan_error: bool = False,
) -> dict[str, object]:
    profile = request.argv[-1]
    file_hashes, files_examined = _visible_file_hashes(request)
    if leak_forbidden_file and profile == "candidate":
        root = request.mounts[0].source.parent
        file_hashes.append(sha256_bytes((root / "grader" / "oracle.json").read_bytes()))
        file_hashes.sort()
    environment = _environment_payload(request)
    environment_sentinels: list[dict[str, str]] = []
    if leak_environment:
        value = os.environ[containment._HOST_ENV_SENTINEL]
        record = {
            "name": containment._HOST_ENV_SENTINEL,
            "value_sha256": sha256_bytes(value.encode()),
        }
        environment.append(record)
        environment.sort(key=lambda item: item["name"])
        environment_sentinels.append(record)
    return {
        "credentials": _credential_payload(),
        "environment": environment,
        "mountinfo_complete": True,
        "mounts": _mount_payload(request, writable=writable_mount),
        "network": _network_payload(
            weak_external=weak_external,
            interface_scan_error=interface_scan_error,
            route_scan_error=route_scan_error,
        ),
        "profile": profile,
        "program_sha256": containment._EXPECTED_PROGRAM_SHA256,
        "schema_version": "guildmind.containment-probe/v1",
        "sentinels": {
            "environment": environment_sentinels,
            "file_sha256": file_hashes,
            "files_examined": files_examined,
            "scan_errors": 0,
            "scan_truncated": False,
        },
        "unexpected_input_mounts": [],
    }


def _observed(
    request: SandboxRequest,
    stdout: bytes,
    *,
    cleanup_confirmed: bool = True,
    hostile_diagnostic: str | None = None,
) -> ObservedSandboxRun:
    cleanup = DockerCleanupEvidence(
        target=_CONTAINER_ID,
        removal_attempted=True,
        removal_succeeded=cleanup_confirmed,
        absence_checked=True,
        absent=cleanup_confirmed,
        diagnostic=(
            hostile_diagnostic
            if hostile_diagnostic is not None
            else (None if cleanup_confirmed else "remove rejected")
        ),
    )
    prior = SandboxResult(
        execution_id=request.execution_id,
        status=SandboxStatus.EXITED,
        exit_code=0,
        stdout=stdout,
        container_id=_CONTAINER_ID,
        image_id=_IMAGE_ID,
        diagnostic=hostile_diagnostic,
    )
    result = prior
    if not cleanup_confirmed:
        result = SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.INFRASTRUCTURE_ERROR,
            exit_code=None,
            stdout=stdout,
            container_id=_CONTAINER_ID,
            image_id=_IMAGE_ID,
            diagnostic="container cleanup failed: remove rejected",
        )
    return ObservedSandboxRun(
        result=result,
        evidence=DockerExecutionEvidence(
            container_id=_CONTAINER_ID,
            image_id=_IMAGE_ID,
            state=DockerContainerState(
                exit_code=0,
                oom_killed=False,
                running=False,
                error=hostile_diagnostic or "",
            ),
            termination_trigger=None,
            kill=DockerKillEvidence(
                attempted=False,
                succeeded=None,
                diagnostic=hostile_diagnostic,
            ),
            cleanup=cleanup,
            pre_cleanup_status=prior.status,
        ),
    )


def _dispatch_failure(request: SandboxRequest, *, diagnostic: str) -> ObservedSandboxRun:
    return ObservedSandboxRun(
        result=SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.INFRASTRUCTURE_ERROR,
            exit_code=None,
            diagnostic=diagnostic,
        ),
        evidence=DockerExecutionEvidence(
            container_id=None,
            image_id=_IMAGE_ID,
            state=None,
            termination_trigger=None,
            kill=DockerKillEvidence(attempted=False, succeeded=None, diagnostic=diagnostic),
            cleanup=DockerCleanupEvidence(
                target=None,
                removal_attempted=False,
                removal_succeeded=None,
                absence_checked=False,
                absent=None,
                diagnostic=diagnostic,
            ),
            pre_cleanup_status=SandboxStatus.INFRASTRUCTURE_ERROR,
        ),
    )


class _FakeDockerSandbox:
    def __init__(
        self,
        *,
        leak_forbidden_file: bool = False,
        leak_environment: bool = False,
        malformed_profile: str | None = None,
        malformed_secret: str | None = None,
        weak_external: bool = False,
        writable_mount: str | None = None,
        interface_scan_error: bool = False,
        route_scan_error: bool = False,
        cleanup_confirmed: bool = True,
        dispatch_failure: bool = False,
        hostile_diagnostic: str | None = None,
        reference: bool = False,
    ) -> None:
        self.host_policy = DockerHostPolicy() if reference else DockerHostPolicy.development_only()
        self.leak_forbidden_file = leak_forbidden_file
        self.leak_environment = leak_environment
        self.malformed_profile = malformed_profile
        self.malformed_secret = malformed_secret
        self.weak_external = weak_external
        self.writable_mount = writable_mount
        self.interface_scan_error = interface_scan_error
        self.route_scan_error = route_scan_error
        self.cleanup_confirmed = cleanup_confirmed
        self.dispatch_failure = dispatch_failure
        self.hostile_diagnostic = hostile_diagnostic
        self.reference = reference
        self.requests: list[SandboxRequest] = []
        self.ambient_sentinel_seen: list[bool] = []

    def assess_host(self) -> DockerHostAssessment:
        if self.reference:
            return DockerHostAssessment(
                accepted=True,
                reference_ready=True,
                failures=(),
                warnings=(),
            )
        return DockerHostAssessment(
            accepted=True,
            reference_ready=False,
            failures=(),
            warnings=("architecture_not_x86_64", "rootless_required"),
        )

    def verify_image(self, reference: str) -> str:
        assert reference == _IMAGE
        return _IMAGE_ID

    def run_observed(self, request: SandboxRequest) -> ObservedSandboxRun:
        self.requests.append(request)
        ambient = os.environ.get(containment._HOST_ENV_SENTINEL, "")
        self.ambient_sentinel_seen.append(ambient.startswith(containment._SENTINEL_PREFIX))
        if self.dispatch_failure:
            return _dispatch_failure(
                request,
                diagnostic=self.hostile_diagnostic or "docker create failed",
            )
        if self.malformed_profile == request.argv[-1]:
            stdout = _json_line(
                {
                    "hostile_unexpected_value": self.malformed_secret or "malformed",
                    "schema_version": "guildmind.containment-probe/v1",
                }
            )
        else:
            stdout = _json_line(
                _observation_payload(
                    request,
                    leak_forbidden_file=self.leak_forbidden_file,
                    leak_environment=self.leak_environment,
                    weak_external=self.weak_external,
                    writable_mount=self.writable_mount,
                    interface_scan_error=self.interface_scan_error,
                    route_scan_error=self.route_scan_error,
                )
            )
        return _observed(
            request,
            stdout,
            cleanup_confirmed=self.cleanup_confirmed,
            hostile_diagnostic=self.hostile_diagnostic,
        )


class _EvaluatorRequestRecorder:
    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            return SandboxResult(
                execution_id=request.execution_id,
                status=SandboxStatus.EXITED,
                exit_code=0,
                stdout=b"opaque request-shape response\n",
            )
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.INFRASTRUCTURE_ERROR,
            exit_code=None,
            diagnostic="request recorder stops after scorer dispatch",
        )


def _run(
    fake: _FakeDockerSandbox,
    *,
    code_revision: str = "abc123",
) -> containment.ContainmentProbeSuiteEvidence:
    return containment.run_containment_probe_suite(
        cast(DockerSandbox, fake),
        image=_IMAGE,
        code_revision=code_revision,
        probe_id="containment-development-001",
        occurred_at=datetime(2026, 8, 2, 14, 0, tzinfo=UTC),
    )


def test_two_production_shaped_profiles_are_contained_without_reference_promotion() -> None:
    fake = _FakeDockerSandbox()

    report = _run(fake)

    assert [profile.verdict for profile in report.profiles] == [
        containment.ContainmentVerdict.CONTAINED,
        containment.ContainmentVerdict.CONTAINED,
    ]
    assert report.all_contained
    assert not report.reference_eligible
    assert not report.reference_passed
    assert fake.ambient_sentinel_seen == [True, True]
    assert containment._HOST_ENV_SENTINEL not in os.environ
    candidate, scorer = fake.requests
    assert candidate.argv == (
        "/usr/local/bin/python",
        "-I",
        "/opt/guildmind/containment_probe.py",
        "candidate",
    )
    assert [mount.target for mount in candidate.mounts] == [
        "/inputs/workspace",
        "/inputs/challenge.json",
    ]
    assert [mount.target for mount in scorer.mounts] == [
        "/inputs/challenge.json",
        "/inputs/grader",
        "/inputs/response.txt",
    ]
    assert not candidate.environment
    assert set(scorer.environment) == {
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
    }
    round_trip = containment.ContainmentProbeSuiteEvidence.model_validate_json(
        report.canonical_bytes()
    )
    assert round_trip == report
    assert round_trip.content_sha256 == report.content_sha256


def test_containment_requests_cannot_drift_from_public_evaluator_requests() -> None:
    containment_sandbox = _FakeDockerSandbox()
    _run(containment_sandbox)
    evaluator_sandbox = _EvaluatorRequestRecorder()

    ContainerEvaluator(sandbox=evaluator_sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert len(containment_sandbox.requests) == 2
    assert len(evaluator_sandbox.requests) == 2
    for containment_request, evaluator_request in zip(
        containment_sandbox.requests,
        evaluator_sandbox.requests,
        strict=True,
    ):
        assert [mount.target for mount in containment_request.mounts] == [
            mount.target for mount in evaluator_request.mounts
        ]
        assert containment_request.limits == evaluator_request.limits
        assert set(containment_request.environment) == set(evaluator_request.environment)
        assert containment_request.working_directory == evaluator_request.working_directory


def test_exact_forbidden_canary_disclosure_is_exposed() -> None:
    report = _run(_FakeDockerSandbox(leak_forbidden_file=True))

    assert report.profiles[0].verdict is containment.ContainmentVerdict.EXPOSED
    assert report.profiles[1].verdict is containment.ContainmentVerdict.CONTAINED
    assert not report.all_contained
    leaked = next(check for check in report.profiles[0].checks if check.name == "sentinel_grader")
    assert leaked.outcome is containment.CheckOutcome.EXPOSURE


def test_ambient_environment_disclosure_is_exposed_without_recording_raw_value() -> None:
    report = _run(_FakeDockerSandbox(leak_environment=True))

    assert all(
        profile.verdict is containment.ContainmentVerdict.EXPOSED for profile in report.profiles
    )
    encoded = report.canonical_bytes()
    assert containment._SENTINEL_PREFIX.encode() not in encoded
    assert b"environment_unexpected_names" in encoded


def test_writable_expected_mount_is_explicit_exposure() -> None:
    report = _run(_FakeDockerSandbox(writable_mount="challenge"))

    assert all(
        profile.verdict is containment.ContainmentVerdict.EXPOSED for profile in report.profiles
    )
    check = next(
        item for item in report.profiles[0].checks if item.name == "mount_challenge_active_write"
    )
    assert check.outcome is containment.CheckOutcome.EXPOSURE


def test_connection_refused_is_not_strong_nonloopback_containment_evidence() -> None:
    report = _run(_FakeDockerSandbox(weak_external=True))

    assert all(
        profile.verdict is containment.ContainmentVerdict.INCONCLUSIVE
        for profile in report.profiles
    )
    check = next(item for item in report.profiles[0].checks if item.name == "tcp_external_ipv4")
    assert check.outcome is containment.CheckOutcome.INCONCLUSIVE


@pytest.mark.parametrize(
    ("interface_error", "route_error", "check_name"),
    [
        (True, False, "usable_non_loopback_interfaces"),
        (False, True, "default_routes"),
    ],
)
def test_network_inventory_scan_errors_are_inconclusive(
    interface_error: bool,
    route_error: bool,
    check_name: str,
) -> None:
    report = _run(
        _FakeDockerSandbox(
            interface_scan_error=interface_error,
            route_scan_error=route_error,
        )
    )

    assert all(
        profile.verdict is containment.ContainmentVerdict.INCONCLUSIVE
        for profile in report.profiles
    )
    check = next(item for item in report.profiles[0].checks if item.name == check_name)
    assert check.outcome is containment.CheckOutcome.INCONCLUSIVE


def test_malformed_observation_and_cleanup_failure_are_inconclusive() -> None:
    malformed = _run(_FakeDockerSandbox(malformed_profile="candidate"))
    cleanup = _run(_FakeDockerSandbox(cleanup_confirmed=False))

    assert malformed.profiles[0].verdict is containment.ContainmentVerdict.INCONCLUSIVE
    assert (
        malformed.profiles[0].execution.error_stage is containment.ExecutionErrorStage.OBSERVATION
    )
    assert all(
        profile.verdict is containment.ContainmentVerdict.INCONCLUSIVE
        for profile in cleanup.profiles
    )
    assert all(
        profile.execution.error_stage is containment.ExecutionErrorStage.CLEANUP
        for profile in cleanup.profiles
    )


def test_pre_create_failure_is_dispatch_not_cleanup() -> None:
    report = _run(_FakeDockerSandbox(dispatch_failure=True))

    for profile in report.profiles:
        assert profile.verdict is containment.ContainmentVerdict.INCONCLUSIVE
        assert profile.execution.error_stage is containment.ExecutionErrorStage.DISPATCH
        assert profile.execution.cleanup.target is None
        assert not profile.execution.cleanup.removal_attempted


def test_adapter_diagnostics_are_hashed_without_reflecting_host_paths() -> None:
    hostile = "/Users/private-owner/.docker/run/docker.sock: permission denied"
    expected_hash = sha256_bytes(hostile.encode())
    report = _run(_FakeDockerSandbox(hostile_diagnostic=hostile))

    assert hostile.encode() not in report.canonical_bytes()
    for profile in report.profiles:
        execution = profile.execution
        assert execution.diagnostic is None
        assert execution.adapter_diagnostic_sha256 == expected_hash
        assert execution.state is not None
        assert execution.state.error_sha256 == expected_hash
        assert execution.kill.diagnostic_sha256 == expected_hash
        assert execution.cleanup.diagnostic_sha256 == expected_hash
        assert profile.verdict is containment.ContainmentVerdict.INCONCLUSIVE


def test_malformed_observation_never_reflects_hostile_values_into_evidence() -> None:
    secret = "guildmind-containment-v1:hostile:do-not-reflect:0123456789abcdef"
    report = _run(
        _FakeDockerSandbox(
            malformed_profile="candidate",
            malformed_secret=secret,
        )
    )

    candidate = report.profiles[0]
    assert candidate.execution.diagnostic == "malformed containment observation"
    assert secret.encode() not in report.canonical_bytes()
    assert candidate.execution.transcript.stdout_bytes > 0
    assert candidate.execution.transcript.stdout_sha256 != sha256_bytes(b"")


def test_reference_claim_requires_reference_host_and_clean_revision() -> None:
    clean = _run(_FakeDockerSandbox(reference=True), code_revision="f" * 40)
    dirty = _run(_FakeDockerSandbox(reference=True), code_revision="f" * 40 + "+dirty")

    assert clean.reference_eligible
    assert clean.reference_passed
    assert not dirty.reference_eligible
    assert not dirty.reference_passed


def test_strict_parser_rejects_duplicate_fields_and_extra_fields() -> None:
    duplicate = (
        b'{"schema_version":"guildmind.containment-probe/v1",'
        b'"profile":"candidate","profile":"scorer"}\n'
    )

    try:
        containment.parse_containment_observation(duplicate)
    except ValueError as error:
        assert "duplicate JSON field: profile" in str(error)
    else:
        raise AssertionError("duplicate image output field was accepted")

    fake = _FakeDockerSandbox()
    request_report = _run(fake)
    payload = request_report.profiles[0].observation
    assert payload is not None
    raw = payload.model_dump(mode="json")
    raw["unexpected"] = True
    try:
        containment.ContainmentObservation.model_validate(raw)
    except ValidationError as error:
        assert "extra_forbidden" in str(error)
    else:
        raise AssertionError("extra image output field was accepted")


@pytest.mark.parametrize(
    ("field_path", "replacement"),
    [
        (("network", "raw_socket", "errno"), True),
        (("network", "packet_socket", "errno"), True),
        (("mounts", 0, "present"), 1),
        (("mounts", 0, "read_only"), 1),
        (("mounts", 0, "write_errno"), True),
        (("network", "proc_net_unix_entries"), True),
    ],
)
def test_observation_parser_rejects_bool_integer_coercion(
    field_path: tuple[str | int, ...],
    replacement: object,
) -> None:
    report = _run(_FakeDockerSandbox())
    observation = report.profiles[0].observation
    assert observation is not None
    payload = observation.model_dump(mode="json")
    target: object = payload
    for part in field_path[:-1]:
        target = target[part]  # type: ignore[index]
    target[field_path[-1]] = replacement  # type: ignore[index]

    with pytest.raises(ValidationError):
        containment.parse_containment_observation(_json_line(payload))


def test_profile_claims_cannot_be_promoted_by_replacing_checks_and_observation() -> None:
    report = _run(
        _FakeDockerSandbox(weak_external=True, reference=True),
        code_revision="f" * 40,
    )
    payload = report.model_dump(mode="json")
    for profile in payload["profiles"]:
        profile["observation"] = None
        profile["checks"] = [
            {
                "expected": True,
                "name": "forged-pass",
                "observed": True,
                "outcome": "pass",
                "source": "forged",
            }
        ]
        profile["verdict"] = "contained"
        profile["diagnostic"] = None
    payload["all_contained"] = True
    payload["reference_passed"] = True

    try:
        containment.ContainmentProbeSuiteEvidence.model_validate_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )
    except ValidationError as error:
        assert "containment checks must be exactly derived" in str(error)
    else:
        raise AssertionError("forged checks and missing observations became a reference pass")


def test_observation_and_checks_from_another_run_cannot_replace_retained_evidence() -> None:
    weak = _run(_FakeDockerSandbox(weak_external=True))
    clean = _run(_FakeDockerSandbox())
    payload = weak.model_dump(mode="json")
    clean_profile = clean.model_dump(mode="json")["profiles"][0]
    payload["profiles"][0]["observation"] = clean_profile["observation"]
    payload["profiles"][0]["checks"] = clean_profile["checks"]
    payload["profiles"][0]["verdict"] = "contained"
    payload["profiles"][0]["diagnostic"] = None

    with pytest.raises(ValidationError, match="exactly derived"):
        containment.ContainmentProbeSuiteEvidence.model_validate_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )


def test_fixed_canary_layout_cannot_be_relabelled() -> None:
    report = _run(_FakeDockerSandbox())
    payload = report.model_dump(mode="json")
    payload["profiles"][0]["expectations"][0]["name"] = "renamed"

    with pytest.raises(ValidationError, match="fixed phase layout"):
        containment.ContainmentProbeSuiteEvidence.model_validate_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )
