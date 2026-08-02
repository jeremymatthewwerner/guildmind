from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from guildmind.evaluation import ContainerEvaluator, load_fixture
from guildmind.sandbox import (
    ContainmentPhase,
    ContainmentVerdict,
    DockerHostPolicy,
    DockerSandbox,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    run_containment_probe_suite,
)
from guildmind.sandbox.containment_probe import ExecutionStatus
from guildmind.sandbox.docker import ObservedSandboxRun

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


class _RecordingDockerSandbox(DockerSandbox):
    def __init__(self, *, host_policy: DockerHostPolicy) -> None:
        super().__init__(host_policy=host_policy)
        self.requests: list[SandboxRequest] = []

    def run_observed(
        self,
        request: SandboxRequest,
        *,
        verify_cleanup: bool = True,
    ) -> ObservedSandboxRun:
        self.requests.append(request)
        return super().run_observed(request, verify_cleanup=verify_cleanup)


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
                stdout=b"opaque containment shape response\n",
            )
        return SandboxResult(
            execution_id=request.execution_id,
            status=SandboxStatus.INFRASTRUCTURE_ERROR,
            exit_code=None,
            diagnostic="request-shape recorder stops after scorer dispatch",
        )


def _configured_sandbox() -> tuple[_RecordingDockerSandbox, str, bool]:
    reference_image = os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE")
    if reference_image is not None:
        return _RecordingDockerSandbox(host_policy=DockerHostPolicy()), reference_image, True
    development_image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if development_image is not None:
        return (
            _RecordingDockerSandbox(host_policy=DockerHostPolicy.development_only()),
            development_image,
            False,
        )
    pytest.skip("no digest-pinned Guildmind evaluator image is configured")


def _head_revision() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        text=True,
        timeout=5,
    )
    revision = completed.stdout.strip()
    assert len(revision) == 40
    assert all(character in "0123456789abcdef" for character in revision)
    return revision


@pytest.mark.container
def test_live_containment_suite_and_evaluator_request_shapes_agree() -> None:
    sandbox, image, reference = _configured_sandbox()
    head_revision = _head_revision()
    code_revision = head_revision if reference else f"{head_revision}+dirty"

    report = run_containment_probe_suite(
        sandbox,
        image=image,
        code_revision=code_revision,
        probe_id="containment-live-integration",
    )

    assert [profile.profile for profile in report.profiles] == [
        ContainmentPhase.CANDIDATE,
        ContainmentPhase.SCORER,
    ]
    assert all(profile.verdict is ContainmentVerdict.CONTAINED for profile in report.profiles)
    assert report.all_contained
    for profile in report.profiles:
        execution = profile.execution
        assert execution.status is ExecutionStatus.COMPLETED
        assert execution.pre_cleanup_status is SandboxStatus.EXITED
        assert execution.sandbox_status is SandboxStatus.EXITED
        assert execution.exit_code == 0
        assert not execution.output_truncated
        assert execution.cleanup.confirmed

    if reference:
        assert report.reference_eligible
        assert report.reference_passed
    else:
        assert not report.reference_eligible
        assert not report.reference_passed

    assert len(sandbox.requests) == 2
    recorder = _EvaluatorRequestRecorder()
    ContainerEvaluator(sandbox=recorder, image=image).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )
    assert len(recorder.requests) == 2

    for containment_request, evaluator_request in zip(
        sandbox.requests,
        recorder.requests,
        strict=True,
    ):
        assert [mount.target for mount in containment_request.mounts] == [
            mount.target for mount in evaluator_request.mounts
        ]
        assert containment_request.limits == evaluator_request.limits
        assert set(containment_request.environment) == set(evaluator_request.environment)
        assert containment_request.working_directory == evaluator_request.working_directory
