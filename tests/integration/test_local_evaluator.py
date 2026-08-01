from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guildmind.domain import ArtifactRef, RunStatus, TaskSpec, canonical_sha256
from guildmind.evaluation import EvaluationStatus, LocalEvaluator, load_fixture

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


def test_fixture_starts_with_a_visible_failure_and_solution_passes() -> None:
    spec = load_fixture(_FIXTURE)
    visible = subprocess.run(
        [sys.executable, "-m", "unittest", "test_visible.py"],
        cwd=spec.pristine_workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        check=False,
        timeout=5,
    )
    before = (spec.pristine_workspace / "addition.py").read_text(encoding="utf-8")

    result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")
    replay = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")

    assert visible.returncode != 0
    assert result.status is EvaluationStatus.PASSED
    assert replay == result
    assert result.passed
    assert result.exit_code == 0
    assert (spec.pristine_workspace / "addition.py").read_text(encoding="utf-8") == before
    assert "left - right" in before


def test_local_result_converts_to_content_verified_domain_result() -> None:
    spec = load_fixture(_FIXTURE)
    local_result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")
    artifact = ArtifactRef(
        media_type="text/plain",
        size_bytes=1,
        sha256="a" * 64,
        storage_ref="fixture://artifact",
    )
    task = TaskSpec(
        task_id=spec.task_id,
        source="repository-fixture-v1",
        split="fixture",
        repository="guildmind/001-python-addition",
        repository_commit="fixture-v1",
        image_digest=f"sha256:{'b' * 64}",
        task_content_hash="c" * 64,
        problem_statement=artifact,
        repository_snapshot=artifact,
    )

    domain_result = local_result.to_domain_result(
        task,
        evaluation_id="evaluation-001",
        run_id="run-001",
        run_status=RunStatus.SUCCEEDED,
        evaluator_version="local-fixture-v1",
        patch_hash="d" * 64,
        evaluated_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    assert domain_result.outcome == "passed"
    assert domain_result.score == 1.0
    assert domain_result.task_hash == task.task_content_hash
    assert domain_result.result_sha256 == canonical_sha256(domain_result.result)


def test_visible_only_patch_fails_the_hidden_evaluator(tmp_path: Path) -> None:
    patch = tmp_path / "visible-only.patch"
    patch.write_text(
        """diff --git a/addition.py b/addition.py
--- a/addition.py
+++ b/addition.py
@@ -7 +7 @@ def add(left: int, right: int) -> int:
-    return left - right
+    return left + right if left >= 0 and right >= 0 else 0
""",
        encoding="utf-8",
    )

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)
    replay = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert replay == result
    assert not result.passed
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert "guildmind-evaluation-" not in result.stderr


def test_cleanly_rejects_a_patch_with_stale_context(tmp_path: Path) -> None:
    patch = tmp_path / "stale.patch"
    patch.write_text(
        """diff --git a/addition.py b/addition.py
--- a/addition.py
+++ b/addition.py
@@ -7 +7 @@
-    return a completely different expression
+    return left + right
""",
        encoding="utf-8",
    )

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.PATCH_APPLY_FAILED
    assert result.exit_code is None


def test_authoritative_evaluation_includes_the_visible_regression(tmp_path: Path) -> None:
    patch = tmp_path / "hidden-only.patch"
    patch.write_text(
        """diff --git a/addition.py b/addition.py
--- a/addition.py
+++ b/addition.py
@@ -7 +7 @@ def add(left: int, right: int) -> int:
-    return left - right
+    return left - right if left > 0 and right > 0 else left + right
""",
        encoding="utf-8",
    )

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert "test_adds_two_positive_integers" in result.stderr


@pytest.mark.parametrize(
    "patch_text",
    [
        """diff --git /etc/passwd /etc/passwd
--- /etc/passwd
+++ /etc/passwd
@@ -1 +1 @@
-old
+new
""",
        """diff --git a/../escape.py b/../escape.py
--- a/../escape.py
+++ b/../escape.py
@@ -1 +1 @@
-old
+new
""",
        """diff --git a/test_visible.py b/test_visible.py
--- a/test_visible.py
+++ b/test_visible.py
@@ -1 +1 @@
-import unittest
+import unittest as unittest
""",
        """diff --git a/addition.py b/addition.py
new file mode 160000
index 0000000..1111111
--- /dev/null
+++ b/addition.py
@@ -0,0 +1 @@
+Subproject commit 1111111111111111111111111111111111111111
""",
        """diff --git a/addition.py b/addition.py
GIT binary patch
literal 1
Ac$@<O00001
""",
    ],
)
def test_rejects_unsafe_or_unexpected_patch_shapes(tmp_path: Path, patch_text: str) -> None:
    patch = tmp_path / "unsafe.patch"
    patch.write_text(patch_text, encoding="utf-8")

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.INVALID_PATCH
    assert result.exit_code is None


def test_rejects_oversized_patches(tmp_path: Path) -> None:
    patch = tmp_path / "oversized.patch"
    patch.write_bytes(b"x" * 33)
    spec = replace(load_fixture(_FIXTURE), max_patch_bytes=32)

    result = LocalEvaluator().evaluate(spec, patch)

    assert result.status is EvaluationStatus.INVALID_PATCH


def test_rejects_symlinks_in_the_source_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(_FIXTURE / "workspace", workspace)
    target = tmp_path / "outside.py"
    target.write_text("def add(left: int, right: int) -> int:\n    return left + right\n")
    (workspace / "addition.py").unlink()
    (workspace / "addition.py").symlink_to(target)
    spec = replace(load_fixture(_FIXTURE), pristine_workspace=workspace)

    result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.INVALID_PATCH


def test_caps_retained_subprocess_output(tmp_path: Path) -> None:
    hidden_test = tmp_path / "test_noisy.py"
    hidden_test.write_text(
        """import unittest

print("x" * 10000)

class NoisyFailure(unittest.TestCase):
    def test_fails(self) -> None:
        self.fail("expected")
""",
        encoding="utf-8",
    )
    spec = replace(
        load_fixture(_FIXTURE),
        hidden_test_files=(hidden_test,),
        max_output_bytes=128,
    )

    result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert result.output_truncated
    assert len(result.stdout.encode("utf-8")) <= 128
    assert "output truncated" in result.stdout


def test_terminates_tests_at_the_configured_timeout(tmp_path: Path) -> None:
    hidden_test = tmp_path / "test_slow.py"
    hidden_test.write_text(
        """import time
import unittest

class SlowTest(unittest.TestCase):
    def test_sleeps(self) -> None:
        time.sleep(5)
""",
        encoding="utf-8",
    )
    spec = replace(
        load_fixture(_FIXTURE),
        hidden_test_files=(hidden_test,),
        timeout_seconds=0.2,
    )

    result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")

    assert result.status is EvaluationStatus.TIMED_OUT
    assert not result.passed


def test_background_children_cannot_hold_capture_pipes_open(tmp_path: Path) -> None:
    hidden_test = tmp_path / "test_background.py"
    hidden_test.write_text(
        """import subprocess
import sys
import unittest

class BackgroundProcessTest(unittest.TestCase):
    def test_leaves_a_child(self) -> None:
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
""",
        encoding="utf-8",
    )
    spec = replace(
        load_fixture(_FIXTURE),
        hidden_test_files=(hidden_test,),
        timeout_seconds=0.3,
    )

    started = time.monotonic()
    result = LocalEvaluator().evaluate(spec, _FIXTURE / "solution.patch")
    elapsed = time.monotonic() - started

    assert result.status is EvaluationStatus.PASSED
    assert elapsed < 1.0
