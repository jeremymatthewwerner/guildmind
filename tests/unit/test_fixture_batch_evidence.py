from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Never, cast

import pytest
from pydantic import JsonValue

from guildmind.domain import canonical_sha256, sha256_bytes
from guildmind.evaluation import ContainerEvaluatorResources, load_fixture, load_python_call_bundle

_REPOSITORY_ROOT = Path(__file__).parents[2]
_REPORT_ROOT = _REPOSITORY_ROOT / "docs" / "evidence" / "fixture-qualification"
_REPORTS = (
    (
        "2026-08-03-batch-001-development-container/report.json",
        "stage1-fixture-batch-001",
        "c11d38b8372937191153ce4a87805f27b281f1d0",
        "31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7",
        (
            "002-slug-normalization",
            "003-interval-merge",
            "004-json-pointer",
            "005-stable-dedupe",
        ),
    ),
    (
        "2026-08-03-batch-002-development-container/report.json",
        "stage1-fixture-batch-002",
        "13d3b5ebf0dba0b585999e135bac15b5f0032d5d",
        "31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7",
        (
            "006-run-decoder",
            "007-apportionment",
            "008-topological-order",
            "009-ordered-changes",
        ),
    ),
    (
        "2026-08-03-batch-003-development-container/report.json",
        "stage1-fixture-batch-003",
        "a39d03f8f8ca6b64fa53192c4828e45f8a4ab83c",
        "31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7",
        (
            "010-word-wrap",
            "011-business-days",
            "012-roman-parser",
            "013-grid-rotation",
        ),
    ),
    (
        "2026-08-03-batch-004-development-container/report.json",
        "stage1-fixture-batch-004",
        "7c9eebaf293ea088db07fdaa9daf8441c21a0b00",
        "5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e",
        (
            "014-transaction-summary",
            "015-route-matcher",
            "016-backoff-schedule",
            "017-inventory-delta",
        ),
    ),
    (
        "2026-08-03-batch-005-development-container/report.json",
        "stage1-fixture-batch-005",
        "6492d5580fae5ab11de8cd3231cf1f91f99f4395",
        "5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e",
        (
            "018-latest-versions",
            "019-recursive-redaction",
            "020-rule-evaluation",
        ),
    ),
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _reject_constant(value: str) -> Never:
    raise ValueError(f"non-finite JSON constant: {value}")


def _object_from_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_report(path: Path) -> dict[str, JsonValue]:
    raw = json.loads(
        path.read_bytes(),
        object_pairs_hook=_object_from_pairs,
        parse_constant=_reject_constant,
    )
    assert isinstance(raw, dict)
    return cast(dict[str, JsonValue], raw)


def _object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _array(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def _string(value: JsonValue) -> str:
    assert isinstance(value, str)
    return value


def _integer(value: JsonValue) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _assert_sha256(value: JsonValue) -> str:
    digest = _string(value)
    assert _SHA256.fullmatch(digest) is not None
    return digest


@pytest.mark.parametrize(
    ("report_relative", "batch_id", "revision", "image_digest", "fixtures"), _REPORTS
)
def test_fixture_batch_container_report_is_self_bound_and_matches_sources(
    report_relative: str,
    batch_id: str,
    revision: str,
    image_digest: str,
    fixtures: tuple[str, ...],
) -> None:
    report = _load_report(_REPORT_ROOT / report_relative)
    assert set(report) == {
        "batch_id",
        "evaluator_version",
        "evidence_level",
        "expected_outcomes",
        "fixture_count",
        "fixtures",
        "image_reference",
        "recorded_on",
        "repetitions_per_outcome",
        "report_body_sha256",
        "repository_revision",
        "repository_tracked_clean",
        "schema_version",
        "total_evaluations",
    }
    claimed_body_sha256 = _assert_sha256(report["report_body_sha256"])
    body = dict(report)
    del body["report_body_sha256"]
    assert canonical_sha256(body) == claimed_body_sha256
    assert report["schema_version"] == "guildmind.fixture-batch-container-qualification/v1"
    assert report["batch_id"] == batch_id
    assert report["evidence_level"] == "development-container"
    assert report["evaluator_version"] == "guildmind/container-python-call-v2"
    assert report["image_reference"] == f"guildmind/evaluator@sha256:{image_digest}"
    assert report["recorded_on"] == "2026-08-03"
    assert report["repository_revision"] == revision
    assert report["repository_tracked_clean"] is True
    assert report["fixture_count"] == len(fixtures)
    assert report["repetitions_per_outcome"] == 3
    assert report["total_evaluations"] == len(fixtures) * 2 * 3
    assert report["expected_outcomes"] == {
        "gold": "passed",
        "pristine_control": "tests_failed",
    }

    fixture_entries = _array(report["fixtures"])
    assert len(fixture_entries) == len(fixtures)
    resources = ContainerEvaluatorResources()
    for fixture_name, raw_entry in zip(fixtures, fixture_entries, strict=True):
        entry = _object(raw_entry)
        fixture_root = _REPOSITORY_ROOT / "fixtures" / fixture_name
        spec = load_fixture(fixture_root)
        assert spec.fixture_manifest_bytes is not None
        assert spec.python_call_protocol is not None
        assert spec.pristine_workspace_sha256 is not None
        bundle = load_python_call_bundle(
            spec.python_call_protocol,
            expected_case_count=spec.expected_test_count,
        )

        assert entry["fixture_id"] == spec.task_id
        assert entry["fixture_manifest_sha256"] == sha256_bytes(spec.fixture_manifest_bytes)
        assert entry["workspace_sha256"] == spec.pristine_workspace_sha256
        assert entry["source_sha256"] == spec.pristine_workspace_sha256
        assert entry["challenge_sha256"] == bundle.challenge_sha256
        assert entry["oracle_sha256"] == bundle.oracle_sha256
        assert entry["expected_cases"] == bundle.case_count == 6
        assert entry["image_id"] == f"sha256:{image_digest}"
        assert entry["task_content_hash"] == canonical_sha256(
            {
                "challenge_sha256": bundle.challenge_sha256,
                "oracle_sha256": bundle.oracle_sha256,
                "protocol": "python-call-v1",
                "source_sha256": spec.pristine_workspace_sha256,
                "task_id": spec.task_id,
            }
        )
        assert entry["limits_sha256"] == canonical_sha256(
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

        outcomes = _object(entry["outcomes"])
        assert set(outcomes) == {"gold", "pristine_control"}
        for outcome_name, patch_relative, status in (
            ("gold", "solution.patch", "passed"),
            ("pristine_control", "controls/pristine.patch", "tests_failed"),
        ):
            outcome = _object(outcomes[outcome_name])
            assert set(outcome) == {
                "completion",
                "evaluation_binding_sha256",
                "observed_status",
                "patch_path",
                "patch_sha256",
                "repetitions",
                "response_sha256",
                "stable_result_sha256",
                "trusted_completion_record_sha256",
            }
            assert outcome["patch_path"] == patch_relative
            assert outcome["patch_sha256"] == sha256_bytes(
                (fixture_root / patch_relative).read_bytes()
            )
            assert outcome["observed_status"] == status
            assert outcome["repetitions"] == 3
            _assert_sha256(outcome["evaluation_binding_sha256"])
            _assert_sha256(outcome["response_sha256"])
            _assert_sha256(outcome["stable_result_sha256"])
            _assert_sha256(outcome["trusted_completion_record_sha256"])

            completion = _object(outcome["completion"])
            assert set(completion) == {
                "classification",
                "errors",
                "expected_tests",
                "failures",
                "skipped",
                "successful",
                "tests_run",
            }
            assert completion["errors"] == 0
            assert completion["expected_tests"] == 6
            assert completion["skipped"] == 0
            assert completion["tests_run"] == 6
            if outcome_name == "gold":
                assert completion["classification"] == "passed"
                assert completion["successful"] is True
                assert completion["failures"] == 0
            else:
                assert completion["classification"] == "candidate_failed"
                assert completion["successful"] is False
                assert _integer(completion["failures"]) > 0
