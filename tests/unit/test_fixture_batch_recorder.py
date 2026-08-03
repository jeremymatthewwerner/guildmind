from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue

from guildmind.evaluation import require_tracked_clean_revision, write_new_report


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_git_revision_requires_tracked_clean_state_but_ignores_untracked_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    tracked = repository / "tracked.txt"
    tracked.write_text("frozen\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(
        repository,
        "-c",
        "user.name=Guildmind Test",
        "-c",
        "user.email=guildmind@example.invalid",
        "commit",
        "-m",
        "freeze",
    )

    expected_revision = _git(repository, "rev-parse", "HEAD").stdout.strip()
    assert require_tracked_clean_revision(repository) == expected_revision
    (repository / "untracked.txt").write_text("outside evidence\n", encoding="utf-8")
    assert require_tracked_clean_revision(repository) == expected_revision

    tracked.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="tracked files must be clean"):
        require_tracked_clean_revision(repository)


def test_report_writer_is_canonical_durable_and_no_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "report.json"
    report: dict[str, JsonValue] = {"z": 2, "a": "first"}
    write_new_report(output, report)

    assert output.read_bytes() == b'{"a":"first","z":2}\n'
    assert json.loads(output.read_bytes()) == report
    with pytest.raises(FileExistsError):
        write_new_report(output, {"a": "replacement"})
    assert output.read_bytes() == b'{"a":"first","z":2}\n'
