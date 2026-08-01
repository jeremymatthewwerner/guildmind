"""Image-owned unittest harness for trusted fixtures, not hostile candidate code.

Candidate modules currently load into this interpreter and can read the grader mount.
The repository gate report and strict expected-failure test track the required redesign.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path

_INPUT = Path("/inputs/workspace")
_GRADER = Path("/inputs/grader")
_WORK_ROOT = Path("/workspace")
_WORKSPACE = _WORK_ROOT / "repository"
_RESULT_PREFIX = "GUILDMIND_EVALUATION_RESULT="


def _require_plain_tree(root: Path, *, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f"{label} must be a real directory")
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if path.name == ".git":
            raise RuntimeError(f"{label} contains forbidden Git metadata")
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError(f"{label} contains a link or special file")


def _emit(payload: dict[str, object]) -> None:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    print(f"{_RESULT_PREFIX}{encoded}", flush=True)


def main() -> int:
    try:
        expected_tests = int(os.environ["GUILDMIND_EXPECTED_TESTS"])
        if expected_tests <= 0:
            raise ValueError("expected test count must be positive")
        _require_plain_tree(_INPUT, label="workspace input")
        _require_plain_tree(_GRADER, label="grader input")
        if any(_WORK_ROOT.iterdir()):
            raise RuntimeError("writable workspace is not empty")
        shutil.copytree(_INPUT, _WORKSPACE, symlinks=False)
        os.chdir(_WORKSPACE)
        sys.path.insert(0, str(_WORKSPACE))

        suite = unittest.defaultTestLoader.discover(
            str(_GRADER),
            pattern="test_*.py",
            top_level_dir=str(_GRADER),
        )
        discovered_tests = suite.countTestCases()
        result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
        payload: dict[str, object] = {
            "discovered_tests": discovered_tests,
            "errors": len(result.errors),
            "expected_tests": expected_tests,
            "failures": len(result.failures),
            "schema_version": "guildmind.evaluator-completion/v1",
            "skipped": len(result.skipped),
            "successful": (
                result.wasSuccessful() and not result.skipped and discovered_tests == expected_tests
            ),
            "tests_run": result.testsRun,
        }
        _emit(payload)
        return 0 if payload["successful"] else 1
    except BaseException as error:
        _emit(
            {
                "error": f"{type(error).__name__}: {error}",
                "schema_version": "guildmind.evaluator-completion/v1",
                "successful": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
