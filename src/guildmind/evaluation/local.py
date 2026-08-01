"""Deterministic, zero-network-by-convention evaluation for local fixtures.

The adapter copies workspaces and constrains patch shape, runtime, and retained output.
It does not provide hostile-code isolation or technically disable network access. Only
trusted, repository-owned fixtures should be evaluated with it.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, Self, cast

from pydantic import JsonValue

from guildmind.domain import EvaluationResult, RunStatus, TaskSpec, canonical_sha256
from guildmind.sandbox import (
    PatchApplyError,
    PatchPolicy,
    PatchValidationError,
    copy_and_apply_patch,
)

_OUTPUT_TRUNCATED_MARKER = b"\n...[output truncated]\n"
_UNITTEST_DURATION = re.compile(r"(?m)^(?P<prefix>Ran \d+ tests? in )\d+(?:\.\d+)?s$")


class FixtureConfigurationError(ValueError):
    """Raised when a fixture manifest is missing required local-evaluation data."""


class EvaluationStatus(StrEnum):
    PASSED = "passed"
    TESTS_FAILED = "tests_failed"
    INVALID_PATCH = "invalid_patch"
    PATCH_APPLY_FAILED = "patch_apply_failed"
    TIMED_OUT = "timed_out"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True, slots=True)
class LocalEvaluationSpec:
    """Resolved paths and limits for one repository-owned fixture."""

    task_id: str
    pristine_workspace: Path
    hidden_test_files: tuple[Path, ...]
    allowed_patch_paths: tuple[str, ...]
    visible_test_files: tuple[Path, ...] = ()
    timeout_seconds: float = 5.0
    max_output_bytes: int = 8_192
    max_patch_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not self.task_id:
            raise FixtureConfigurationError("task_id must not be empty")
        if not self.hidden_test_files:
            raise FixtureConfigurationError("at least one hidden test file is required")
        grader_test_files = (*self.visible_test_files, *self.hidden_test_files)
        if len({path.name for path in grader_test_files}) != len(grader_test_files):
            raise FixtureConfigurationError("evaluator test filenames must be unique")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise FixtureConfigurationError("timeout_seconds must be positive")
        if self.max_output_bytes <= 0:
            raise FixtureConfigurationError("max_output_bytes must be positive")
        if self.max_patch_bytes <= 0:
            raise FixtureConfigurationError("max_patch_bytes must be positive")
        try:
            PatchPolicy(
                allowed_paths=self.allowed_patch_paths,
                max_patch_bytes=self.max_patch_bytes,
            )
        except ValueError as error:
            raise FixtureConfigurationError(f"invalid patch policy: {error}") from error

    @classmethod
    def from_fixture(cls, fixture_root: Path) -> Self:
        manifest_path = fixture_root / "task.json"
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            message = f"cannot read fixture manifest: {manifest_path}"
            raise FixtureConfigurationError(message) from error
        if not isinstance(raw, dict):
            raise FixtureConfigurationError("fixture manifest must contain a JSON object")

        schema_version = _required_string(raw, "schema_version")
        if schema_version != "guildmind.fixture/v1":
            raise FixtureConfigurationError(f"unsupported fixture schema: {schema_version}")

        workspace_relative = _plain_relative_path(_required_string(raw, "workspace_dir"))
        workspace = fixture_root / workspace_relative
        visible_values = _required_string_list(raw, "visible_test_files")
        visible_tests = tuple(
            fixture_root / _plain_relative_path(value) for value in visible_values
        )
        for visible_test in visible_tests:
            if not _is_within(visible_test, workspace) or not _is_within(
                visible_test.resolve(), workspace.resolve()
            ):
                message = "visible tests must be inside the submitted workspace"
                raise FixtureConfigurationError(message)
        hidden_values = _required_string_list(raw, "hidden_test_files")
        hidden_tests = tuple(fixture_root / _plain_relative_path(value) for value in hidden_values)
        for hidden_test in hidden_tests:
            if _is_within(hidden_test, workspace) or _is_within(
                hidden_test.resolve(), workspace.resolve()
            ):
                message = "hidden tests must be outside the submitted workspace"
                raise FixtureConfigurationError(message)

        return cls(
            task_id=_required_string(raw, "task_id"),
            pristine_workspace=workspace,
            hidden_test_files=hidden_tests,
            allowed_patch_paths=tuple(_required_string_list(raw, "allowed_patch_paths")),
            visible_test_files=visible_tests,
            timeout_seconds=_optional_positive_number(raw, "timeout_seconds", 5.0),
            max_output_bytes=_optional_positive_integer(raw, "max_output_bytes", 8_192),
            max_patch_bytes=_optional_positive_integer(raw, "max_patch_bytes", 65_536),
        )


@dataclass(frozen=True, slots=True)
class LocalEvaluationResult:
    """Small adapter-level result that can later map into the domain result model."""

    task_id: str
    status: EvaluationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False

    @property
    def passed(self) -> bool:
        return self.status is EvaluationStatus.PASSED

    def to_domain_result(
        self,
        task: TaskSpec,
        *,
        evaluation_id: str,
        run_id: str,
        run_status: RunStatus,
        evaluator_version: str,
        patch_hash: str,
        evaluated_at: datetime,
    ) -> EvaluationResult:
        """Attach orchestration provenance and produce the canonical domain record."""

        if task.task_id != self.task_id:
            raise ValueError("local result and domain task IDs do not match")
        outcome: Literal["passed", "failed", "error"]
        score: float | None
        if self.status is EvaluationStatus.PASSED:
            outcome = "passed"
            score = 1.0
        elif self.status is EvaluationStatus.TESTS_FAILED:
            outcome = "failed"
            score = 0.0
        else:
            outcome = "error"
            score = None
        payload: dict[str, JsonValue] = {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "output_truncated": self.output_truncated,
        }
        return EvaluationResult(
            evaluation_id=evaluation_id,
            run_id=run_id,
            run_status=run_status,
            evaluator_version=evaluator_version,
            task_hash=task.task_content_hash,
            patch_hash=patch_hash,
            outcome=outcome,
            score=score,
            result=payload,
            result_sha256=canonical_sha256(payload),
            evaluated_at=evaluated_at,
        )


class LocalEvaluator:
    """Apply a patch to copies and run hidden unittest files with local bounds."""

    def evaluate(self, spec: LocalEvaluationSpec, patch_path: Path) -> LocalEvaluationResult:
        policy = PatchPolicy(
            allowed_paths=spec.allowed_patch_paths,
            max_patch_bytes=spec.max_patch_bytes,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="guildmind-evaluation-") as temporary:
                temporary_root = Path(temporary)
                patched_workspace = temporary_root / "patched-workspace"
                copy_and_apply_patch(
                    spec.pristine_workspace,
                    patch_path,
                    patched_workspace,
                    policy,
                )

                evaluation_root = temporary_root / "evaluation"
                evaluation_workspace = evaluation_root / "workspace"
                grader_directory = evaluation_root / "grader"
                shutil.copytree(patched_workspace, evaluation_workspace, symlinks=True)
                grader_directory.mkdir(parents=True)
                for test_file in (*spec.visible_test_files, *spec.hidden_test_files):
                    _copy_evaluator_test(test_file, grader_directory / test_file.name)

                return _run_hidden_tests(
                    task_id=spec.task_id,
                    workspace=evaluation_workspace,
                    grader=grader_directory,
                    timeout_seconds=spec.timeout_seconds,
                    max_output_bytes=spec.max_output_bytes,
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
        except (OSError, shutil.Error) as error:
            return LocalEvaluationResult(
                task_id=spec.task_id,
                status=EvaluationStatus.INFRASTRUCTURE_ERROR,
                stderr=f"local evaluation setup failed: {error}",
            )


def load_fixture(fixture_root: Path) -> LocalEvaluationSpec:
    """Load one fixture manifest into an evaluation specification."""

    return LocalEvaluationSpec.from_fixture(fixture_root)


@dataclass(slots=True)
class _CappedCapture:
    limit: int
    data: bytearray
    truncated: bool = False

    @classmethod
    def create(cls, limit: int) -> _CappedCapture:
        return cls(limit=limit, data=bytearray())

    def feed(self, chunk: bytes) -> None:
        remaining = max(0, self.limit - len(self.data))
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    def render(self) -> str:
        data = bytes(self.data)
        if self.truncated:
            marker = _OUTPUT_TRUNCATED_MARKER[: self.limit]
            retained = max(0, self.limit - len(marker))
            data = data[:retained] + marker
        return data.decode("utf-8", errors="replace")


def _run_hidden_tests(
    *,
    task_id: str,
    workspace: Path,
    grader: Path,
    timeout_seconds: float,
    max_output_bytes: int,
) -> LocalEvaluationResult:
    deadline = time.monotonic() + timeout_seconds
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(grader),
        "-p",
        "test_*.py",
        "-v",
    ]
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(workspace),
        "TZ": "UTC",
    }

    try:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return LocalEvaluationResult(
            task_id=task_id,
            status=EvaluationStatus.INFRASTRUCTURE_ERROR,
            stderr=f"could not start hidden tests: {error}",
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_capture = _CappedCapture.create(max_output_bytes)
    stderr_capture = _CappedCapture.create(max_output_bytes)
    timed_out = _wait_with_capped_output(
        process,
        stdout_capture=stdout_capture,
        stderr_capture=stderr_capture,
        deadline=deadline,
    )

    stdout = _normalize_test_output(stdout_capture.render(), workspace.parent)
    stderr = _normalize_test_output(stderr_capture.render(), workspace.parent)
    truncated = stdout_capture.truncated or stderr_capture.truncated
    if timed_out:
        return LocalEvaluationResult(
            task_id=task_id,
            status=EvaluationStatus.TIMED_OUT,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            output_truncated=truncated,
        )
    status = EvaluationStatus.PASSED if process.returncode == 0 else EvaluationStatus.TESTS_FAILED
    return LocalEvaluationResult(
        task_id=task_id,
        status=status,
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        output_truncated=truncated,
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _wait_with_capped_output(
    process: subprocess.Popen[bytes],
    *,
    stdout_capture: _CappedCapture,
    stderr_capture: _CappedCapture,
    deadline: float,
) -> bool:
    assert process.stdout is not None
    assert process.stderr is not None
    selector = selectors.DefaultSelector()
    streams = (process.stdout, process.stderr)
    captures = (stdout_capture, stderr_capture)
    for stream, capture in zip(streams, captures, strict=True):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, (capture, stream))

    timed_out = False
    cleaned_process_group = False
    try:
        while process.poll() is None or selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_group(process)
                break

            if process.poll() is not None and not cleaned_process_group:
                # A test may leave children holding the capture pipes after the runner exits.
                _terminate_process_group(process)
                cleaned_process_group = True

            if not selector.get_map():
                try:
                    process.wait(timeout=min(remaining, 0.05))
                except subprocess.TimeoutExpired:
                    continue
                continue

            for key, _ in selector.select(timeout=min(remaining, 0.05)):
                try:
                    chunk = os.read(key.fd, 8_192)
                except BlockingIOError:
                    continue
                if chunk:
                    capture, _ = cast(tuple[_CappedCapture, BinaryIO], key.data)
                    capture.feed(chunk)
                else:
                    _, stream = cast(tuple[_CappedCapture, BinaryIO], key.data)
                    selector.unregister(stream)
                    stream.close()
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
            timed_out = True
        for key in tuple(selector.get_map().values()):
            _, stream = cast(tuple[_CappedCapture, BinaryIO], key.data)
            selector.unregister(stream)
            stream.close()
        selector.close()
        process.wait()
    return timed_out


def _normalize_test_output(output: str, evaluation_root: Path) -> str:
    without_temporary_path = output.replace(str(evaluation_root), "<evaluation>")
    return _UNITTEST_DURATION.sub(r"\g<prefix>0.000s", without_temporary_path)


def _copy_evaluator_test(source: Path, destination: Path) -> None:
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError as error:
        raise FixtureConfigurationError(f"evaluator test does not exist: {source}") from error
    if not stat.S_ISREG(mode) or source.is_symlink():
        raise FixtureConfigurationError(f"evaluator test must be a regular file: {source}")
    shutil.copyfile(source, destination, follow_symlinks=False)


def _required_string(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise FixtureConfigurationError(f"{key} must be a non-empty string")
    return value


def _required_string_list(raw: dict[str, object], key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not value:
        raise FixtureConfigurationError(f"{key} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise FixtureConfigurationError(f"{key} must contain only non-empty strings")
    return value


def _optional_positive_number(raw: dict[str, object], key: str, default: float) -> float:
    value = raw.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise FixtureConfigurationError(f"{key} must be a positive number")
    return float(value)


def _optional_positive_integer(raw: dict[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FixtureConfigurationError(f"{key} must be a positive integer")
    return value


def _plain_relative_path(value: str) -> Path:
    path = Path(value)
    has_unsafe_part = any(part in {"", ".", ".."} for part in value.split("/"))
    if path.is_absolute() or "\\" in value or has_unsafe_part:
        raise FixtureConfigurationError(f"fixture path must be plain and relative: {value!r}")
    return path


def _is_within(candidate: Path, directory: Path) -> bool:
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True
