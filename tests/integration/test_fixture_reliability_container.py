from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import pytest

from guildmind.domain import sha256_bytes
from guildmind.evaluation import (
    ContainerEvaluator,
    EvaluationStatus,
    LocalEvaluationResult,
    load_fixture,
)
from guildmind.sandbox import DockerHostPolicy, DockerSandbox

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIRST_BATCH = (
    "002-slug-normalization",
    "003-interval-merge",
    "004-json-pointer",
    "005-stable-dedupe",
)


def _evaluate_three_times(
    evaluator: ContainerEvaluator,
    *,
    fixture_root: Path,
    patch_path: Path,
) -> tuple[LocalEvaluationResult, ...]:
    patch_sha256 = sha256_bytes(patch_path.read_bytes())
    spec = load_fixture(fixture_root)
    return tuple(
        evaluator.evaluate(
            spec,
            patch_path,
            expected_patch_sha256=patch_sha256,
        )
        for _ in range(3)
    )


def _assert_repeated_container_result(
    results: tuple[LocalEvaluationResult, ...],
    *,
    expected_status: EvaluationStatus,
    image: str,
) -> None:
    assert len(results) == 3
    assert all(result == results[0] for result in results[1:])
    for result in results:
        assert result.status is expected_status
        assert result.output_truncated is False
        assert result.execution["image_reference"] == image
        assert result.execution["evaluator_version"] == "guildmind/container-python-call-v2"
        assert result.execution["protocol"] == "python-call-v1"
        assert result.execution["expected_tests"] == 6
        assert result.raw_candidate_stdout is not None
        assert result.raw_scorer_stdout is not None

        candidate = cast(dict[str, object], result.execution["candidate"])
        scorer = cast(dict[str, object], result.execution["scorer"])
        completion = cast(dict[str, object], result.execution["completion"])
        assert candidate["sandbox_status"] == "exited"
        assert scorer["sandbox_status"] == "exited"
        assert candidate["output_truncated"] is False
        assert scorer["output_truncated"] is False
        assert candidate["image_id"] == scorer["image_id"]
        assert completion["errors"] == 0
        assert completion["skipped"] == 0
        assert completion["tests_run"] == 6
        assert completion["expected_tests"] == 6

        if expected_status is EvaluationStatus.PASSED:
            assert result.exit_code == 0
            assert completion["classification"] == "passed"
            assert completion["successful"] is True
            assert completion["failures"] == 0
        else:
            assert result.exit_code == 1
            assert completion["classification"] == "candidate_failed"
            assert completion["successful"] is False
            failures = completion["failures"]
            assert isinstance(failures, int) and failures > 0


@pytest.mark.container
@pytest.mark.parametrize("fixture_name", _FIRST_BATCH)
def test_first_fixture_batch_repeats_pristine_failure_and_gold_pass_in_container(
    fixture_name: str,
) -> None:
    image = os.environ.get("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE")
    if image is None:
        pytest.skip("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE is not configured")

    fixture_root = _REPOSITORY_ROOT / "fixtures" / fixture_name
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=image,
    )
    pristine_results = _evaluate_three_times(
        evaluator,
        fixture_root=fixture_root,
        patch_path=fixture_root / "controls" / "pristine.patch",
    )
    gold_results = _evaluate_three_times(
        evaluator,
        fixture_root=fixture_root,
        patch_path=fixture_root / "solution.patch",
    )

    _assert_repeated_container_result(
        pristine_results,
        expected_status=EvaluationStatus.TESTS_FAILED,
        image=image,
    )
    _assert_repeated_container_result(
        gold_results,
        expected_status=EvaluationStatus.PASSED,
        image=image,
    )
