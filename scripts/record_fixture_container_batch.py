"""Record a self-bound development-container fixture qualification report."""

from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from guildmind.domain import canonical_json, canonical_sha256, sha256_bytes
from guildmind.evaluation import (
    ContainerEvaluator,
    ContainerEvaluatorResources,
    EvaluationStatus,
    LocalEvaluationResult,
    load_fixture,
    load_python_call_bundle,
    require_tracked_clean_revision,
    write_new_report,
)
from guildmind.sandbox import DockerHostPolicy, DockerSandbox

_BATCH_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FIXTURE_NAME = re.compile(r"^[0-9]{3}-[a-z0-9]+(?:-[a-z0-9]+)*$")
_IMAGE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
_RECORDED_ON = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_EVALUATOR_VERSION = "guildmind/container-python-call-v2"
_SCHEMA_VERSION = "guildmind.fixture-batch-container-qualification/v1"


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recorded-on", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("fixtures", nargs="+")
    arguments = parser.parse_args()
    if _BATCH_ID.fullmatch(arguments.batch_id) is None:
        parser.error("--batch-id must be a lowercase hyphenated identifier")
    if _IMAGE.fullmatch(arguments.image) is None:
        parser.error("--image must be a digest-pinned image reference")
    if _RECORDED_ON.fullmatch(arguments.recorded_on) is None:
        parser.error("--recorded-on must use YYYY-MM-DD")
    try:
        date.fromisoformat(arguments.recorded_on)
    except ValueError:
        parser.error("--recorded-on must be a real calendar date")
    if arguments.repetitions <= 0:
        parser.error("--repetitions must be positive")
    if len(arguments.fixtures) != len(set(arguments.fixtures)):
        parser.error("fixture names must be unique")
    if any(_FIXTURE_NAME.fullmatch(name) is None for name in arguments.fixtures):
        parser.error("fixture names must be plain NNN-lowercase identifiers")
    if arguments.fixtures != sorted(arguments.fixtures):
        parser.error("fixture names must be ordered")
    return arguments


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


def _object(value: JsonValue, *, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _string(value: JsonValue, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be a string")
    return value


def _integer(value: JsonValue, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"{label} must be an integer")
    return value


def _record_outcome(
    evaluator: ContainerEvaluator,
    *,
    fixture_root: Path,
    patch_relative: str,
    repetitions: int,
    expected_status: EvaluationStatus,
) -> tuple[dict[str, JsonValue], str]:
    spec = load_fixture(fixture_root)
    patch_path = fixture_root / patch_relative
    patch_sha256 = sha256_bytes(patch_path.read_bytes())
    results = tuple(
        evaluator.evaluate(
            spec,
            patch_path,
            expected_patch_sha256=patch_sha256,
        )
        for _ in range(repetitions)
    )
    if any(result != results[0] for result in results[1:]):
        raise RuntimeError(f"unstable container result: {spec.task_id} {patch_relative}")

    for result in results:
        if result.status is not expected_status:
            raise RuntimeError(
                f"unexpected result for {spec.task_id} {patch_relative}: {result.status.value}"
            )
        if result.output_truncated:
            raise RuntimeError(f"truncated result: {spec.task_id} {patch_relative}")
        if result.raw_candidate_stdout is None or result.raw_scorer_stdout is None:
            raise RuntimeError(f"missing transcript: {spec.task_id} {patch_relative}")
        execution = result.execution
        if execution.get("evaluator_version") != _EVALUATOR_VERSION:
            raise RuntimeError(f"wrong evaluator version: {spec.task_id} {patch_relative}")
        if execution.get("image_reference") != evaluator.image:
            raise RuntimeError(f"wrong image reference: {spec.task_id} {patch_relative}")
        if execution.get("expected_tests") != spec.expected_test_count:
            raise RuntimeError(f"wrong test count: {spec.task_id} {patch_relative}")
        candidate = _object(execution.get("candidate"), label="candidate execution")
        scorer = _object(execution.get("scorer"), label="scorer execution")
        completion = _object(execution.get("completion"), label="completion")
        if candidate.get("sandbox_status") != "exited":
            raise RuntimeError(f"candidate did not exit: {spec.task_id} {patch_relative}")
        if scorer.get("sandbox_status") != "exited":
            raise RuntimeError(f"scorer did not exit: {spec.task_id} {patch_relative}")
        if candidate.get("output_truncated") is not False:
            raise RuntimeError(f"candidate output truncated: {spec.task_id} {patch_relative}")
        if scorer.get("output_truncated") is not False:
            raise RuntimeError(f"scorer output truncated: {spec.task_id} {patch_relative}")
        if candidate.get("image_id") != scorer.get("image_id"):
            raise RuntimeError(f"phase image mismatch: {spec.task_id} {patch_relative}")
        if completion.get("tests_run") != spec.expected_test_count:
            raise RuntimeError(f"scorer omitted cases: {spec.task_id} {patch_relative}")
        if completion.get("expected_tests") != spec.expected_test_count:
            raise RuntimeError(f"completion count mismatch: {spec.task_id} {patch_relative}")
        if completion.get("errors") != 0 or completion.get("skipped") != 0:
            raise RuntimeError(f"incomplete scoring: {spec.task_id} {patch_relative}")
        failures = _integer(completion.get("failures"), label="completion failures")
        if expected_status is EvaluationStatus.PASSED:
            if (
                result.exit_code != 0
                or completion.get("classification") != "passed"
                or completion.get("successful") is not True
                or failures != 0
            ):
                raise RuntimeError(f"invalid gold completion: {spec.task_id}")
        elif (
            result.exit_code != 1
            or completion.get("classification") != "candidate_failed"
            or completion.get("successful") is not False
            or failures <= 0
        ):
            raise RuntimeError(f"invalid negative completion: {spec.task_id}")

    first = results[0]
    execution = first.execution
    completion = _object(execution["completion"], label="completion")
    candidate = _object(execution["candidate"], label="candidate execution")
    outcome: dict[str, JsonValue] = {
        "completion": cast(JsonValue, completion),
        "evaluation_binding_sha256": _string(
            execution["evaluation_binding_sha256"],
            label="evaluation binding",
        ),
        "observed_status": first.status.value,
        "patch_path": patch_relative,
        "patch_sha256": patch_sha256,
        "repetitions": repetitions,
        "response_sha256": _string(execution["response_sha256"], label="response digest"),
        "stable_result_sha256": canonical_sha256(_result_payload(first)),
        "trusted_completion_record_sha256": _string(
            execution["trusted_completion_record_sha256"],
            label="completion record digest",
        ),
    }
    image_id = _string(candidate["image_id"], label="candidate image ID")
    return outcome, image_id


def _record_fixture(
    evaluator: ContainerEvaluator,
    resources: ContainerEvaluatorResources,
    *,
    fixture_root: Path,
    repetitions: int,
) -> dict[str, JsonValue]:
    spec = load_fixture(fixture_root)
    if (
        spec.fixture_manifest_bytes is None
        or spec.pristine_workspace_sha256 is None
        or spec.python_call_protocol is None
    ):
        raise RuntimeError(f"fixture is not fully frozen: {spec.task_id}")
    bundle = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    pristine, pristine_image_id = _record_outcome(
        evaluator,
        fixture_root=fixture_root,
        patch_relative="controls/pristine.patch",
        repetitions=repetitions,
        expected_status=EvaluationStatus.TESTS_FAILED,
    )
    gold, gold_image_id = _record_outcome(
        evaluator,
        fixture_root=fixture_root,
        patch_relative="solution.patch",
        repetitions=repetitions,
        expected_status=EvaluationStatus.PASSED,
    )
    if pristine_image_id != gold_image_id:
        raise RuntimeError(f"outcome image mismatch: {spec.task_id}")
    task_content_hash = canonical_sha256(
        {
            "challenge_sha256": bundle.challenge_sha256,
            "oracle_sha256": bundle.oracle_sha256,
            "protocol": "python-call-v1",
            "source_sha256": spec.pristine_workspace_sha256,
            "task_id": spec.task_id,
        }
    )
    limits_sha256 = canonical_sha256(
        {
            "cpu_cores": resources.cpu_cores,
            "memory_bytes": resources.memory_bytes,
            "output_bytes": spec.max_output_bytes + 8_192,
            "pids": resources.pids,
            "temporary_bytes": resources.temporary_bytes,
            "wall_time_seconds": spec.timeout_seconds,
            "workspace_bytes": resources.workspace_bytes,
        }
    )
    return {
        "challenge_sha256": bundle.challenge_sha256,
        "expected_cases": bundle.case_count,
        "fixture_id": spec.task_id,
        "fixture_manifest_sha256": sha256_bytes(spec.fixture_manifest_bytes),
        "image_id": pristine_image_id,
        "limits_sha256": limits_sha256,
        "oracle_sha256": bundle.oracle_sha256,
        "outcomes": {"gold": gold, "pristine_control": pristine},
        "source_sha256": spec.pristine_workspace_sha256,
        "task_content_hash": task_content_hash,
        "workspace_sha256": spec.pristine_workspace_sha256,
    }


def main() -> int:
    arguments = _parse_arguments()
    repository = Path(__file__).resolve().parents[1]
    revision = require_tracked_clean_revision(repository)
    resources = ContainerEvaluatorResources()
    evaluator = ContainerEvaluator(
        sandbox=DockerSandbox(host_policy=DockerHostPolicy.development_only()),
        image=cast(str, arguments.image),
        resources=resources,
    )
    fixture_names = cast(list[str], arguments.fixtures)
    repetitions = cast(int, arguments.repetitions)
    fixtures: list[JsonValue] = [
        cast(
            JsonValue,
            _record_fixture(
                evaluator,
                resources,
                fixture_root=repository / "fixtures" / fixture_name,
                repetitions=repetitions,
            ),
        )
        for fixture_name in fixture_names
    ]
    body: dict[str, JsonValue] = {
        "batch_id": cast(str, arguments.batch_id),
        "evaluator_version": _EVALUATOR_VERSION,
        "evidence_level": "development-container",
        "expected_outcomes": {
            "gold": "passed",
            "pristine_control": "tests_failed",
        },
        "fixture_count": len(fixtures),
        "fixtures": fixtures,
        "image_reference": cast(str, arguments.image),
        "recorded_on": cast(str, arguments.recorded_on),
        "repetitions_per_outcome": repetitions,
        "repository_revision": revision,
        "repository_tracked_clean": True,
        "schema_version": _SCHEMA_VERSION,
        "total_evaluations": len(fixtures) * 2 * repetitions,
    }
    report = dict(body)
    report["report_body_sha256"] = canonical_sha256(body)
    output = cast(Path, arguments.output)
    write_new_report(output, report)
    print(
        canonical_json(
            {
                "output": str(output),
                "report_body_sha256": report["report_body_sha256"],
                "repository_revision": revision,
                "total_evaluations": report["total_evaluations"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
