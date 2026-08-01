from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guildmind.evaluation import ContainerEvaluator, EvaluationStatus, load_fixture
from guildmind.models import ScriptedPatchModel
from guildmind.runtime import DeterministicClock
from guildmind.runtime.runner import FixtureRunner
from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_IMAGE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"
_IMAGE_ID = f"sha256:{'b' * 64}"
_COMPLETION = (
    b'GUILDMIND_EVALUATION_RESULT={"discovered_tests":5,"errors":0,'
    b'"expected_tests":5,"failures":0,'
    b'"schema_version":"guildmind.evaluator-completion/v1","skipped":0,'
    b'"successful":true,"tests_run":5}\n'
)


class FakeSandbox:
    def __init__(self, result: SandboxResult) -> None:
        self.result = result
        self.requests: list[SandboxRequest] = []

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        return SandboxResult(
            execution_id=request.execution_id,
            status=self.result.status,
            exit_code=self.result.exit_code,
            stdout=self.result.stdout,
            stderr=self.result.stderr,
            output_truncated=self.result.output_truncated,
            container_id=self.result.container_id,
            image_id=self.result.image_id,
            diagnostic=self.result.diagnostic,
        )


def sandbox_result(
    status: SandboxStatus = SandboxStatus.EXITED,
    *,
    exit_code: int | None = 0,
    stdout: bytes = _COMPLETION,
    stderr: bytes = b"Ran 5 tests in 0.123s\n\nOK\n",
    output_truncated: bool = False,
) -> SandboxResult:
    return SandboxResult(
        execution_id="placeholder",
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        output_truncated=output_truncated,
        container_id="c" * 64,
        image_id=_IMAGE_ID,
    )


def test_container_evaluator_stages_fresh_read_only_inputs_and_requires_completion() -> None:
    sandbox = FakeSandbox(sandbox_result())
    evaluator = ContainerEvaluator(sandbox=sandbox, image=_IMAGE)

    assert evaluator.evaluator_version == "guildmind/container-fixture-v1"
    assert evaluator.environment_digest == f"sha256:{'a' * 64}"

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.PASSED
    assert result.stdout == ""
    assert "Ran 5 tests in 0.000s" in result.stderr
    assert result.execution["image_reference"] == _IMAGE
    assert result.execution["image_id"] == _IMAGE_ID
    request = sandbox.requests[0]
    assert request.argv == ("/usr/local/bin/python", "-I", "/opt/guildmind/evaluate.py")
    assert request.environment == {"GUILDMIND_EXPECTED_TESTS": "5"}
    assert {mount.target for mount in request.mounts} == {
        "/inputs/grader",
        "/inputs/workspace",
    }
    assert all(mount.source.is_absolute() for mount in request.mounts)


@pytest.mark.parametrize(
    ("stdout", "expected_diagnostic"),
    [
        (b"OK\n", "zero or multiple"),
        (_COMPLETION + _COMPLETION, "zero or multiple"),
        (_COMPLETION + b"spoofed trailing output\n", "was not final"),
        (
            _COMPLETION.replace(b'"tests_run":5', b'"tests_run":4'),
            "discover and run every expected test",
        ),
        (
            _COMPLETION.replace(b'"skipped":0', b'"skipped":1'),
            "contains skipped tests",
        ),
    ],
)
def test_container_evaluator_never_accepts_missing_or_malformed_completion(
    stdout: bytes,
    expected_diagnostic: str,
) -> None:
    sandbox = FakeSandbox(sandbox_result(stdout=stdout))

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert expected_diagnostic in result.stderr


def test_container_evaluator_preserves_image_harness_failure_diagnostic() -> None:
    completion = (
        b'GUILDMIND_EVALUATION_RESULT={"error":"RuntimeError: forbidden Git metadata",'
        b'"schema_version":"guildmind.evaluator-completion/v1","successful":false}\n'
    )
    sandbox = FakeSandbox(sandbox_result(exit_code=2, stdout=completion, stderr=b""))

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert "forbidden Git metadata" in result.stderr


@pytest.mark.parametrize(
    ("sandbox_status", "evaluation_status", "truncated"),
    [
        (SandboxStatus.TIMED_OUT, EvaluationStatus.TIMED_OUT, False),
        (SandboxStatus.OUTPUT_EXHAUSTED, EvaluationStatus.OUTPUT_EXHAUSTED, True),
        (SandboxStatus.OOM_KILLED, EvaluationStatus.OOM_KILLED, False),
        (SandboxStatus.INFRASTRUCTURE_ERROR, EvaluationStatus.INFRASTRUCTURE_ERROR, False),
    ],
)
def test_container_evaluator_preserves_typed_sandbox_failures(
    sandbox_status: SandboxStatus,
    evaluation_status: EvaluationStatus,
    truncated: bool,
) -> None:
    sandbox = FakeSandbox(
        sandbox_result(
            sandbox_status,
            exit_code=None if sandbox_status is SandboxStatus.INFRASTRUCTURE_ERROR else 137,
            stdout=b"",
            output_truncated=truncated,
        )
    )

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is evaluation_status
    assert result.output_truncated is truncated


def test_invalid_patch_is_rejected_before_sandbox_dispatch(tmp_path: Path) -> None:
    patch = tmp_path / "invalid.patch"
    patch.write_bytes(b"not a patch\n")
    sandbox = FakeSandbox(sandbox_result())

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        patch,
    )

    assert result.status is EvaluationStatus.INVALID_PATCH
    assert sandbox.requests == []


@pytest.mark.container
def test_development_container_evaluator_smoke() -> None:
    image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=image,
    )

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.PASSED
    assert result.execution["image_reference"] == image


@pytest.mark.container
def test_development_fixture_runner_records_container_evidence(tmp_path: Path) -> None:
    image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=image,
    )
    runner = FixtureRunner(
        state_directory=tmp_path / "state",
        clock=DeterministicClock(started_at=datetime(2026, 8, 1, tzinfo=UTC)),
        evaluator=evaluator,
    )

    result = runner.run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id="run-development-container",
        code_revision="test-revision",
    )

    assert result.task.image_digest == evaluator.environment_digest
    assert result.manifest.environment_digest == evaluator.environment_digest
    assert result.evaluation.evaluator_version == evaluator.evaluator_version
    execution = result.evaluation.result["execution"]
    assert isinstance(execution, dict)
    assert execution["image_reference"] == image

    replay = FixtureRunner(
        state_directory=tmp_path / "replay-state",
        clock=DeterministicClock(started_at=datetime(2026, 8, 2, tzinfo=UTC)),
        evaluator=evaluator,
    ).run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id="run-development-container-replay",
        code_revision="test-revision",
    )

    assert replay.semantic_digest == result.semantic_digest


@pytest.mark.container
@pytest.mark.xfail(
    strict=True,
    reason=("candidate and unittest currently share one interpreter; Stage 1 gate remains open"),
)
def test_development_evaluator_rejects_candidate_unittest_tampering() -> None:
    image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE is not configured")
    patch = _FIXTURE / "adversarial" / "unittest-tampering.patch"
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=image,
    )

    result = evaluator.evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is not EvaluationStatus.PASSED


@pytest.mark.reference_sandbox
def test_reference_host_container_evaluator_smoke() -> None:
    image = os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_REFERENCE_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(sandbox=DockerSandbox(), image=image)

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.PASSED
    assert result.execution["image_reference"] == image
