from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from guildmind.domain import canonical_sha256, sha256_bytes
from guildmind.evaluation import (
    ContainerEvaluator,
    EvaluationStatus,
    LocalEvaluationResult,
    load_fixture,
)
from guildmind.sandbox import DockerHostPolicy, DockerSandbox

_REPOSITORY_ROOT = Path(__file__).parents[2]
_REPORT = (
    _REPOSITORY_ROOT
    / "docs"
    / "evidence"
    / "fixture-qualification"
    / "2026-08-03-batch-001-development-container"
    / "report.json"
)
_SECOND_REPORT = (
    _REPOSITORY_ROOT
    / "docs"
    / "evidence"
    / "fixture-qualification"
    / "2026-08-03-batch-002-development-container"
    / "report.json"
)
_FIRST_BATCH = (
    "002-slug-normalization",
    "003-interval-merge",
    "004-json-pointer",
    "005-stable-dedupe",
)
_SECOND_BATCH = (
    "006-run-decoder",
    "007-apportionment",
    "008-topological-order",
    "009-ordered-changes",
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
    expected_evidence: dict[str, object],
    image: str,
) -> None:
    assert len(results) == 3
    assert all(result == results[0] for result in results[1:])
    assert (
        canonical_sha256(_result_payload(results[0])) == expected_evidence["stable_result_sha256"]
    )
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
        assert (
            result.execution["evaluation_binding_sha256"]
            == expected_evidence["evaluation_binding_sha256"]
        )
        assert result.execution["response_sha256"] == expected_evidence["response_sha256"]
        assert (
            result.execution["trusted_completion_record_sha256"]
            == expected_evidence["trusted_completion_record_sha256"]
        )
        assert completion == expected_evidence["completion"]
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


def _result_payload(result: LocalEvaluationResult) -> dict[str, object]:
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "output_truncated": result.output_truncated,
        "execution": result.execution,
        "candidate_stdout_sha256": sha256_bytes(result.raw_candidate_stdout or b""),
        "scorer_stdout_sha256": sha256_bytes(result.raw_scorer_stdout or b""),
    }


def _load_expected_outcome(fixture_name: str, outcome_name: str) -> dict[str, object]:
    expected_fixture_id = f"fixture-{fixture_name}"
    for report_path in (_REPORT, _SECOND_REPORT):
        report = cast(dict[str, object], json.loads(report_path.read_bytes()))
        fixture_entries = cast(list[object], report["fixtures"])
        for raw_entry in fixture_entries:
            entry = cast(dict[str, object], raw_entry)
            if entry["fixture_id"] == expected_fixture_id:
                outcomes = cast(dict[str, object], entry["outcomes"])
                return cast(dict[str, object], outcomes[outcome_name])
    raise AssertionError(f"missing fixture evidence: {expected_fixture_id}")


def _assert_fixture_repeats_pristine_failure_and_gold_pass_in_container(
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
        expected_evidence=_load_expected_outcome(fixture_name, "pristine_control"),
        image=image,
    )
    _assert_repeated_container_result(
        gold_results,
        expected_status=EvaluationStatus.PASSED,
        expected_evidence=_load_expected_outcome(fixture_name, "gold"),
        image=image,
    )


@pytest.mark.container
@pytest.mark.parametrize("fixture_name", _FIRST_BATCH)
def test_first_fixture_batch_repeats_pristine_failure_and_gold_pass_in_container(
    fixture_name: str,
) -> None:
    _assert_fixture_repeats_pristine_failure_and_gold_pass_in_container(fixture_name)


@pytest.mark.container
@pytest.mark.parametrize("fixture_name", _SECOND_BATCH)
def test_second_fixture_batch_repeats_pristine_failure_and_gold_pass_in_container(
    fixture_name: str,
) -> None:
    _assert_fixture_repeats_pristine_failure_and_gold_pass_in_container(fixture_name)
