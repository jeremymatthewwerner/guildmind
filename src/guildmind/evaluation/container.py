"""Bounded container evaluator for trusted repository-owned fixtures.

This adapter is not yet a hostile-code evaluator: candidate imports and ``unittest``
share one interpreter, and candidate code can read the mounted grader. The strict
expected-failure integration case preserves that open Stage 1 boundary defect.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from guildmind.domain import sha256_bytes
from guildmind.evaluation.local import (
    EvaluationStatus,
    FixtureConfigurationError,
    LocalEvaluationResult,
    LocalEvaluationSpec,
)
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
_UNITTEST_DURATION = re.compile(r"(?m)^(?P<prefix>Ran \d+ tests? in )\d+(?:\.\d+)?s$")
_RESULT_OVERHEAD_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class ContainerEvaluatorResources:
    """Fixed non-time/output bounds for the first evaluator image."""

    cpu_cores: float = 1.0
    memory_bytes: int = 268_435_456
    pids: int = 64
    workspace_bytes: int = 67_108_864
    temporary_bytes: int = 16_777_216


class ContainerEvaluator:
    """Apply a constrained patch, then grade a trusted fixture in a fresh sandbox."""

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
        return "guildmind/container-fixture-v1"

    @property
    def environment_digest(self) -> str:
        return self.image.rsplit("@", maxsplit=1)[1]

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
    ) -> LocalEvaluationResult:
        policy = PatchPolicy(
            allowed_paths=spec.allowed_patch_paths,
            max_patch_bytes=spec.max_patch_bytes,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="guildmind-container-evaluation-") as temporary:
                temporary_root = Path(temporary).resolve()
                patched_workspace = temporary_root / "patched-workspace"
                validated = copy_and_apply_patch(
                    spec.pristine_workspace,
                    patch_path,
                    patched_workspace,
                    policy,
                )
                grader_directory = temporary_root / "grader"
                grader_directory.mkdir()
                for test_file in (*spec.visible_test_files, *spec.hidden_test_files):
                    _copy_regular_file(test_file, grader_directory / test_file.name)

                limits = SandboxLimits(
                    cpu_cores=self.resources.cpu_cores,
                    memory_bytes=self.resources.memory_bytes,
                    pids=self.resources.pids,
                    workspace_bytes=self.resources.workspace_bytes,
                    temporary_bytes=self.resources.temporary_bytes,
                    output_bytes=spec.max_output_bytes + _RESULT_OVERHEAD_BYTES,
                    wall_time_seconds=spec.timeout_seconds,
                )
                patch_digest = sha256_bytes(validated.data)
                request = SandboxRequest(
                    execution_id=_execution_id(spec.task_id, patch_digest),
                    image=self.image,
                    argv=("/usr/local/bin/python", "-I", "/opt/guildmind/evaluate.py"),
                    limits=limits,
                    environment={"GUILDMIND_EXPECTED_TESTS": str(spec.expected_test_count)},
                    mounts=(
                        SandboxMount(
                            source=patched_workspace.resolve(),
                            target="/inputs/workspace",
                        ),
                        SandboxMount(
                            source=grader_directory.resolve(),
                            target="/inputs/grader",
                        ),
                    ),
                )
                sandbox_result = self.sandbox.run(request)
                return _to_local_result(spec, request, sandbox_result)
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


def _to_local_result(
    spec: LocalEvaluationSpec,
    request: SandboxRequest,
    result: SandboxResult,
) -> LocalEvaluationResult:
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    completion, clean_stdout, completion_error = _extract_completion(
        stdout,
        expected_tests=spec.expected_test_count,
    )
    stderr = _UNITTEST_DURATION.sub(r"\g<prefix>0.000s", stderr)
    if result.diagnostic:
        stderr = _append_diagnostic(stderr, f"sandbox: {result.diagnostic}")
    if completion_error:
        stderr = _append_diagnostic(stderr, completion_error)
    if completion is not None and completion.get("successful") is False:
        completion_message = completion.get("error")
        if isinstance(completion_message, str):
            stderr = _append_diagnostic(stderr, f"evaluator: {completion_message}")

    status = _evaluation_status(result, completion)
    execution: dict[str, JsonValue] = {
        "cpu_cores": request.limits.cpu_cores,
        "execution_id": result.execution_id,
        "image_id": result.image_id,
        "image_reference": request.image,
        "memory_bytes": request.limits.memory_bytes,
        "output_bytes": request.limits.output_bytes,
        "pids": request.limits.pids,
        "sandbox_status": result.status.value,
        "temporary_bytes": request.limits.temporary_bytes,
        "wall_time_seconds": request.limits.wall_time_seconds,
        "workspace_bytes": request.limits.workspace_bytes,
    }
    return LocalEvaluationResult(
        task_id=spec.task_id,
        status=status,
        exit_code=result.exit_code,
        stdout=clean_stdout,
        stderr=stderr,
        output_truncated=result.output_truncated,
        execution=execution,
    )


def _evaluation_status(
    result: SandboxResult,
    completion: dict[str, JsonValue] | None,
) -> EvaluationStatus:
    if result.status is SandboxStatus.TIMED_OUT:
        return EvaluationStatus.TIMED_OUT
    if result.status is SandboxStatus.OUTPUT_EXHAUSTED:
        return EvaluationStatus.OUTPUT_EXHAUSTED
    if result.status is SandboxStatus.OOM_KILLED:
        return EvaluationStatus.OOM_KILLED
    if result.status is SandboxStatus.INFRASTRUCTURE_ERROR:
        return EvaluationStatus.INFRASTRUCTURE_ERROR
    if result.exit_code == 0 and completion is not None and completion.get("successful") is True:
        return EvaluationStatus.PASSED
    return EvaluationStatus.TESTS_FAILED


def _extract_completion(
    stdout: str,
    *,
    expected_tests: int,
) -> tuple[dict[str, JsonValue] | None, str, str | None]:
    lines = stdout.splitlines()
    marker_indexes = [index for index, line in enumerate(lines) if line.startswith(_RESULT_PREFIX)]
    clean_stdout = "\n".join(
        line for index, line in enumerate(lines) if index not in marker_indexes
    )
    if stdout.endswith("\n") and clean_stdout:
        clean_stdout += "\n"
    if len(marker_indexes) != 1:
        return None, clean_stdout, "evaluator emitted zero or multiple completion records"
    marker_index = marker_indexes[0]
    if any(line.strip() for line in lines[marker_index + 1 :]):
        return None, clean_stdout, "evaluator completion record was not final"
    encoded = lines[marker_index][len(_RESULT_PREFIX) :]
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError:
        return None, clean_stdout, "evaluator completion record was not valid JSON"
    if not isinstance(raw, dict):
        return None, clean_stdout, "evaluator completion record was not an object"
    try:
        completion = _validate_completion(raw, expected_tests=expected_tests)
    except ValueError as error:
        return None, clean_stdout, str(error)
    return completion, clean_stdout, None


def _validate_completion(
    raw: dict[str, object],
    *,
    expected_tests: int,
) -> dict[str, JsonValue]:
    if raw.get("schema_version") != "guildmind.evaluator-completion/v1":
        raise ValueError("evaluator completion record used an unknown schema")
    successful = raw.get("successful")
    if not isinstance(successful, bool):
        raise ValueError("evaluator completion record omitted successful")
    if successful:
        integer_fields = (
            "discovered_tests",
            "errors",
            "expected_tests",
            "failures",
            "skipped",
            "tests_run",
        )
        for field_name in integer_fields:
            value = raw.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"evaluator completion record has invalid {field_name}")
        if raw["expected_tests"] != expected_tests:
            raise ValueError("evaluator completion expected-test count does not match the task")
        if raw["discovered_tests"] != expected_tests or raw["tests_run"] != expected_tests:
            raise ValueError("evaluator did not discover and run every expected test")
        if raw["failures"] != 0 or raw["errors"] != 0:
            raise ValueError("successful evaluator completion contains failures or errors")
        if raw["skipped"] != 0:
            raise ValueError("successful evaluator completion contains skipped tests")
    elif "error" in raw and (not isinstance(raw["error"], str) or not raw["error"].strip()):
        raise ValueError("unsuccessful evaluator completion has an invalid error")
    return {str(key): value for key, value in raw.items()}  # type: ignore[misc]


def _append_diagnostic(stderr: str, diagnostic: str) -> str:
    separator = "" if not stderr or stderr.endswith("\n") else "\n"
    return f"{stderr}{separator}[guildmind] {diagnostic}\n"


def _copy_regular_file(source: Path, destination: Path) -> None:
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError as error:
        raise FixtureConfigurationError(f"evaluator test does not exist: {source}") from error
    if not stat.S_ISREG(mode) or source.is_symlink():
        raise FixtureConfigurationError(f"evaluator test must be a regular file: {source}")
    shutil.copyfile(source, destination, follow_symlinks=False)


def _execution_id(task_id: str, patch_digest: str) -> str:
    normalized = re.sub(r"[^a-z0-9.-]+", "-", task_id.lower()).strip("-.")
    prefix = normalized[:42] or "fixture"
    return f"eval-{prefix}-{patch_digest[:12]}"
