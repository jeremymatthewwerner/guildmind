from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError

from guildmind.sandbox import (
    ConfigurationVerdict,
    DockerCleanupEvidence,
    DockerContainerState,
    DockerExecutionEvidence,
    DockerHostAssessment,
    DockerHostPolicy,
    DockerKillEvidence,
    DockerSandbox,
    EnforcementVerdict,
    EvidenceTier,
    ObservedSandboxRun,
    ResourceProbeSuiteEvidence,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    run_resource_probe_suite,
)

_IMAGE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"
_IMAGE_ID = f"sha256:{'b' * 64}"
_CONTAINER_ID = "c" * 64


def _json_line(value: dict[str, object]) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()


def _limits_output() -> bytes:
    return _json_line(
        {
            "cpu_max": "100000 100000",
            "memory_max": "268435456",
            "memory_swap_max": "0",
            "pids_current": 2,
            "pids_events": {"max": 0},
            "pids_events_local": {"max": 0},
            "pids_max": "64",
            "probe": "limits",
            "program_sha256": "6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7",
            "schema_version": "guildmind.resource-probe/v1",
        }
    )


def _pids_output() -> bytes:
    return _json_line(
        {
            "baseline_current": 2,
            "children_started": 62,
            "events_after": {"max": 1},
            "events_before": {"max": 0},
            "events_local_after": {"max": 1},
            "events_local_before": {"max": 0},
            "final_current": 2,
            "fork_attempts": 96,
            "fork_errno": 11,
            "fork_error": "EAGAIN",
            "pids_max": "64",
            "pids_peak": 64,
            "pressure_current": 64,
            "probe": "pids",
            "schema_version": "guildmind.resource-probe/v1",
        }
    )


def _disk_output() -> bytes:
    mounts: list[dict[str, object]] = []
    for path, configured in (("/workspace", 67_108_864), ("/tmp", 16_777_216)):
        mounts.append(
            {
                "bytes_written": configured,
                "configured_bytes": configured,
                "failure_errno": 28,
                "path": path,
                "stat_free_after_cleanup": configured,
                "stat_free_at_failure": 0,
                "stat_free_before": configured,
                "stat_total_before": configured,
            }
        )
    return _json_line(
        {
            "mounts": mounts,
            "probe": "disk",
            "schema_version": "guildmind.resource-probe/v1",
        }
    )


def _observed(
    request: SandboxRequest,
    *,
    status: SandboxStatus = SandboxStatus.EXITED,
    exit_code: int = 0,
    stdout: bytes = b"",
    oom_killed: bool = False,
    cleanup_confirmed: bool = True,
) -> ObservedSandboxRun:
    cleanup = DockerCleanupEvidence(
        target=_CONTAINER_ID,
        removal_attempted=True,
        removal_succeeded=cleanup_confirmed,
        absence_checked=True,
        absent=cleanup_confirmed,
        diagnostic=None if cleanup_confirmed else "remove rejected",
    )
    prior_result = SandboxResult(
        execution_id=request.execution_id,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        container_id=_CONTAINER_ID,
        image_id=_IMAGE_ID,
    )
    result = prior_result
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
                exit_code=exit_code,
                oom_killed=oom_killed,
                running=False,
                error="",
            ),
            termination_trigger=None,
            kill=DockerKillEvidence(attempted=False, succeeded=None),
            cleanup=cleanup,
            pre_cleanup_status=prior_result.status,
        ),
    )


class _FakeDockerSandbox:
    def __init__(
        self,
        *,
        pids_output: bytes | None = None,
        disk_output: bytes | None = None,
        memory_status: SandboxStatus = SandboxStatus.OOM_KILLED,
        cleanup_confirmed: bool = True,
        reference: bool = False,
    ) -> None:
        self.host_policy = DockerHostPolicy() if reference else DockerHostPolicy.development_only()
        self.pids_output = pids_output or _pids_output()
        self.disk_output = disk_output or _disk_output()
        self.memory_status = memory_status
        self.cleanup_confirmed = cleanup_confirmed
        self.reference = reference
        self.requests: list[SandboxRequest] = []

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
        command = request.argv[-1]
        if command == "limits":
            return _observed(request, stdout=_limits_output())
        if command == "memory":
            if self.memory_status is SandboxStatus.OOM_KILLED:
                return _observed(
                    request,
                    status=SandboxStatus.OOM_KILLED,
                    exit_code=137,
                    oom_killed=True,
                    cleanup_confirmed=self.cleanup_confirmed,
                )
            return _observed(
                request,
                status=self.memory_status,
                exit_code=137,
                cleanup_confirmed=self.cleanup_confirmed,
            )
        if command == "pids":
            return _observed(request, stdout=self.pids_output)
        if command == "disk":
            return _observed(request, stdout=self.disk_output)
        raise AssertionError(f"unexpected resource probe command: {command}")


def _run(
    fake: _FakeDockerSandbox,
    *,
    code_revision: str = "abc123",
) -> ResourceProbeSuiteEvidence:
    return run_resource_probe_suite(
        cast(DockerSandbox, fake),
        image=_IMAGE,
        code_revision=code_revision,
        probe_id="development-001",
        occurred_at=datetime(2026, 8, 2, 10, 30, tzinfo=UTC),
    )


def test_development_suite_can_observe_enforcement_without_becoming_reference_evidence() -> None:
    fake = _FakeDockerSandbox()

    report = _run(fake)

    assert report.environment.tier is EvidenceTier.DEVELOPMENT
    assert report.configuration.verdict is ConfigurationVerdict.MATCHED
    assert [probe.verdict for probe in report.probes] == [
        EnforcementVerdict.ENFORCED,
        EnforcementVerdict.ENFORCED,
        EnforcementVerdict.ENFORCED,
    ]
    assert report.all_enforced
    assert not report.reference_eligible
    assert not report.reference_passed
    assert [request.argv[-1] for request in fake.requests] == [
        "limits",
        "memory",
        "pids",
        "disk",
    ]
    assert all(request.image == _IMAGE for request in fake.requests)

    round_trip = ResourceProbeSuiteEvidence.model_validate_json(report.canonical_bytes())
    assert round_trip == report
    assert round_trip.content_sha256 == report.content_sha256


def test_generic_timeout_is_inconclusive_memory_evidence() -> None:
    report = _run(_FakeDockerSandbox(memory_status=SandboxStatus.TIMED_OUT))

    memory = report.probes[0]
    assert memory.verdict is EnforcementVerdict.INCONCLUSIVE
    assert not report.all_enforced
    assert memory.diagnostic is not None and "without conclusive" in memory.diagnostic


def test_malformed_pid_record_is_an_observation_error_not_candidate_outcome() -> None:
    report = _run(_FakeDockerSandbox(pids_output=b'{"probe":"pids"}\n'))

    pids = report.probes[1]
    assert pids.verdict is EnforcementVerdict.INCONCLUSIVE
    assert pids.execution.error_stage is not None
    assert pids.execution.error_stage.value == "observation"
    assert pids.diagnostic is not None and "validation errors" in pids.diagnostic


def test_cleanup_failure_overrides_an_observed_oom() -> None:
    report = _run(_FakeDockerSandbox(cleanup_confirmed=False))

    memory = report.probes[0]
    assert memory.execution.pre_cleanup_status is SandboxStatus.OOM_KILLED
    assert memory.execution.sandbox_status is SandboxStatus.INFRASTRUCTURE_ERROR
    assert memory.verdict is EnforcementVerdict.INCONCLUSIVE
    assert not report.all_enforced


def test_optional_pid_observations_do_not_turn_enforcement_into_failure() -> None:
    payload = json.loads(_pids_output())
    payload["events_local_before"] = None
    payload["events_local_after"] = None
    payload["pids_peak"] = None

    report = _run(_FakeDockerSandbox(pids_output=_json_line(payload)))

    pids = report.probes[1]
    assert pids.verdict is EnforcementVerdict.ENFORCED
    assert all("local" not in check.name and "peak" not in check.name for check in pids.checks)


def test_pid_cleanup_anomaly_is_inconclusive_not_not_enforced() -> None:
    payload = json.loads(_pids_output())
    payload["final_current"] = 3

    report = _run(_FakeDockerSandbox(pids_output=_json_line(payload)))

    pids = report.probes[1]
    assert pids.verdict is EnforcementVerdict.INCONCLUSIVE
    assert pids.diagnostic is not None and "children_reaped" in pids.diagnostic


def test_pid_limit_failure_requires_an_explicit_counterexample() -> None:
    payload = json.loads(_pids_output())
    payload.update(
        {
            "children_started": 96,
            "events_after": {"max": 0},
            "events_local_after": {"max": 0},
            "final_current": 2,
            "fork_errno": None,
            "fork_error": None,
            "pids_peak": 98,
            "pressure_current": 98,
        }
    )

    report = _run(_FakeDockerSandbox(pids_output=_json_line(payload)))

    assert report.probes[1].verdict is EnforcementVerdict.NOT_ENFORCED


def test_unrelated_early_fork_error_is_inconclusive() -> None:
    payload = json.loads(_pids_output())
    payload.update(
        {
            "children_started": 10,
            "events_after": {"max": 0},
            "events_local_after": {"max": 0},
            "fork_errno": 1,
            "fork_error": "EPERM",
            "pids_peak": 12,
            "pressure_current": 12,
        }
    )

    report = _run(_FakeDockerSandbox(pids_output=_json_line(payload)))

    assert report.probes[1].verdict is EnforcementVerdict.INCONCLUSIVE


def test_unrelated_disk_error_is_inconclusive() -> None:
    payload = json.loads(_disk_output())
    workspace = payload["mounts"][0]
    workspace["bytes_written"] = 1_048_576
    workspace["failure_errno"] = 5
    workspace["stat_free_at_failure"] = 66_060_288

    report = _run(_FakeDockerSandbox(disk_output=_json_line(payload)))

    assert report.probes[2].verdict is EnforcementVerdict.INCONCLUSIVE


def test_disk_limit_failure_requires_writes_beyond_the_configured_ceiling() -> None:
    payload = json.loads(_disk_output())
    workspace = payload["mounts"][0]
    workspace["bytes_written"] = 69_206_016
    workspace["failure_errno"] = None
    workspace["stat_free_at_failure"] = 1

    report = _run(_FakeDockerSandbox(disk_output=_json_line(payload)))

    assert report.probes[2].verdict is EnforcementVerdict.NOT_ENFORCED


def test_reference_host_with_dirty_revision_is_not_reference_eligible() -> None:
    report = _run(_FakeDockerSandbox(reference=True), code_revision="f" * 40 + "+dirty")

    assert report.environment.tier is EvidenceTier.REFERENCE
    assert report.all_enforced
    assert not report.reference_eligible
    assert not report.reference_passed


def test_development_record_cannot_claim_reference_eligibility() -> None:
    report = _run(_FakeDockerSandbox())
    payload = report.model_dump(mode="json")
    payload["reference_eligible"] = True
    payload["reference_passed"] = True

    try:
        ResourceProbeSuiteEvidence.model_validate(payload)
    except ValidationError as error:
        assert "reference_eligible must be derived" in str(error)
    else:
        raise AssertionError("development evidence was promoted to a reference pass")


def test_resource_evidence_rejects_an_unaccepted_host() -> None:
    report = _run(_FakeDockerSandbox())
    payload = report.model_dump(mode="json")
    payload["environment"]["accepted"] = False

    try:
        ResourceProbeSuiteEvidence.model_validate(payload)
    except ValidationError as error:
        assert "requires an accepted Docker host" in str(error)
    else:
        raise AssertionError("resource evidence accepted a rejected host")
