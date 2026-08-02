from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from guildmind.domain import ArtifactRef, RunStatus, TaskSpec, canonical_sha256, sha256_bytes
from guildmind.evaluation import (
    EvaluationStatus,
    FixtureConfigurationError,
    LocalEvaluator,
    load_fixture,
)
from guildmind.sandbox import PatchPolicy, PatchValidationError, copy_and_apply_patch

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


def test_loaded_fixture_evaluates_from_frozen_bytes_after_source_removal(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(_FIXTURE, fixture)
    patch = tmp_path / "solution.patch"
    shutil.copyfile(fixture / "solution.patch", patch)
    spec = load_fixture(fixture)

    assert spec.pristine_workspace_snapshot_bytes is not None
    assert spec.pristine_workspace_sha256 == sha256_bytes(spec.pristine_workspace_snapshot_bytes)
    assert spec.pristine_workspace.resolve() != (fixture / "workspace").resolve()

    shutil.rmtree(fixture / "workspace")
    shutil.rmtree(fixture / "grader")

    result = LocalEvaluator().evaluate(spec, patch)

    assert result.status is EvaluationStatus.PASSED


def test_loaded_fixture_ignores_mutation_of_frozen_materialization(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(_FIXTURE, fixture)
    spec = load_fixture(fixture)

    shutil.rmtree(spec.pristine_workspace)
    spec.pristine_workspace.write_bytes(b"corrupted workspace materialization")
    for hidden_test in spec.hidden_test_files:
        hidden_test.write_text("raise RuntimeError('corrupted hidden test')\n", encoding="utf-8")

    result = LocalEvaluator().evaluate(spec, fixture / "solution.patch")

    assert result.status is EvaluationStatus.PASSED


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


@pytest.mark.parametrize(
    "patch_name",
    ["no-op.patch", "visible-only.patch", "wrong-operation.patch"],
)
def test_checked_in_functional_controls_fail_the_local_evaluator(patch_name: str) -> None:
    patch = _FIXTURE / "adversarial" / patch_name

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)
    replay = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.TESTS_FAILED
    assert replay == result
    assert not result.passed
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert "guildmind-evaluation-" not in result.stderr


def test_visible_only_control_passes_visible_tests_before_hidden_failure(
    tmp_path: Path,
) -> None:
    spec = load_fixture(_FIXTURE)
    patch = _FIXTURE / "adversarial" / "visible-only.patch"
    patched_workspace = tmp_path / "patched-workspace"
    copy_and_apply_patch(
        spec.pristine_workspace,
        patch,
        patched_workspace,
        PatchPolicy(
            allowed_paths=spec.allowed_patch_paths,
            max_patch_bytes=spec.max_patch_bytes,
        ),
    )

    visible = subprocess.run(
        [sys.executable, "-m", "unittest", "test_visible.py"],
        cwd=patched_workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        check=False,
        timeout=5,
    )
    authoritative = LocalEvaluator().evaluate(spec, patch)

    assert visible.returncode == 0
    assert authoritative.status is EvaluationStatus.TESTS_FAILED


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


def test_rejects_compressed_patch_bytes_without_decompression(tmp_path: Path) -> None:
    patch = tmp_path / "compressed.patch"
    patch.write_bytes(gzip.compress((_FIXTURE / "solution.patch").read_bytes()))

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.INVALID_PATCH


def test_rejects_absurd_hunk_numbers_as_invalid_patch(tmp_path: Path) -> None:
    patch = tmp_path / "huge-hunk-count.patch"
    patch.write_text(
        "diff --git a/addition.py b/addition.py\n"
        "--- a/addition.py\n"
        "+++ b/addition.py\n"
        f"@@ -1,{'9' * 5000} +1 @@\n"
        "-old\n"
        "+new\n",
        encoding="utf-8",
    )
    spec = replace(load_fixture(_FIXTURE), max_patch_bytes=8_192)

    result = LocalEvaluator().evaluate(spec, patch)

    assert result.status is EvaluationStatus.INVALID_PATCH
    assert "hunk number is too large" in result.stderr


def test_rejects_a_symlinked_patch_file(tmp_path: Path) -> None:
    patch = tmp_path / "linked.patch"
    patch.symlink_to(_FIXTURE / "solution.patch")

    result = LocalEvaluator().evaluate(load_fixture(_FIXTURE), patch)

    assert result.status is EvaluationStatus.INVALID_PATCH
    assert "patch may not be a symlink" in result.stderr


def test_rejects_patch_replaced_by_symlink_while_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = tmp_path / "racing.patch"
    shutil.copyfile(_FIXTURE / "solution.patch", patch)
    replacement = tmp_path / "replacement.patch"
    shutil.copyfile(_FIXTURE / "solution.patch", replacement)
    spec = load_fixture(_FIXTURE)
    original_open = os.open
    replaced = False

    def replace_before_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if not replaced and Path(path) == patch:
            replaced = True
            patch.unlink()
            patch.symlink_to(replacement)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)

    result = LocalEvaluator().evaluate(spec, patch)

    assert replaced
    assert result.status is EvaluationStatus.INVALID_PATCH
    assert (
        "without following links" in result.stderr or "changed while being opened" in result.stderr
    )


def test_patch_policy_rejects_case_insensitive_nested_git_metadata() -> None:
    with pytest.raises(PatchValidationError, match="Git metadata"):
        PatchPolicy(allowed_paths=("src/.GIT/config",))


def test_fixture_rejects_case_insensitive_git_metadata(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(_FIXTURE, fixture)
    metadata = fixture / "workspace" / ".GIT"
    metadata.mkdir()
    (metadata / "config").write_text("[core]\n\trepositoryformatversion = 0\n")

    with pytest.raises(FixtureConfigurationError, match="forbidden Git metadata"):
        load_fixture(fixture)


def test_rejects_patch_bytes_that_do_not_match_the_committed_identity() -> None:
    result = LocalEvaluator().evaluate(
        load_fixture(_FIXTURE),
        _FIXTURE / "solution.patch",
        expected_patch_sha256="0" * 64,
    )

    assert result.status is EvaluationStatus.INFRASTRUCTURE_ERROR
    assert "do not match the committed artifact identity" in result.stderr


def test_rejects_symlinks_in_the_source_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(_FIXTURE / "workspace", workspace)
    target = tmp_path / "outside.py"
    target.write_text("def add(left: int, right: int) -> int:\n    return left + right\n")
    (workspace / "addition.py").unlink()
    (workspace / "addition.py").symlink_to(target)
    spec = replace(
        load_fixture(_FIXTURE),
        pristine_workspace=workspace,
        _frozen_workspace=None,
    )

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
