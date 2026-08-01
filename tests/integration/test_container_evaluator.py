from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from guildmind.domain import canonical_json, canonical_sha256, sha256_bytes
from guildmind.evaluation import (
    AdversarialCase,
    ContainerEvaluator,
    EvaluationStatus,
    LocalEvaluationResult,
    LocalEvaluationSpec,
    load_adversarial_corpus,
    load_fixture,
)
from guildmind.models import ScriptedPatchModel
from guildmind.runtime import DeterministicClock
from guildmind.runtime.runner import FixtureRunner
from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxMount,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    SandboxUnavailableError,
)
from guildmind.storage import FileArtifactStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_ADVERSARIAL_CORPUS = load_adversarial_corpus(_FIXTURE / "adversarial" / "corpus.json")
_IMAGE = f"registry.example/guildmind/evaluator@sha256:{'a' * 64}"
_IMAGE_ID = f"sha256:{'b' * 64}"
_CANDIDATE_RESPONSE = b"opaque candidate response bytes\n"
_ResultFactory = Callable[[SandboxRequest], SandboxResult]


class FakeSandbox:
    def __init__(
        self,
        *,
        candidate: SandboxResult | None = None,
        scorer: SandboxResult | _ResultFactory | None = None,
    ) -> None:
        self.candidate = candidate or sandbox_result(stdout=_CANDIDATE_RESPONSE, stderr=b"")
        self.scorer = scorer or passing_scorer_result
        self.requests: list[SandboxRequest] = []
        self.mount_snapshots: list[dict[str, dict[str, bytes]]] = []

    def run(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        self.mount_snapshots.append(_snapshot_mounts(request))
        selected: SandboxResult
        if len(self.requests) == 1:
            selected = self.candidate
        elif callable(self.scorer):
            selected = self.scorer(request)
        else:
            selected = self.scorer
        return SandboxResult(
            execution_id=request.execution_id,
            status=selected.status,
            exit_code=selected.exit_code,
            stdout=selected.stdout,
            stderr=selected.stderr,
            output_truncated=selected.output_truncated,
            container_id=selected.container_id,
            image_id=selected.image_id,
            diagnostic=selected.diagnostic,
        )


class CandidateMountLeakEvaluator(ContainerEvaluator):
    def _candidate_request(
        self,
        *,
        spec: LocalEvaluationSpec,
        patch_digest: str,
        patched_workspace: Path,
        challenge_path: Path,
    ) -> SandboxRequest:
        request = super()._candidate_request(
            spec=spec,
            patch_digest=patch_digest,
            patched_workspace=patched_workspace,
            challenge_path=challenge_path,
        )
        mounts = tuple(
            SandboxMount(source=spec.hidden_test_files[0].resolve(), target=mount.target)
            if mount.target == "/inputs/challenge.json"
            else mount
            for mount in request.mounts
        )
        return replace(request, mounts=mounts)


def sandbox_result(
    status: SandboxStatus = SandboxStatus.EXITED,
    *,
    exit_code: int | None = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    output_truncated: bool = False,
    diagnostic: str | None = None,
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
        diagnostic=diagnostic,
    )


def passing_scorer_result(request: SandboxRequest) -> SandboxResult:
    return scorer_result(request, classification="passed")


def scorer_result(
    request: SandboxRequest,
    *,
    classification: str,
    overrides: Mapping[str, object] | None = None,
) -> SandboxResult:
    environment = request.environment
    expected_tests = int(environment["GUILDMIND_EXPECTED_TESTS"])
    binding_payload = {
        "challenge_sha256": environment["GUILDMIND_CHALLENGE_SHA256"],
        "evaluator_version": environment["GUILDMIND_EVALUATOR_VERSION"],
        "expected_tests": expected_tests,
        "image_digest": environment["GUILDMIND_IMAGE_DIGEST"],
        "limits_sha256": environment["GUILDMIND_LIMITS_SHA256"],
        "oracle_sha256": environment["GUILDMIND_ORACLE_SHA256"],
        "patch_sha256": environment["GUILDMIND_PATCH_SHA256"],
        "protocol": "python-call-v1",
        "response_sha256": environment["GUILDMIND_RESPONSE_SHA256"],
        "source_sha256": environment["GUILDMIND_SOURCE_SHA256"],
        "task_content_hash": environment["GUILDMIND_TASK_CONTENT_HASH"],
        "task_id": environment["GUILDMIND_TASK_ID"],
    }
    payload: dict[str, object] = {
        **binding_payload,
        "classification": classification,
        "errors": 0,
        "evaluation_binding_sha256": canonical_sha256(binding_payload),
        "failures": 0,
        "schema_version": "guildmind.evaluator-completion/v2",
        "skipped": 0,
        "successful": classification == "passed",
        "tests_run": expected_tests,
    }
    exit_code = 0
    if classification == "candidate_failed":
        payload["failures"] = 1
        exit_code = 1
    elif classification == "evaluator_error":
        payload["errors"] = 1
        payload["error"] = "trusted scorer failed"
        payload["successful"] = False
        payload["tests_run"] = 0
        exit_code = 2
    if overrides:
        payload.update(overrides)
    completion = f"GUILDMIND_EVALUATION_RESULT={canonical_json(payload)}\n".encode()
    return sandbox_result(exit_code=exit_code, stdout=completion)


def _snapshot_mounts(request: SandboxRequest) -> dict[str, dict[str, bytes]]:
    snapshots: dict[str, dict[str, bytes]] = {}
    for mount in request.mounts:
        if mount.source.is_file():
            snapshots[mount.target] = {mount.source.name: mount.source.read_bytes()}
        else:
            snapshots[mount.target] = {
                path.relative_to(mount.source).as_posix(): path.read_bytes()
                for path in sorted(mount.source.rglob("*"))
                if path.is_file()
            }
    return snapshots


def test_container_evaluator_uses_disjoint_candidate_and_scorer_mounts() -> None:
    sandbox = FakeSandbox()
    evaluator = ContainerEvaluator(sandbox=sandbox, image=_IMAGE)

    assert evaluator.evaluator_version == "guildmind/container-python-call-v2"
    assert evaluator.environment_digest == f"sha256:{'a' * 64}"

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.PASSED
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.raw_candidate_stdout == _CANDIDATE_RESPONSE
    assert result.raw_scorer_stdout is not None
    assert result.raw_scorer_stdout.startswith(b"GUILDMIND_EVALUATION_RESULT=")
    assert result.execution["image_reference"] == _IMAGE
    assert result.execution["image_id"] == _IMAGE_ID
    assert result.execution["protocol"] == "python-call-v1"
    assert len(sandbox.requests) == 2

    candidate, scorer = sandbox.requests
    assert candidate.argv == ("/usr/local/bin/python", "-I", "/opt/guildmind/invoke.py")
    assert candidate.environment == {}
    assert {mount.target for mount in candidate.mounts} == {
        "/inputs/challenge.json",
        "/inputs/workspace",
    }
    assert scorer.argv == ("/usr/local/bin/python", "-I", "/opt/guildmind/score.py")
    assert {mount.target for mount in scorer.mounts} == {
        "/inputs/challenge.json",
        "/inputs/grader",
        "/inputs/response.txt",
    }
    assert candidate.execution_id.endswith("-invoke")
    assert scorer.execution_id.endswith("-score")
    assert candidate.execution_id != scorer.execution_id
    assert all(
        mount.source.is_absolute() for request in sandbox.requests for mount in request.mounts
    )

    candidate_snapshot, scorer_snapshot = sandbox.mount_snapshots
    candidate_bytes = b"".join(
        data for files in candidate_snapshot.values() for data in files.values()
    )
    assert b'"expected"' not in candidate_bytes
    assert b"test_hidden" not in candidate_bytes
    assert "/inputs/grader" not in json.dumps(dict(candidate.environment))
    assert b'"expected"' in b"".join(scorer_snapshot["/inputs/grader"].values())
    assert next(iter(scorer_snapshot["/inputs/response.txt"].values())) == _CANDIDATE_RESPONSE


def test_container_evaluator_ignores_mutation_of_frozen_materialization(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(_FIXTURE, fixture)
    spec = load_fixture(fixture)
    shutil.rmtree(spec.pristine_workspace)
    spec.pristine_workspace.write_bytes(b"corrupted workspace materialization")
    sandbox = FakeSandbox()

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        spec,
        fixture / "solution.patch",
    )

    assert result.status is EvaluationStatus.PASSED
    patched_source = sandbox.mount_snapshots[0]["/inputs/workspace"]["addition.py"]
    assert b'"""Small arithmetic function used by the first deterministic fixture."""' in (
        patched_source
    )
    assert b"return left + right" in patched_source
    assert b"return left - right" not in patched_source


@pytest.mark.parametrize(
    ("stdout", "expected_diagnostic"),
    [
        (b"OK\n", "zero or multiple"),
        (
            b"GUILDMIND_EVALUATION_RESULT={}\nGUILDMIND_EVALUATION_RESULT={}\n",
            "zero or multiple",
        ),
        (b"GUILDMIND_EVALUATION_RESULT={}\ntrailing\n", "was not final"),
        (b"GUILDMIND_EVALUATION_RESULT={not-json}\n", "not valid JSON"),
        (
            b'GUILDMIND_EVALUATION_RESULT={"schema_version":"x","schema_version":"y"}\n',
            "duplicate key",
        ),
        (b"\xff", "not UTF-8"),
    ],
)
def test_malformed_trusted_completion_is_infrastructure_error(
    stdout: bytes,
    expected_diagnostic: str,
) -> None:
    sandbox = FakeSandbox(scorer=sandbox_result(stdout=stdout))

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert expected_diagnostic in result.stderr
    assert result.raw_candidate_stdout == _CANDIDATE_RESPONSE
    assert result.raw_scorer_stdout == stdout


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_trusted_completion_framing_uses_only_ascii_lf(separator: str) -> None:
    def negative(request: SandboxRequest) -> SandboxResult:
        return scorer_result(
            request,
            classification="candidate_failed",
            overrides={"message": f"before{separator}after"},
        )

    result = ContainerEvaluator(sandbox=FakeSandbox(scorer=negative), image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert f"before{separator}after" in result.stderr


@pytest.mark.parametrize(
    "field",
    [
        "task_id",
        "patch_sha256",
        "challenge_sha256",
        "response_sha256",
        "oracle_sha256",
        "image_digest",
        "evaluator_version",
        "limits_sha256",
        "source_sha256",
        "task_content_hash",
        "evaluation_binding_sha256",
        "expected_tests",
    ],
)
def test_trusted_completion_is_bound_to_every_evaluation_identity(field: str) -> None:
    def mismatched(request: SandboxRequest) -> SandboxResult:
        replacement: object = 999 if field == "expected_tests" else "mismatch"
        return scorer_result(request, classification="passed", overrides={field: replacement})

    result = ContainerEvaluator(sandbox=FakeSandbox(scorer=mismatched), image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert f"mismatched {field}" in result.stderr


@pytest.mark.parametrize(
    ("sandbox_status", "evaluation_status", "truncated"),
    [
        (SandboxStatus.TIMED_OUT, EvaluationStatus.TIMED_OUT, False),
        (SandboxStatus.OUTPUT_EXHAUSTED, EvaluationStatus.OUTPUT_EXHAUSTED, True),
        (SandboxStatus.OOM_KILLED, EvaluationStatus.OOM_KILLED, False),
        (SandboxStatus.INFRASTRUCTURE_ERROR, EvaluationStatus.INFRASTRUCTURE_ERROR, False),
    ],
)
def test_candidate_phase_preserves_typed_sandbox_failures_and_does_not_score(
    sandbox_status: SandboxStatus,
    evaluation_status: EvaluationStatus,
    truncated: bool,
) -> None:
    candidate = sandbox_result(
        sandbox_status,
        exit_code=None if sandbox_status is SandboxStatus.INFRASTRUCTURE_ERROR else 137,
        output_truncated=truncated,
    )
    sandbox = FakeSandbox(candidate=candidate)

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is evaluation_status
    assert result.output_truncated is truncated
    assert len(sandbox.requests) == 1


def test_candidate_nonzero_exit_is_tests_failed_and_does_not_score() -> None:
    candidate_stdout = b"partial candidate response\n"
    sandbox = FakeSandbox(
        candidate=sandbox_result(
            exit_code=2,
            stdout=candidate_stdout,
            stderr=b"candidate error\n",
        )
    )

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert "candidate invoke phase exited 2" in result.stderr
    assert result.raw_candidate_stdout == candidate_stdout
    assert result.raw_scorer_stdout is None
    assert len(sandbox.requests) == 1


@pytest.mark.parametrize(
    "scorer_status",
    [
        SandboxStatus.TIMED_OUT,
        SandboxStatus.OUTPUT_EXHAUSTED,
        SandboxStatus.OOM_KILLED,
        SandboxStatus.INFRASTRUCTURE_ERROR,
    ],
)
def test_trusted_scorer_sandbox_failures_are_infrastructure_errors(
    scorer_status: SandboxStatus,
) -> None:
    scorer = sandbox_result(
        scorer_status,
        exit_code=None if scorer_status is SandboxStatus.INFRASTRUCTURE_ERROR else 137,
        output_truncated=scorer_status is SandboxStatus.OUTPUT_EXHAUSTED,
    )
    result = ContainerEvaluator(sandbox=FakeSandbox(scorer=scorer), image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR


def test_scorer_dispatch_failure_preserves_the_candidate_transcript() -> None:
    def unavailable(_: SandboxRequest) -> SandboxResult:
        raise SandboxUnavailableError("simulated scorer dispatch failure")

    sandbox = FakeSandbox(scorer=unavailable)
    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert "simulated scorer dispatch failure" in result.stderr
    assert result.raw_candidate_stdout == _CANDIDATE_RESPONSE
    assert result.raw_scorer_stdout is None
    assert result.execution["candidate_stdout_sha256"] == sha256_bytes(_CANDIDATE_RESPONSE)
    assert len(sandbox.requests) == 2


def test_runner_persists_candidate_transcript_when_scorer_dispatch_fails(
    tmp_path: Path,
) -> None:
    def unavailable(_: SandboxRequest) -> SandboxResult:
        raise SandboxUnavailableError("simulated scorer dispatch failure")

    evaluator = ContainerEvaluator(
        sandbox=FakeSandbox(scorer=unavailable),
        image=_IMAGE,
    )
    runner = FixtureRunner(
        state_directory=tmp_path / "state",
        clock=DeterministicClock(started_at=datetime(2026, 8, 1, tzinfo=UTC)),
        evaluator=evaluator,
    )

    result = runner.run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id="run-scorer-dispatch-failure",
        code_revision="test-revision",
    )

    assert result.evaluation.outcome == "error"
    assert "evaluation_candidate_stdout" in result.manifest.artifacts
    assert "evaluation_scorer_stdout" not in result.manifest.artifacts
    candidate = result.manifest.artifacts["evaluation_candidate_stdout"]
    assert FileArtifactStore(result.artifact_root).get_bytes(candidate) == _CANDIDATE_RESPONSE
    assert result.replay.artifacts["evaluation_candidate_stdout"] == candidate.sha256


def test_candidate_success_marker_cannot_override_trusted_negative_verdict() -> None:
    forged = sandbox_result(
        stdout=(
            b'GUILDMIND_EVALUATION_RESULT={"schema_version":'
            b'"guildmind.evaluator-completion/v2","successful":true}\n'
        )
    )

    def negative(request: SandboxRequest) -> SandboxResult:
        return scorer_result(request, classification="candidate_failed")

    result = ContainerEvaluator(
        sandbox=FakeSandbox(candidate=forged, scorer=negative),
        image=_IMAGE,
    ).evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.TESTS_FAILED


def test_invalid_patch_is_rejected_before_sandbox_dispatch(tmp_path: Path) -> None:
    patch = tmp_path / "invalid.patch"
    patch.write_bytes(b"not a patch\n")
    sandbox = FakeSandbox()

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        patch,
    )

    assert result.status is EvaluationStatus.INVALID_PATCH
    assert sandbox.requests == []


def test_patch_identity_mismatch_is_rejected_before_sandbox_dispatch() -> None:
    sandbox = FakeSandbox()

    result = ContainerEvaluator(sandbox=sandbox, image=_IMAGE).evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
        expected_patch_sha256="0" * 64,
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert "do not match the committed artifact identity" in result.stderr
    assert sandbox.requests == []


def test_candidate_mount_allowlist_rejects_grader_source_before_dispatch() -> None:
    sandbox = FakeSandbox()
    evaluator = CandidateMountLeakEvaluator(sandbox=sandbox, image=_IMAGE)

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert "candidate phase mount allowlist was violated" in result.stderr
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
    execution = cast(dict[str, object], result.evaluation.result["execution"])
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
@pytest.mark.parametrize(
    "case",
    _ADVERSARIAL_CORPUS.cases,
    ids=[case.case_id for case in _ADVERSARIAL_CORPUS.cases],
)
def test_development_evaluator_matches_adversarial_corpus(case: AdversarialCase) -> None:
    image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=image,
    )

    result = evaluator.evaluate(
        load_fixture(_FIXTURE),
        case.patch_path,
        expected_patch_sha256=case.patch_sha256,
    )

    _assert_adversarial_result(case, result)


@pytest.mark.reference_sandbox
def test_reference_host_container_evaluator_smoke() -> None:
    image = os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_REFERENCE_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(sandbox=DockerSandbox(), image=image)

    result = evaluator.evaluate(load_fixture(_FIXTURE), _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.PASSED
    assert result.execution["image_reference"] == image


@pytest.mark.reference_sandbox
@pytest.mark.parametrize(
    "case",
    _ADVERSARIAL_CORPUS.cases,
    ids=[case.case_id for case in _ADVERSARIAL_CORPUS.cases],
)
def test_reference_host_matches_adversarial_corpus(case: AdversarialCase) -> None:
    image = os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_REFERENCE_EVALUATOR_IMAGE is not configured")
    evaluator = ContainerEvaluator(sandbox=DockerSandbox(), image=image)

    result = evaluator.evaluate(
        load_fixture(_FIXTURE),
        case.patch_path,
        expected_patch_sha256=case.patch_sha256,
    )

    _assert_adversarial_result(case, result)


def _assert_adversarial_result(
    case: AdversarialCase,
    result: LocalEvaluationResult,
) -> None:
    expected = case.expected
    assert result.status is expected.evaluation_status
    assert result.output_truncated is expected.output_truncated
    assert result.raw_candidate_stdout is not None
    if expected.phase == "candidate":
        assert result.raw_scorer_stdout is None
        assert "scorer" not in result.execution
        return

    assert result.raw_scorer_stdout is not None
    completion = cast(dict[str, object], result.execution["completion"])
    assert completion["classification"] == expected.scorer_classification
