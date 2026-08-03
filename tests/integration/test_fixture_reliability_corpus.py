from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from guildmind.domain import sha256_bytes
from guildmind.evaluation import (
    EvaluationStatus,
    LocalEvaluationResult,
    LocalEvaluator,
    load_fixture,
    load_python_call_bundle,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIRST_BATCH = (
    ("002-slug-normalization", "slug.py"),
    ("003-interval-merge", "intervals.py"),
    ("004-json-pointer", "pointer.py"),
    ("005-stable-dedupe", "dedupe.py"),
)


def _assert_identical_results(
    results: tuple[LocalEvaluationResult, ...],
    expected_status: EvaluationStatus,
) -> None:
    assert len(results) == 3
    assert all(result.status is expected_status for result in results)
    assert all(result == results[0] for result in results[1:])


@pytest.mark.parametrize(("fixture_name", "implementation"), _FIRST_BATCH)
def test_first_fixture_batch_has_stable_pristine_and_gold_outcomes(
    fixture_name: str,
    implementation: str,
) -> None:
    fixture_root = _REPOSITORY_ROOT / "fixtures" / fixture_name
    spec = load_fixture(fixture_root)

    assert spec.task_id == f"fixture-{fixture_name}"
    assert spec.expected_test_count == 6
    assert spec.allowed_patch_paths == (implementation,)
    assert spec.python_call_protocol is not None
    assert all(
        not hidden_test.is_relative_to(spec.pristine_workspace)
        for hidden_test in spec.hidden_test_files
    )

    first_bundle = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    replay_bundle = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    assert first_bundle == replay_bundle
    assert first_bundle.case_count == 6
    assert b'"expected"' not in first_bundle.challenge_bytes
    assert b'"expected"' in first_bundle.oracle_bytes

    pristine_source = (spec.pristine_workspace / implementation).read_bytes()
    visible = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "test_visible.py"],
        cwd=spec.pristine_workspace,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert visible.returncode != 0

    evaluator = LocalEvaluator()
    pristine_patch = fixture_root / "controls" / "pristine.patch"
    pristine_sha256 = sha256_bytes(pristine_patch.read_bytes())
    gold_patch = fixture_root / "solution.patch"
    gold_sha256 = sha256_bytes(gold_patch.read_bytes())
    pristine_results = tuple(
        evaluator.evaluate(
            spec,
            pristine_patch,
            expected_patch_sha256=pristine_sha256,
        )
        for _ in range(3)
    )
    gold_results = tuple(
        evaluator.evaluate(
            spec,
            gold_patch,
            expected_patch_sha256=gold_sha256,
        )
        for _ in range(3)
    )

    _assert_identical_results(pristine_results, EvaluationStatus.TESTS_FAILED)
    _assert_identical_results(gold_results, EvaluationStatus.PASSED)
    assert (spec.pristine_workspace / implementation).read_bytes() == pristine_source
