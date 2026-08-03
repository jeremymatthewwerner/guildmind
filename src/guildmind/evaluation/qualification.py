"""Durable helpers for recording fixture qualification evidence."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import uuid
from pathlib import Path

from pydantic import JsonValue

from guildmind.domain import canonical_json
from guildmind.storage._fsops import rename_noreplace_at


def require_tracked_clean_revision(repository: Path) -> str:
    """Return the full Git revision after requiring a tracked-clean worktree."""

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=no"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout:
        raise RuntimeError("repository tracked files must be clean before qualification")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise RuntimeError("Git did not return a full commit revision")
    return revision


def write_new_report(path: Path, report: dict[str, JsonValue]) -> None:
    """Write canonical report bytes durably while refusing replacement."""

    lexical = Path(os.path.abspath(path))
    lexical.parent.mkdir(parents=True, exist_ok=True)
    parent = lexical.parent.resolve(strict=True)
    if parent != lexical.parent:
        raise ValueError("report output parent must not traverse symlinks")
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("report output parent must be a real directory")
    output_name = lexical.name
    if not output_name or output_name in {".", ".."}:
        raise ValueError("report output name is invalid")
    data = (canonical_json(report) + "\n").encode("utf-8")
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_descriptor = os.open(parent, parent_flags)
    temporary_name = f".{output_name}.tmp-{uuid.uuid4().hex}"
    temporary_created = False
    try:
        file_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(temporary_name, file_flags, 0o600, dir_fd=parent_descriptor)
        temporary_created = True
        try:
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != len(data)
            ):
                raise OSError("report temporary file failed identity validation")
        finally:
            os.close(descriptor)
        rename_noreplace_at(
            parent_descriptor,
            temporary_name,
            parent_descriptor,
            output_name,
        )
        temporary_created = False
        os.fsync(parent_descriptor)
    finally:
        if temporary_created:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
            except FileNotFoundError:
                pass
        os.close(parent_descriptor)
