"""Deterministic, zero-network-by-convention evaluation for local fixtures.

The adapter copies workspaces and constrains patch shape, runtime, and retained output.
It does not provide hostile-code isolation or technically disable network access. Only
trusted, repository-owned fixtures should be evaluated with it.
"""

from __future__ import annotations

import base64
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
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal, Self, cast

from pydantic import JsonValue

from guildmind.domain import (
    EvaluationResult,
    RunStatus,
    TaskSpec,
    canonical_json,
    canonical_sha256,
    sha256_bytes,
)
from guildmind.sandbox import (
    PatchApplyError,
    PatchPolicy,
    PatchValidationError,
    copy_and_apply_patch,
)

_OUTPUT_TRUNCATED_MARKER = b"\n...[output truncated]\n"
_UNITTEST_DURATION = re.compile(r"(?m)^(?P<prefix>Ran \d+ tests? in )\d+(?:\.\d+)?s$")
_PYTHON_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PYTHON_CALLABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class FixtureConfigurationError(ValueError):
    """Raised when a fixture manifest is missing required local-evaluation data."""


@dataclass(frozen=True, slots=True)
class _FrozenWorkspaceEntry:
    """One immutable regular-file or directory entry in a loaded workspace."""

    relative_path: str
    mode: int
    data: bytes | None


@dataclass(frozen=True, slots=True)
class _FrozenWorkspace:
    """Canonical workspace bytes plus a lifetime-owned evaluator materialization."""

    entries: tuple[_FrozenWorkspaceEntry, ...] = field(repr=False)
    snapshot_bytes: bytes = field(repr=False)
    snapshot_sha256: str
    root: Path
    _temporary: tempfile.TemporaryDirectory[str] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _FrozenEvaluatorTest:
    """One evaluator-owned test retained independently of its materialized path."""

    path: Path
    mode: int
    data: bytes = field(repr=False)


class EvaluationStatus(StrEnum):
    PASSED = "passed"
    TESTS_FAILED = "tests_failed"
    INVALID_PATCH = "invalid_patch"
    PATCH_APPLY_FAILED = "patch_apply_failed"
    TIMED_OUT = "timed_out"
    OUTPUT_EXHAUSTED = "output_exhausted"
    OOM_KILLED = "oom_killed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True, slots=True)
class PythonCallProtocol:
    """Black-box JSON call protocol used by the isolated container evaluator."""

    module: str
    callable_name: str
    cases_file: Path
    sealed_cases_bytes: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if _PYTHON_MODULE.fullmatch(self.module) is None:
            raise FixtureConfigurationError("evaluation module must be a dotted Python name")
        if _PYTHON_CALLABLE.fullmatch(self.callable_name) is None:
            raise FixtureConfigurationError("evaluation callable must be a Python identifier")


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
    max_patch_files: int = 8
    expected_test_count: int = 1
    python_call_protocol: PythonCallProtocol | None = None
    pristine_workspace_sha256: str | None = None
    task_content_hash: str | None = None
    fixture_manifest_bytes: bytes | None = field(default=None, repr=False)
    _frozen_workspace: _FrozenWorkspace | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _frozen_hidden_tests: tuple[_FrozenEvaluatorTest, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )

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
        if self.max_patch_files <= 0:
            raise FixtureConfigurationError("max_patch_files must be positive")
        if self.expected_test_count <= 0:
            raise FixtureConfigurationError("expected_test_count must be positive")
        if (
            self.pristine_workspace_sha256 is not None
            and _SHA256.fullmatch(self.pristine_workspace_sha256) is None
        ):
            raise FixtureConfigurationError("pristine_workspace_sha256 must be a SHA-256 digest")
        if self.task_content_hash is not None and _SHA256.fullmatch(self.task_content_hash) is None:
            raise FixtureConfigurationError("task_content_hash must be a SHA-256 digest")
        try:
            PatchPolicy(
                allowed_paths=self.allowed_patch_paths,
                max_patch_bytes=self.max_patch_bytes,
                max_files=self.max_patch_files,
            )
        except ValueError as error:
            raise FixtureConfigurationError(f"invalid patch policy: {error}") from error

    @property
    def pristine_workspace_snapshot_bytes(self) -> bytes | None:
        """Return the canonical repository snapshot captured while loading the fixture."""

        if self._frozen_workspace is None:
            return None
        return self._frozen_workspace.snapshot_bytes

    def materialize_pristine_workspace(self, destination: Path) -> None:
        """Materialize the captured workspace into a new evaluation directory."""

        if self._frozen_workspace is None:
            if os.path.lexists(destination):
                raise FixtureConfigurationError(
                    f"workspace materialization destination already exists: {destination}"
                )
            shutil.copytree(self.pristine_workspace, destination, symlinks=True)
            return
        _materialize_workspace_entries(self._frozen_workspace.entries, destination)

    @property
    def visible_test_bytes(self) -> tuple[bytes, ...]:
        """Return visible-test bytes from the captured workspace when available."""

        return tuple(self._visible_test_payload(path)[0] for path in self.visible_test_files)

    @property
    def hidden_test_bytes(self) -> tuple[bytes, ...]:
        """Return hidden-test bytes captured while loading the fixture."""

        return tuple(self._hidden_test_payload(path)[0] for path in self.hidden_test_files)

    def materialize_evaluator_tests(self, destination: Path) -> None:
        """Materialize visible and hidden tests from their retained byte payloads."""

        destination.mkdir(parents=True, exist_ok=False)
        payloads = (
            *(self._visible_test_payload(path) for path in self.visible_test_files),
            *(self._hidden_test_payload(path) for path in self.hidden_test_files),
        )
        for data, mode, name in payloads:
            target = destination / name
            target.write_bytes(data)
            target.chmod(mode)

    def _visible_test_payload(self, path: Path) -> tuple[bytes, int, str]:
        if self._frozen_workspace is not None:
            try:
                relative = path.relative_to(self._frozen_workspace.root).as_posix()
            except ValueError:
                pass
            else:
                for entry in self._frozen_workspace.entries:
                    if entry.relative_path == relative and entry.data is not None:
                        return entry.data, entry.mode, path.name
                raise FixtureConfigurationError(
                    f"visible evaluator test is not a captured regular file: {path}"
                )
        if not _is_within(path, self.pristine_workspace) or not _is_within(
            path.resolve(), self.pristine_workspace.resolve()
        ):
            raise FixtureConfigurationError("visible tests must be inside the submitted workspace")
        mode = _regular_file_mode(path, label="visible evaluator test")
        return _read_regular_file(path, label="visible evaluator test"), mode, path.name

    def _hidden_test_payload(self, path: Path) -> tuple[bytes, int, str]:
        for frozen in self._frozen_hidden_tests:
            if path == frozen.path:
                return frozen.data, frozen.mode, path.name
        mode = _regular_file_mode(path, label="hidden evaluator test")
        return _read_regular_file(path, label="hidden evaluator test"), mode, path.name

    @classmethod
    def from_fixture(cls, fixture_root: Path) -> Self:
        manifest_path = fixture_root / "task.json"
        try:
            manifest_source_bytes = _read_regular_file(
                manifest_path,
                label="fixture manifest",
            )
            raw = json.loads(manifest_source_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            message = f"cannot read fixture manifest: {manifest_path}"
            raise FixtureConfigurationError(message) from error
        if not isinstance(raw, dict):
            raise FixtureConfigurationError("fixture manifest must contain a JSON object")

        schema_version = _required_string(raw, "schema_version")
        if schema_version != "guildmind.fixture/v1":
            raise FixtureConfigurationError(f"unsupported fixture schema: {schema_version}")

        workspace_relative = _plain_relative_path(_required_string(raw, "workspace_dir"))
        workspace_source = fixture_root / workspace_relative
        frozen_workspace = _freeze_workspace(workspace_source)
        workspace = frozen_workspace.root
        visible_values = _required_string_list(raw, "visible_test_files")
        visible_sources = tuple(
            fixture_root / _plain_relative_path(value) for value in visible_values
        )
        visible_tests: list[Path] = []
        for visible_test in visible_sources:
            if not _is_within(visible_test, workspace_source) or not _is_within(
                visible_test.resolve(), workspace_source.resolve()
            ):
                message = "visible tests must be inside the submitted workspace"
                raise FixtureConfigurationError(message)
            frozen_visible = workspace / visible_test.relative_to(workspace_source)
            if frozen_visible.is_symlink() or not frozen_visible.is_file():
                raise FixtureConfigurationError(
                    f"visible evaluator test must be a regular file: {visible_test}"
                )
            visible_tests.append(frozen_visible)
        hidden_values = _required_string_list(raw, "hidden_test_files")
        hidden_sources = tuple(
            fixture_root / _plain_relative_path(value) for value in hidden_values
        )
        for hidden_test in hidden_sources:
            if _is_within(hidden_test, workspace_source) or _is_within(
                hidden_test.resolve(), workspace_source.resolve()
            ):
                message = "hidden tests must be outside the submitted workspace"
                raise FixtureConfigurationError(message)
        hidden_tests, frozen_hidden_tests = _freeze_evaluator_tests(
            hidden_sources,
            destination=frozen_workspace.root.parent / "grader",
        )

        python_call_protocol = _optional_python_call_protocol(
            raw,
            fixture_root=fixture_root,
            workspace=workspace_source,
        )
        expected_test_count = _required_positive_integer(raw, "expected_test_count")
        spec = cls(
            task_id=_required_string(raw, "task_id"),
            pristine_workspace=workspace,
            hidden_test_files=hidden_tests,
            allowed_patch_paths=tuple(_required_string_list(raw, "allowed_patch_paths")),
            visible_test_files=tuple(visible_tests),
            timeout_seconds=_optional_positive_number(raw, "timeout_seconds", 5.0),
            max_output_bytes=_optional_positive_integer(raw, "max_output_bytes", 8_192),
            max_patch_bytes=_optional_positive_integer(raw, "max_patch_bytes", 65_536),
            max_patch_files=_optional_positive_integer(raw, "max_patch_files", 8),
            expected_test_count=expected_test_count,
            python_call_protocol=python_call_protocol,
            pristine_workspace_sha256=frozen_workspace.snapshot_sha256,
            fixture_manifest_bytes=canonical_json(raw).encode("utf-8"),
            _frozen_workspace=frozen_workspace,
            _frozen_hidden_tests=frozen_hidden_tests,
        )
        if python_call_protocol is None:
            return spec

        # Import lazily to keep the protocol module free to depend on these fixture
        # configuration types. The first validation canonicalizes the sealed bytes;
        # subsequent evaluator loads never consult the mutable fixture path again.
        from guildmind.evaluation.protocol import load_python_call_bundle

        bundle = load_python_call_bundle(
            python_call_protocol,
            expected_case_count=expected_test_count,
        )
        return replace(
            spec,
            python_call_protocol=replace(
                python_call_protocol,
                sealed_cases_bytes=bundle.oracle_bytes,
            ),
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
    execution: dict[str, JsonValue] = field(default_factory=dict)
    raw_candidate_stdout: bytes | None = field(default=None, repr=False)
    raw_scorer_stdout: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.raw_scorer_stdout is not None and self.raw_candidate_stdout is None:
            raise ValueError("a scorer transcript requires a candidate transcript")

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
        elif self.status is not EvaluationStatus.INFRASTRUCTURE_ERROR:
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
            "execution": self.execution,
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

    @property
    def evaluator_version(self) -> str:
        return "guildmind/local-fixture-v1"

    @property
    def environment_digest(self) -> str:
        digest = sha256_bytes(b"guildmind/local-fixture-evaluator-v1")
        return f"sha256:{digest}"

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
            with tempfile.TemporaryDirectory(prefix="guildmind-evaluation-") as temporary:
                temporary_root = Path(temporary)
                pristine_workspace = temporary_root / "pristine-workspace"
                spec.materialize_pristine_workspace(pristine_workspace)
                patched_workspace = temporary_root / "patched-workspace"
                validated = copy_and_apply_patch(
                    pristine_workspace,
                    patch_path,
                    patched_workspace,
                    policy,
                )
                _require_expected_patch_identity(
                    validated.data,
                    expected_patch_sha256=expected_patch_sha256,
                )

                evaluation_root = temporary_root / "evaluation"
                evaluation_workspace = evaluation_root / "workspace"
                grader_directory = evaluation_root / "grader"
                shutil.copytree(patched_workspace, evaluation_workspace, symlinks=True)
                spec.materialize_evaluator_tests(grader_directory)

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


def _require_expected_patch_identity(
    patch_data: bytes,
    *,
    expected_patch_sha256: str | None,
) -> str:
    """Bind exact validated patch bytes to their orchestration-supplied identity."""

    actual_patch_sha256 = sha256_bytes(patch_data)
    if expected_patch_sha256 is None:
        return actual_patch_sha256
    if _SHA256.fullmatch(expected_patch_sha256) is None:
        raise FixtureConfigurationError("expected patch identity must be a SHA-256 digest")
    if actual_patch_sha256 != expected_patch_sha256:
        raise FixtureConfigurationError(
            "validated patch bytes do not match the committed artifact identity: "
            f"expected {expected_patch_sha256}, observed {actual_patch_sha256}"
        )
    return actual_patch_sha256


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


def _freeze_workspace(source: Path) -> _FrozenWorkspace:
    """Capture one plain workspace tree and materialize only those captured bytes."""

    try:
        root_mode = source.lstat().st_mode
    except OSError as error:
        raise FixtureConfigurationError(f"fixture workspace is unavailable: {source}") from error
    if source.is_symlink() or not stat.S_ISDIR(root_mode):
        raise FixtureConfigurationError("fixture workspace must be a real directory")

    entries: list[_FrozenWorkspaceEntry] = []
    snapshot_files: list[JsonValue] = []
    try:
        paths = sorted(source.rglob("*"))
    except OSError as error:
        raise FixtureConfigurationError(f"cannot enumerate fixture workspace: {source}") from error
    for path in paths:
        relative = path.relative_to(source).as_posix()
        try:
            mode = path.lstat().st_mode
        except OSError as error:
            raise FixtureConfigurationError(
                f"cannot inspect fixture workspace entry: {path}"
            ) from error
        if any(part.casefold() == ".git" for part in Path(relative).parts):
            raise FixtureConfigurationError("fixture workspace contains forbidden Git metadata")
        if stat.S_ISLNK(mode):
            raise FixtureConfigurationError(f"fixture workspace contains a symlink: {path}")
        permissions = stat.S_IMODE(mode) & 0o777
        if stat.S_ISDIR(mode):
            entries.append(
                _FrozenWorkspaceEntry(
                    relative_path=relative,
                    mode=permissions,
                    data=None,
                )
            )
            continue
        if not stat.S_ISREG(mode):
            raise FixtureConfigurationError(f"fixture workspace contains a special file: {path}")
        data = _read_regular_file(path, label="fixture workspace file")
        entries.append(
            _FrozenWorkspaceEntry(
                relative_path=relative,
                mode=permissions,
                data=data,
            )
        )
        snapshot_files.append(
            {
                "content_base64": base64.b64encode(data).decode("ascii"),
                "path": relative,
                "sha256": sha256_bytes(data),
                "size_bytes": len(data),
            }
        )

    snapshot: dict[str, JsonValue] = {
        "files": snapshot_files,
        "schema_version": "guildmind.tree/v1",
    }
    snapshot_bytes = canonical_json(snapshot).encode("utf-8")
    temporary = tempfile.TemporaryDirectory(prefix="guildmind-frozen-fixture-")
    root = Path(temporary.name).resolve() / "workspace"
    _materialize_workspace_entries(tuple(entries), root)
    return _FrozenWorkspace(
        entries=tuple(entries),
        snapshot_bytes=snapshot_bytes,
        snapshot_sha256=sha256_bytes(snapshot_bytes),
        root=root,
        _temporary=temporary,
    )


def _materialize_workspace_entries(
    entries: tuple[_FrozenWorkspaceEntry, ...],
    root: Path,
) -> None:
    """Write captured entries into one new, plain workspace tree."""

    if os.path.lexists(root):
        raise FixtureConfigurationError(
            f"workspace materialization destination already exists: {root}"
        )
    root.mkdir(parents=True)
    directory_entries: list[tuple[Path, int]] = []
    for entry in entries:
        destination = root / entry.relative_path
        if entry.data is None:
            destination.mkdir(parents=True, exist_ok=True)
            directory_entries.append((destination, entry.mode))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(entry.data)
        destination.chmod(entry.mode)
    for directory, mode in sorted(
        directory_entries,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        directory.chmod(mode)


def _freeze_evaluator_tests(
    sources: tuple[Path, ...],
    *,
    destination: Path,
) -> tuple[tuple[Path, ...], tuple[_FrozenEvaluatorTest, ...]]:
    """Copy evaluator-owned test bytes into the same frozen fixture lifetime."""

    names = [source.name for source in sources]
    if len(names) != len(set(names)):
        raise FixtureConfigurationError("evaluator test filenames must be unique")
    captured: list[tuple[str, bytes, int]] = []
    for source in sources:
        try:
            mode = source.lstat().st_mode
        except OSError as error:
            raise FixtureConfigurationError(f"evaluator test does not exist: {source}") from error
        if source.is_symlink() or not stat.S_ISREG(mode):
            raise FixtureConfigurationError(f"evaluator test must be a regular file: {source}")
        captured.append(
            (
                source.name,
                _read_regular_file(source, label="evaluator test"),
                stat.S_IMODE(mode) & 0o777,
            )
        )

    destination.mkdir()
    frozen_paths: list[Path] = []
    frozen_tests: list[_FrozenEvaluatorTest] = []
    for name, data, mode in captured:
        frozen = destination / name
        frozen.write_bytes(data)
        frozen.chmod(mode)
        frozen_paths.append(frozen)
        frozen_tests.append(_FrozenEvaluatorTest(path=frozen, mode=mode, data=data))
    return tuple(frozen_paths), tuple(frozen_tests)


def _regular_file_mode(path: Path, *, label: str) -> int:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise FixtureConfigurationError(f"{label} is unavailable: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise FixtureConfigurationError(f"{label} must be a regular non-symlink file: {path}")
    return stat.S_IMODE(mode) & 0o777


def _read_regular_file(path: Path, *, label: str) -> bytes:
    """Read one regular file without following a final-component symlink."""

    try:
        before = path.lstat()
    except OSError as error:
        raise FixtureConfigurationError(f"{label} is unavailable: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise FixtureConfigurationError(f"{label} must be a regular non-symlink file: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FixtureConfigurationError(f"cannot open {label}: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise FixtureConfigurationError(f"{label} changed while loading: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    except OSError as error:
        raise FixtureConfigurationError(f"cannot read {label}: {path}") from error
    finally:
        os.close(descriptor)


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


def _required_positive_integer(raw: dict[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FixtureConfigurationError(f"{key} must be a positive integer")
    return value


def _optional_python_call_protocol(
    raw: dict[str, object],
    *,
    fixture_root: Path,
    workspace: Path,
) -> PythonCallProtocol | None:
    value = raw.get("evaluation_protocol")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FixtureConfigurationError("evaluation_protocol must be an object")
    required_keys = {"kind", "module", "callable", "cases_file"}
    if set(value) != required_keys:
        raise FixtureConfigurationError(
            "evaluation_protocol must contain exactly kind, module, callable, and cases_file"
        )
    kind = _required_string(value, "kind")
    if kind != "python-call-v1":
        raise FixtureConfigurationError(f"unsupported evaluation protocol: {kind}")
    cases_file = fixture_root / _plain_relative_path(_required_string(value, "cases_file"))
    if _is_within(cases_file, workspace) or _is_within(cases_file.resolve(), workspace.resolve()):
        raise FixtureConfigurationError("evaluation cases must be outside the submitted workspace")
    return PythonCallProtocol(
        module=_required_string(value, "module"),
        callable_name=_required_string(value, "callable"),
        cases_file=cases_file,
        sealed_cases_bytes=_read_regular_file(cases_file, label="evaluation cases"),
    )


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
