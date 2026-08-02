"""Two-phase container evaluator for black-box, repository-owned fixtures.

Candidate code runs only in the invoke phase with a sanitized challenge and no grader.
The trusted score phase receives the sealed oracle and candidate response but never the
candidate workspace. Only score-phase stdout is interpreted as an evaluator verdict.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from guildmind.domain import canonical_sha256, sha256_bytes
from guildmind.evaluation.local import (
    EvaluationStatus,
    FixtureConfigurationError,
    LocalEvaluationResult,
    LocalEvaluationSpec,
    _require_expected_patch_identity,
)
from guildmind.evaluation.protocol import PythonCallBundle, load_python_call_bundle
from guildmind.sandbox import (
    PatchApplyError,
    PatchPolicy,
    PatchValidationError,
    Sandbox,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxMount,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    SandboxUnavailableError,
    copy_and_apply_patch,
)
from guildmind.sandbox.base import validate_image_reference

_RESULT_PREFIX = "GUILDMIND_EVALUATION_RESULT="
_RESULT_PREFIX_BYTES = _RESULT_PREFIX.encode("ascii")
_COMPLETION_SCHEMA = "guildmind.evaluator-completion/v2"
_PROTOCOL = "python-call-v1"
_EVALUATOR_VERSION = "guildmind/container-python-call-v2"
_RESULT_OVERHEAD_BYTES = 8_192


@dataclass(frozen=True, slots=True)
class ContainerEvaluatorResources:
    """Fixed non-time/output bounds for both evaluator phases."""

    cpu_cores: float = 1.0
    memory_bytes: int = 268_435_456
    pids: int = 64
    workspace_bytes: int = 67_108_864
    temporary_bytes: int = 16_777_216


@dataclass(frozen=True, slots=True)
class _CompletionBinding:
    task_id: str
    patch_sha256: str
    challenge_sha256: str
    response_sha256: str
    oracle_sha256: str
    image_digest: str
    expected_tests: int
    evaluator_version: str
    limits_sha256: str
    source_sha256: str
    task_content_hash: str

    @property
    def evaluation_binding_sha256(self) -> str:
        return canonical_sha256(
            {
                "challenge_sha256": self.challenge_sha256,
                "evaluator_version": self.evaluator_version,
                "expected_tests": self.expected_tests,
                "image_digest": self.image_digest,
                "limits_sha256": self.limits_sha256,
                "oracle_sha256": self.oracle_sha256,
                "patch_sha256": self.patch_sha256,
                "protocol": _PROTOCOL,
                "response_sha256": self.response_sha256,
                "source_sha256": self.source_sha256,
                "task_content_hash": self.task_content_hash,
                "task_id": self.task_id,
            }
        )


class ContainerEvaluator:
    """Apply a constrained patch, invoke it as data, then score it separately."""

    def __init__(
        self,
        *,
        sandbox: Sandbox,
        image: str,
        resources: ContainerEvaluatorResources | None = None,
    ) -> None:
        validate_image_reference(image)
        self.sandbox = sandbox
        self.image = image
        self.resources = resources or ContainerEvaluatorResources()

    @property
    def evaluator_version(self) -> str:
        return _EVALUATOR_VERSION

    @property
    def environment_digest(self) -> str:
        return self.image.rsplit("@", maxsplit=1)[1]

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
        *,
        expected_patch_sha256: str | None = None,
    ) -> LocalEvaluationResult:
        policy = PatchPolicy(
            allowed_paths=spec.allowed_patch_paths,
            max_patch_bytes=spec.max_patch_bytes,
            max_files=spec.max_patch_files,
        )
        try:
            protocol = spec.python_call_protocol
            if protocol is None:
                raise FixtureConfigurationError(
                    "container evaluation requires an explicit python-call-v1 protocol"
                )
            bundle = load_python_call_bundle(
                protocol,
                expected_case_count=spec.expected_test_count,
            )
            with tempfile.TemporaryDirectory(prefix="guildmind-container-evaluation-") as temporary:
                temporary_root = Path(temporary).resolve()
                pristine_workspace = temporary_root / "pristine-workspace"
                spec.materialize_pristine_workspace(pristine_workspace)
                patched_workspace = temporary_root / "patched-workspace"
                validated = copy_and_apply_patch(
                    pristine_workspace,
                    patch_path,
                    patched_workspace,
                    policy,
                )
                patch_digest = _require_expected_patch_identity(
                    validated.data,
                    expected_patch_sha256=expected_patch_sha256,
                )
                challenge_path = temporary_root / "challenge.json"
                challenge_path.write_bytes(bundle.challenge_bytes)
                grader_directory = temporary_root / "grader"
                grader_directory.mkdir()
                oracle_path = grader_directory / "oracle.json"
                oracle_path.write_bytes(bundle.oracle_bytes)

                candidate_request = self._candidate_request(
                    spec=spec,
                    patch_digest=patch_digest,
                    patched_workspace=patched_workspace,
                    challenge_path=challenge_path,
                )
                _require_candidate_trust_zone(
                    candidate_request,
                    workspace=patched_workspace,
                    challenge=challenge_path,
                )
                candidate_result = self.sandbox.run(candidate_request)
                if (
                    candidate_result.status is not SandboxStatus.EXITED
                    or candidate_result.exit_code != 0
                ):
                    return _candidate_phase_result(
                        spec,
                        candidate_request,
                        candidate_result,
                        bundle=bundle,
                        patch_sha256=patch_digest,
                    )
                try:
                    response_path = temporary_root / "response.txt"
                    response_path.write_bytes(candidate_result.stdout)
                    response_digest = sha256_bytes(candidate_result.stdout)
                    binding = _CompletionBinding(
                        task_id=spec.task_id,
                        patch_sha256=patch_digest,
                        challenge_sha256=bundle.challenge_sha256,
                        response_sha256=response_digest,
                        oracle_sha256=bundle.oracle_sha256,
                        image_digest=self.environment_digest,
                        expected_tests=bundle.case_count,
                        evaluator_version=self.evaluator_version,
                        limits_sha256=_limits_sha256(candidate_request.limits),
                        source_sha256=_required_source_sha256(spec),
                        task_content_hash=_task_content_hash(spec, bundle=bundle),
                    )
                    scorer_request = self._scorer_request(
                        spec=spec,
                        patch_digest=patch_digest,
                        bundle=bundle,
                        binding=binding,
                        challenge_path=challenge_path,
                        grader_directory=grader_directory,
                        response_path=response_path,
                    )
                    _require_scorer_trust_zone(
                        scorer_request,
                        challenge=challenge_path,
                        grader=grader_directory,
                        response=response_path,
                    )
                    scorer_result = self.sandbox.run(scorer_request)
                    return _trusted_score_result(
                        spec,
                        candidate_request=candidate_request,
                        candidate_result=candidate_result,
                        scorer_request=scorer_request,
                        scorer_result=scorer_result,
                        binding=binding,
                    )
                except (
                    OSError,
                    shutil.Error,
                    FixtureConfigurationError,
                    SandboxConfigurationError,
                    SandboxUnavailableError,
                ) as error:
                    return _post_candidate_infrastructure_result(
                        spec,
                        candidate_request=candidate_request,
                        candidate_result=candidate_result,
                        bundle=bundle,
                        patch_sha256=patch_digest,
                        error=error,
                    )
        except PatchValidationError as error:
            return LocalEvaluationResult(
                task_id=spec.task_id,
                status=EvaluationStatus.INVALID_PATCH,
                stderr=str(error),
            )
        except PatchApplyError as error:
            return LocalEvaluationResult(
                task_id=spec.task_id,
                status=EvaluationStatus.PATCH_APPLY_FAILED,
                stderr=str(error),
            )
        except FixtureConfigurationError as error:
            return LocalEvaluationResult(
                task_id=spec.task_id,
                status=EvaluationStatus.INFRASTRUCTURE_ERROR,
                stderr=str(error),
            )
        except (OSError, shutil.Error, SandboxConfigurationError, SandboxUnavailableError) as error:
            return LocalEvaluationResult(
                task_id=spec.task_id,
                status=EvaluationStatus.INFRASTRUCTURE_ERROR,
                stderr=f"container evaluation setup failed: {error}",
            )

    def _candidate_request(
        self,
        *,
        spec: LocalEvaluationSpec,
        patch_digest: str,
        patched_workspace: Path,
        challenge_path: Path,
    ) -> SandboxRequest:
        return SandboxRequest(
            execution_id=_execution_id(spec.task_id, patch_digest, "invoke"),
            image=self.image,
            argv=("/usr/local/bin/python", "-I", "/opt/guildmind/invoke.py"),
            limits=self._limits(spec, output_overhead=_RESULT_OVERHEAD_BYTES),
            mounts=(
                SandboxMount(
                    source=patched_workspace.resolve(),
                    target="/inputs/workspace",
                ),
                SandboxMount(
                    source=challenge_path.resolve(),
                    target="/inputs/challenge.json",
                ),
            ),
        )

    def _scorer_request(
        self,
        *,
        spec: LocalEvaluationSpec,
        patch_digest: str,
        bundle: PythonCallBundle,
        binding: _CompletionBinding,
        challenge_path: Path,
        grader_directory: Path,
        response_path: Path,
    ) -> SandboxRequest:
        return SandboxRequest(
            execution_id=_execution_id(spec.task_id, patch_digest, "score"),
            image=self.image,
            argv=("/usr/local/bin/python", "-I", "/opt/guildmind/score.py"),
            limits=self._limits(spec, output_overhead=_RESULT_OVERHEAD_BYTES),
            environment={
                "GUILDMIND_CHALLENGE_SHA256": binding.challenge_sha256,
                "GUILDMIND_EVALUATOR_VERSION": binding.evaluator_version,
                "GUILDMIND_EXPECTED_TESTS": str(bundle.case_count),
                "GUILDMIND_IMAGE_DIGEST": binding.image_digest,
                "GUILDMIND_LIMITS_SHA256": binding.limits_sha256,
                "GUILDMIND_ORACLE_SHA256": binding.oracle_sha256,
                "GUILDMIND_PATCH_SHA256": binding.patch_sha256,
                "GUILDMIND_RESPONSE_SHA256": binding.response_sha256,
                "GUILDMIND_SOURCE_SHA256": binding.source_sha256,
                "GUILDMIND_TASK_CONTENT_HASH": binding.task_content_hash,
                "GUILDMIND_TASK_ID": binding.task_id,
            },
            mounts=(
                SandboxMount(
                    source=challenge_path.resolve(),
                    target="/inputs/challenge.json",
                ),
                SandboxMount(
                    source=grader_directory.resolve(),
                    target="/inputs/grader",
                ),
                SandboxMount(
                    source=response_path.resolve(),
                    target="/inputs/response.txt",
                ),
            ),
        )

    def _limits(self, spec: LocalEvaluationSpec, *, output_overhead: int) -> SandboxLimits:
        return SandboxLimits(
            cpu_cores=self.resources.cpu_cores,
            memory_bytes=self.resources.memory_bytes,
            pids=self.resources.pids,
            workspace_bytes=self.resources.workspace_bytes,
            temporary_bytes=self.resources.temporary_bytes,
            output_bytes=spec.max_output_bytes + output_overhead,
            wall_time_seconds=spec.timeout_seconds,
        )


def _candidate_phase_result(
    spec: LocalEvaluationSpec,
    request: SandboxRequest,
    result: SandboxResult,
    *,
    bundle: PythonCallBundle,
    patch_sha256: str,
) -> LocalEvaluationResult:
    status = {
        SandboxStatus.TIMED_OUT: EvaluationStatus.TIMED_OUT,
        SandboxStatus.OUTPUT_EXHAUSTED: EvaluationStatus.OUTPUT_EXHAUSTED,
        SandboxStatus.OOM_KILLED: EvaluationStatus.OOM_KILLED,
        SandboxStatus.INFRASTRUCTURE_ERROR: EvaluationStatus.INFRASTRUCTURE_ERROR,
        SandboxStatus.EXITED: EvaluationStatus.TESTS_FAILED,
    }[result.status]
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.diagnostic:
        stderr = _append_diagnostic(stderr, f"candidate sandbox: {result.diagnostic}")
    if result.status is SandboxStatus.EXITED:
        stderr = _append_diagnostic(
            stderr,
            f"candidate invoke phase exited {result.exit_code} before trusted scoring",
        )
    return LocalEvaluationResult(
        task_id=spec.task_id,
        status=status,
        exit_code=result.exit_code,
        stderr=stderr,
        output_truncated=result.output_truncated,
        execution={
            "candidate": _phase_evidence(request, result),
            "candidate_stdout_sha256": sha256_bytes(result.stdout),
            "challenge_sha256": bundle.challenge_sha256,
            "evaluator_version": _EVALUATOR_VERSION,
            "expected_tests": bundle.case_count,
            "image_reference": request.image,
            "limits_sha256": _limits_sha256(request.limits),
            "oracle_sha256": bundle.oracle_sha256,
            "patch_sha256": patch_sha256,
            "protocol": _PROTOCOL,
            "source_sha256": _required_source_sha256(spec),
            "task_content_hash": _task_content_hash(spec, bundle=bundle),
        },
        raw_candidate_stdout=result.stdout,
    )


def _post_candidate_infrastructure_result(
    spec: LocalEvaluationSpec,
    *,
    candidate_request: SandboxRequest,
    candidate_result: SandboxResult,
    bundle: PythonCallBundle,
    patch_sha256: str,
    error: BaseException,
) -> LocalEvaluationResult:
    """Retain candidate evidence when trusted scoring cannot be dispatched."""

    stderr = candidate_result.stderr.decode("utf-8", errors="replace")
    if candidate_result.diagnostic:
        stderr = _append_diagnostic(
            stderr,
            f"candidate sandbox: {candidate_result.diagnostic}",
        )
    stderr = _append_diagnostic(stderr, f"trusted scorer setup failed: {error}")
    execution: dict[str, JsonValue] = {
        "candidate": _phase_evidence(candidate_request, candidate_result),
        "candidate_stdout_sha256": sha256_bytes(candidate_result.stdout),
        "challenge_sha256": bundle.challenge_sha256,
        "evaluator_version": _EVALUATOR_VERSION,
        "expected_tests": bundle.case_count,
        "image_reference": candidate_request.image,
        "limits_sha256": _limits_sha256(candidate_request.limits),
        "oracle_sha256": bundle.oracle_sha256,
        "patch_sha256": patch_sha256,
        "protocol": _PROTOCOL,
    }
    if spec.pristine_workspace_sha256 is not None:
        execution["source_sha256"] = spec.pristine_workspace_sha256
        execution["task_content_hash"] = _task_content_hash(spec, bundle=bundle)
    return LocalEvaluationResult(
        task_id=spec.task_id,
        status=EvaluationStatus.INFRASTRUCTURE_ERROR,
        stderr=stderr,
        output_truncated=candidate_result.output_truncated,
        execution=execution,
        raw_candidate_stdout=candidate_result.stdout,
    )


def _trusted_score_result(
    spec: LocalEvaluationSpec,
    *,
    candidate_request: SandboxRequest,
    candidate_result: SandboxResult,
    scorer_request: SandboxRequest,
    scorer_result: SandboxResult,
    binding: _CompletionBinding,
) -> LocalEvaluationResult:
    completion, clean_stdout, completion_error = _extract_completion(
        scorer_result.stdout,
        binding=binding,
    )
    stderr = scorer_result.stderr.decode("utf-8", errors="replace")
    candidate_stderr = candidate_result.stderr.decode("utf-8", errors="replace")
    if candidate_stderr:
        stderr = _append_diagnostic(stderr, f"candidate stderr:\n{candidate_stderr}")
    if scorer_result.diagnostic:
        stderr = _append_diagnostic(stderr, f"scorer sandbox: {scorer_result.diagnostic}")
    if completion_error:
        stderr = _append_diagnostic(stderr, completion_error)
    if completion is not None:
        message = completion.get("message")
        error = completion.get("error")
        if isinstance(message, str):
            stderr = _append_diagnostic(stderr, f"scorer: {message}")
        if isinstance(error, str):
            stderr = _append_diagnostic(stderr, f"scorer: {error}")

    status = _trusted_status(scorer_result, completion, completion_error)
    execution: dict[str, JsonValue] = {
        "candidate": _phase_evidence(candidate_request, candidate_result),
        "challenge_sha256": binding.challenge_sha256,
        "evaluator_version": binding.evaluator_version,
        "evaluation_binding_sha256": binding.evaluation_binding_sha256,
        "expected_tests": binding.expected_tests,
        "image_id": scorer_result.image_id,
        "image_reference": scorer_request.image,
        "limits_sha256": binding.limits_sha256,
        "oracle_sha256": binding.oracle_sha256,
        "patch_sha256": binding.patch_sha256,
        "protocol": _PROTOCOL,
        "response_sha256": binding.response_sha256,
        "scorer": _phase_evidence(scorer_request, scorer_result),
        "source_sha256": binding.source_sha256,
        "task_content_hash": binding.task_content_hash,
    }
    if completion is not None:
        completion_summary: dict[str, JsonValue] = {
            "classification": completion["classification"],
            "errors": completion["errors"],
            "expected_tests": completion["expected_tests"],
            "failures": completion["failures"],
            "skipped": completion["skipped"],
            "successful": completion["successful"],
            "tests_run": completion["tests_run"],
        }
        execution["completion"] = completion_summary
        execution["trusted_completion_record_sha256"] = _completion_record_sha256(
            scorer_result.stdout
        )
    return LocalEvaluationResult(
        task_id=spec.task_id,
        status=status,
        exit_code=scorer_result.exit_code,
        stdout=clean_stdout,
        stderr=stderr,
        output_truncated=scorer_result.output_truncated,
        execution=execution,
        raw_candidate_stdout=candidate_result.stdout,
        raw_scorer_stdout=scorer_result.stdout,
    )


def _trusted_status(
    result: SandboxResult,
    completion: dict[str, JsonValue] | None,
    completion_error: str | None,
) -> EvaluationStatus:
    if result.status is not SandboxStatus.EXITED or completion is None or completion_error:
        return EvaluationStatus.INFRASTRUCTURE_ERROR
    classification = completion.get("classification")
    successful = completion.get("successful")
    if result.exit_code == 0 and classification == "passed" and successful is True:
        return EvaluationStatus.PASSED
    if result.exit_code == 1 and classification == "candidate_failed" and successful is False:
        return EvaluationStatus.TESTS_FAILED
    return EvaluationStatus.INFRASTRUCTURE_ERROR


def _extract_completion(
    stdout: bytes,
    *,
    binding: _CompletionBinding,
) -> tuple[dict[str, JsonValue] | None, str, str | None]:
    try:
        stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None, "", "trusted scorer output was not UTF-8"
    lines = stdout.split(b"\n")
    marker_indexes = [
        index for index, line in enumerate(lines) if line.startswith(_RESULT_PREFIX_BYTES)
    ]
    clean_bytes = b"\n".join(
        line for index, line in enumerate(lines) if index not in marker_indexes
    )
    clean_stdout = clean_bytes.decode("utf-8")
    if len(marker_indexes) != 1:
        return None, clean_stdout, "trusted scorer emitted zero or multiple completion records"
    marker_index = marker_indexes[0]
    if any(line.strip() for line in lines[marker_index + 1 :]):
        return None, clean_stdout, "trusted scorer completion record was not final"
    encoded = lines[marker_index][len(_RESULT_PREFIX_BYTES) :]
    try:
        raw = _strict_json_object(encoded)
        completion = _validate_completion(raw, binding=binding)
    except (ValueError, RecursionError) as error:
        return None, clean_stdout, str(error)
    return completion, clean_stdout, None


def _strict_json_object(encoded: bytes) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"trusted scorer completion contains non-finite number {value}")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"trusted scorer completion contains duplicate key {key!r}")
            result[key] = value
        return result

    try:
        raw = json.loads(
            encoded,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("trusted scorer completion was not valid JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("trusted scorer completion was not an object")
    return cast(dict[str, object], raw)


def _completion_record_sha256(stdout: bytes) -> str:
    records = [line for line in stdout.split(b"\n") if line.startswith(_RESULT_PREFIX_BYTES)]
    if len(records) != 1:
        raise ValueError("trusted scorer completion record is not unique")
    return sha256_bytes(records[0])


def _validate_completion(
    raw: dict[str, object],
    *,
    binding: _CompletionBinding,
) -> dict[str, JsonValue]:
    base_fields = {
        "challenge_sha256",
        "classification",
        "evaluator_version",
        "errors",
        "evaluation_binding_sha256",
        "expected_tests",
        "failures",
        "image_digest",
        "limits_sha256",
        "oracle_sha256",
        "patch_sha256",
        "protocol",
        "response_sha256",
        "schema_version",
        "skipped",
        "successful",
        "source_sha256",
        "task_content_hash",
        "task_id",
        "tests_run",
    }
    classification = raw.get("classification")
    allowed_fields = set(base_fields)
    if classification == "candidate_failed" and "message" in raw:
        allowed_fields.add("message")
    if classification == "evaluator_error":
        allowed_fields.add("error")
    if set(raw) != allowed_fields:
        raise ValueError("trusted scorer completion fields are invalid")
    expected_values: dict[str, object] = {
        "challenge_sha256": binding.challenge_sha256,
        "evaluator_version": binding.evaluator_version,
        "evaluation_binding_sha256": binding.evaluation_binding_sha256,
        "expected_tests": binding.expected_tests,
        "image_digest": binding.image_digest,
        "limits_sha256": binding.limits_sha256,
        "oracle_sha256": binding.oracle_sha256,
        "patch_sha256": binding.patch_sha256,
        "protocol": _PROTOCOL,
        "response_sha256": binding.response_sha256,
        "schema_version": _COMPLETION_SCHEMA,
        "source_sha256": binding.source_sha256,
        "task_content_hash": binding.task_content_hash,
        "task_id": binding.task_id,
    }
    for key, expected in expected_values.items():
        if raw.get(key) != expected:
            raise ValueError(f"trusted scorer completion has mismatched {key}")
    for field_name in ("errors", "expected_tests", "failures", "skipped", "tests_run"):
        value = raw.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"trusted scorer completion has invalid {field_name}")
    errors = cast(int, raw["errors"])
    failures = cast(int, raw["failures"])
    skipped = cast(int, raw["skipped"])
    tests_run = cast(int, raw["tests_run"])
    if skipped != 0:
        raise ValueError("trusted scorer completion contains skipped tests")
    if tests_run > binding.expected_tests:
        raise ValueError("trusted scorer completion ran too many tests")
    if failures > binding.expected_tests:
        raise ValueError("trusted scorer completion reported too many failures")
    successful = raw.get("successful")
    if not isinstance(successful, bool):
        raise ValueError("trusted scorer completion omitted successful")
    if classification == "passed":
        if successful is not True:
            raise ValueError("trusted scorer passed classification is not successful")
        if tests_run != binding.expected_tests:
            raise ValueError("trusted scorer did not run every expected test")
        if failures != 0 or errors != 0:
            raise ValueError("trusted scorer successful completion contains failures or errors")
    elif classification == "candidate_failed":
        if successful is not False:
            raise ValueError("candidate-failed scorer completion is successful")
        if failures <= 0 or errors != 0:
            raise ValueError("candidate-failed scorer completion has invalid counts")
        if tests_run not in {0, binding.expected_tests}:
            raise ValueError("candidate-failed scorer completion has partial test counts")
        message = raw.get("message")
        if message is not None and (not isinstance(message, str) or not message.strip()):
            raise ValueError("candidate-failed scorer completion has invalid message")
    elif classification == "evaluator_error":
        error = raw.get("error")
        if (
            successful is not False
            or not isinstance(error, str)
            or not error.strip()
            or errors <= 0
            or failures != 0
            or tests_run != 0
        ):
            raise ValueError("evaluator-error scorer completion is invalid")
    else:
        raise ValueError("trusted scorer completion has unknown classification")
    return cast(dict[str, JsonValue], raw)


def _phase_evidence(request: SandboxRequest, result: SandboxResult) -> dict[str, JsonValue]:
    return {
        "cpu_cores": request.limits.cpu_cores,
        "execution_id": result.execution_id,
        "exit_code": result.exit_code,
        "image_id": result.image_id,
        "memory_bytes": request.limits.memory_bytes,
        "output_bytes": request.limits.output_bytes,
        "output_truncated": result.output_truncated,
        "pids": request.limits.pids,
        "sandbox_status": result.status.value,
        "temporary_bytes": request.limits.temporary_bytes,
        "wall_time_seconds": request.limits.wall_time_seconds,
        "workspace_bytes": request.limits.workspace_bytes,
    }


def _limits_sha256(limits: SandboxLimits) -> str:
    return canonical_sha256(
        {
            "cpu_cores": limits.cpu_cores,
            "memory_bytes": limits.memory_bytes,
            "output_bytes": limits.output_bytes,
            "pids": limits.pids,
            "temporary_bytes": limits.temporary_bytes,
            "wall_time_seconds": limits.wall_time_seconds,
            "workspace_bytes": limits.workspace_bytes,
        }
    )


def _required_source_sha256(spec: LocalEvaluationSpec) -> str:
    if spec.pristine_workspace_sha256 is None:
        raise FixtureConfigurationError("evaluation workspace has no frozen source identity")
    return spec.pristine_workspace_sha256


def _task_content_hash(spec: LocalEvaluationSpec, *, bundle: PythonCallBundle) -> str:
    if spec.task_content_hash is not None:
        return spec.task_content_hash
    return canonical_sha256(
        {
            "challenge_sha256": bundle.challenge_sha256,
            "oracle_sha256": bundle.oracle_sha256,
            "protocol": _PROTOCOL,
            "source_sha256": _required_source_sha256(spec),
            "task_id": spec.task_id,
        }
    )


def _require_candidate_trust_zone(
    request: SandboxRequest,
    *,
    workspace: Path,
    challenge: Path,
) -> None:
    actual = {mount.target: mount.source for mount in request.mounts}
    expected = {
        "/inputs/challenge.json": challenge.resolve(),
        "/inputs/workspace": workspace.resolve(),
    }
    if actual != expected:
        raise SandboxConfigurationError("candidate phase mount allowlist was violated")


def _require_scorer_trust_zone(
    request: SandboxRequest,
    *,
    challenge: Path,
    grader: Path,
    response: Path,
) -> None:
    actual = {mount.target: mount.source for mount in request.mounts}
    expected = {
        "/inputs/challenge.json": challenge.resolve(),
        "/inputs/grader": grader.resolve(),
        "/inputs/response.txt": response.resolve(),
    }
    if actual != expected:
        raise SandboxConfigurationError("scorer phase mount allowlist was violated")


def _append_diagnostic(stderr: str, diagnostic: str) -> str:
    separator = "" if not stderr or stderr.endswith("\n") else "\n"
    return f"{stderr}{separator}[guildmind] {diagnostic}\n"


def _execution_id(task_id: str, patch_digest: str, phase: str) -> str:
    normalized = re.sub(r"[^a-z0-9.-]+", "-", task_id.lower()).strip("-.")
    prefix = normalized[:28] or "fixture"
    return f"eval-{prefix}-{patch_digest[:12]}-{phase}"
