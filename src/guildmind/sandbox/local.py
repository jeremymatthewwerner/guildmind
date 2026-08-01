"""Conservative patch handling for the local evaluation adapter.

This module reduces accidental filesystem damage while running deterministic, trusted
fixtures. It is explicitly *not* a security sandbox: it does not isolate processes,
credentials, the network, the kernel, or the host filesystem from evaluated code.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>[^ ]+) b/(?P<new>[^ ]+)$")
_INDEX_HEADER = re.compile(
    r"^index [0-9a-fA-F]{4,64}\.\.[0-9a-fA-F]{4,64}(?: (?P<mode>[0-7]{6}))?$"
)
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
_NO_NEWLINE_MARKER = "\\ No newline at end of file"
_DEFAULT_GIT_TIMEOUT_SECONDS = 5.0
_MAX_GIT_DIAGNOSTIC_BYTES = 4_096


class PatchValidationError(ValueError):
    """Raised when a patch is outside the deliberately narrow accepted format."""


class PatchApplyError(RuntimeError):
    """Raised when a validated patch does not apply cleanly."""


@dataclass(frozen=True, slots=True)
class PatchPolicy:
    """Limits for one trusted fixture's scripted patch."""

    allowed_paths: tuple[str, ...]
    max_patch_bytes: int = 65_536
    max_files: int = 8

    def __post_init__(self) -> None:
        if not self.allowed_paths:
            raise ValueError("allowed_paths must not be empty")
        if self.max_patch_bytes <= 0:
            raise ValueError("max_patch_bytes must be positive")
        if self.max_files <= 0:
            raise ValueError("max_files must be positive")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("allowed_paths must not contain duplicates")
        for path in self.allowed_paths:
            _validate_relative_path(path)


@dataclass(frozen=True, slots=True)
class ValidatedPatch:
    """Patch bytes plus the normalized workspace paths they modify."""

    data: bytes
    paths: tuple[str, ...]


def validate_patch(
    patch_path: Path,
    workspace: Path,
    policy: PatchPolicy,
) -> ValidatedPatch:
    """Read and validate a narrowly formatted, text-only Git unified diff."""

    _ensure_regular_file(patch_path, label="patch")
    patch_size = patch_path.stat().st_size
    if patch_size > policy.max_patch_bytes:
        raise PatchValidationError(
            f"patch is {patch_size} bytes; limit is {policy.max_patch_bytes} bytes"
        )

    data = patch_path.read_bytes()
    if len(data) > policy.max_patch_bytes:
        raise PatchValidationError("patch grew beyond its size limit while being read")
    if b"\x00" in data:
        raise PatchValidationError("binary patches are not supported")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PatchValidationError("patch must be UTF-8 text") from error
    if not text.endswith("\n"):
        raise PatchValidationError("patch must end with a newline")
    if "\r" in text:
        raise PatchValidationError("patch must use Unix newlines")
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in text):
        raise PatchValidationError("patch contains unsupported control characters")

    paths = _parse_unified_diff(text, policy)
    _ensure_workspace_is_plain_tree(workspace)
    for path in paths:
        _ensure_workspace_target_is_regular(workspace, path)
    return ValidatedPatch(data=data, paths=paths)


def copy_and_apply_patch(
    source_workspace: Path,
    patch_path: Path,
    destination_workspace: Path,
    policy: PatchPolicy,
) -> ValidatedPatch:
    """Copy a pristine workspace and apply one validated patch to the copy."""

    if os.path.lexists(destination_workspace):
        raise PatchApplyError(f"destination already exists: {destination_workspace}")

    validated = validate_patch(patch_path, source_workspace, policy)
    destination_workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_workspace, destination_workspace, symlinks=True)
    _ensure_workspace_is_plain_tree(destination_workspace)

    command = ["git", "apply", "--whitespace=error-all", "-"]
    check_command = [*command[:2], "--check", *command[2:]]
    checked = _run_git_apply(check_command, validated.data, destination_workspace)
    if checked.returncode != 0:
        raise PatchApplyError(_format_git_error("patch does not apply cleanly", checked.stderr))

    applied = _run_git_apply(command, validated.data, destination_workspace)
    if applied.returncode != 0:
        raise PatchApplyError(_format_git_error("git failed while applying patch", applied.stderr))

    return validated


def _parse_unified_diff(text: str, policy: PatchPolicy) -> tuple[str, ...]:
    lines = text.splitlines()
    paths: list[str] = []
    line_index = 0
    total_changes = 0

    while line_index < len(lines):
        diff_match = _DIFF_HEADER.fullmatch(lines[line_index])
        if diff_match is None:
            raise PatchValidationError(f"expected a Git diff header at patch line {line_index + 1}")
        old_path = diff_match.group("old")
        new_path = diff_match.group("new")
        if old_path != new_path:
            raise PatchValidationError("renames and copies are not supported")
        _validate_relative_path(old_path)
        if old_path not in policy.allowed_paths:
            raise PatchValidationError(f"patch modifies an unexpected path: {old_path}")
        if old_path in paths:
            raise PatchValidationError(f"patch contains duplicate sections for: {old_path}")
        paths.append(old_path)
        if len(paths) > policy.max_files:
            raise PatchValidationError(f"patch modifies more than {policy.max_files} files")
        line_index += 1

        if line_index < len(lines) and lines[line_index].startswith("index "):
            index_match = _INDEX_HEADER.fullmatch(lines[line_index])
            if index_match is None:
                raise PatchValidationError("malformed or unsupported Git index header")
            mode = index_match.group("mode")
            if mode is not None and mode != "100644":
                raise PatchValidationError("symlink and submodule modes are not supported")
            line_index += 1

        expected_old = f"--- a/{old_path}"
        expected_new = f"+++ b/{new_path}"
        if line_index >= len(lines) or lines[line_index] != expected_old:
            raise PatchValidationError(
                "new/deleted files, binary patches, and extended Git headers are not supported"
            )
        line_index += 1
        if line_index >= len(lines) or lines[line_index] != expected_new:
            raise PatchValidationError("malformed new-file marker in patch")
        line_index += 1

        section_hunks = 0
        section_changes = 0
        while line_index < len(lines) and lines[line_index].startswith("@@"):
            line_index, changes = _parse_hunk(lines, line_index)
            section_hunks += 1
            section_changes += changes
        if section_hunks == 0 or section_changes == 0:
            raise PatchValidationError("each patched file must contain a non-empty hunk")
        total_changes += section_changes

        if line_index < len(lines) and not lines[line_index].startswith("diff --git "):
            raise PatchValidationError(f"unsupported patch content at patch line {line_index + 1}")

    if not paths or total_changes == 0:
        raise PatchValidationError("patch must modify at least one allowed file")
    return tuple(paths)


def _parse_hunk(lines: list[str], header_index: int) -> tuple[int, int]:
    header = _HUNK_HEADER.fullmatch(lines[header_index])
    if header is None:
        raise PatchValidationError(f"malformed hunk header at patch line {header_index + 1}")

    old_count = int(header.group("old_count") or "1")
    new_count = int(header.group("new_count") or "1")
    old_seen = 0
    new_seen = 0
    changes = 0
    line_index = header_index + 1

    while old_seen < old_count or new_seen < new_count:
        if line_index >= len(lines):
            raise PatchValidationError("hunk ended before its declared line counts")
        line = lines[line_index]
        if line == _NO_NEWLINE_MARKER:
            if line_index == header_index + 1:
                raise PatchValidationError("misplaced no-newline marker")
            line_index += 1
            continue
        prefix = line[:1]
        if prefix == " ":
            old_seen += 1
            new_seen += 1
        elif prefix == "-":
            old_seen += 1
            changes += 1
        elif prefix == "+":
            new_seen += 1
            changes += 1
        else:
            raise PatchValidationError(f"malformed hunk line at patch line {line_index + 1}")
        if old_seen > old_count or new_seen > new_count:
            raise PatchValidationError("hunk contains more lines than its header declares")
        line_index += 1

    while line_index < len(lines) and lines[line_index] == _NO_NEWLINE_MARKER:
        line_index += 1
    return line_index, changes


def _validate_relative_path(path: str) -> None:
    if not path or not _SAFE_PATH.fullmatch(path):
        raise PatchValidationError(f"unsupported patch path: {path!r}")
    if path.startswith("/") or path.endswith("/") or "//" in path or "\\" in path:
        raise PatchValidationError(f"patch path is not a plain relative path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PatchValidationError(f"patch path contains traversal: {path!r}")
    if parts[0] == ".git":
        raise PatchValidationError("patches may not modify Git metadata")


def _ensure_regular_file(path: Path, *, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as error:
        raise PatchValidationError(f"{label} does not exist: {path}") from error
    if stat.S_ISLNK(mode):
        raise PatchValidationError(f"{label} may not be a symlink: {path}")
    if not stat.S_ISREG(mode):
        raise PatchValidationError(f"{label} must be a regular file: {path}")


def _ensure_workspace_is_plain_tree(workspace: Path) -> None:
    try:
        root_mode = workspace.lstat().st_mode
    except FileNotFoundError as error:
        raise PatchValidationError(f"workspace does not exist: {workspace}") from error
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise PatchValidationError("workspace must be a real directory, not a symlink")

    for root, directory_names, file_names in os.walk(workspace, followlinks=False):
        root_path = Path(root)
        for name in [*directory_names, *file_names]:
            candidate = root_path / name
            mode = candidate.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise PatchValidationError(f"workspace contains a symlink: {candidate}")
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise PatchValidationError(f"workspace contains a special file: {candidate}")


def _ensure_workspace_target_is_regular(workspace: Path, relative_path: str) -> None:
    candidate = workspace
    for part in relative_path.split("/"):
        candidate = candidate / part
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as error:
            raise PatchValidationError(f"patch target does not exist: {relative_path}") from error
        if stat.S_ISLNK(mode):
            raise PatchValidationError(f"patch target traverses a symlink: {relative_path}")
    if not stat.S_ISREG(candidate.lstat().st_mode):
        raise PatchValidationError(f"patch target is not a regular file: {relative_path}")


def _run_git_apply(
    command: list[str],
    patch: bytes,
    workspace: Path,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            cwd=workspace,
            input=patch,
            capture_output=True,
            check=False,
            timeout=_DEFAULT_GIT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise PatchApplyError("git is required by the local patch adapter") from error
    except subprocess.TimeoutExpired as error:
        raise PatchApplyError("git apply exceeded its local timeout") from error


def _format_git_error(prefix: str, stderr: bytes) -> str:
    diagnostic = stderr[:_MAX_GIT_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace").strip()
    return f"{prefix}: {diagnostic}" if diagnostic else prefix
