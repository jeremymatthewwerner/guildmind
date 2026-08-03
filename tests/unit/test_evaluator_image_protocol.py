from __future__ import annotations

import json
import math
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

_REPOSITORY_ROOT = Path(__file__).parents[2]
_IMAGE_ROOT = _REPOSITORY_ROOT / "containers" / "evaluator"


def _load_script(name: str) -> dict[str, Any]:
    return runpy.run_path(str(_IMAGE_ROOT / name))


@pytest.mark.parametrize(
    "value",
    [
        (1, 2),
        {1: "coerced-by-json-dumps"},
        {"nested": [{2: "also-coerced"}]},
    ],
)
def test_invoke_rejects_non_exact_json_return_types(value: object) -> None:
    validate = cast(Callable[..., None], _load_script("invoke.py")["_validate_json_value"])

    with pytest.raises(TypeError, match="candidate return"):
        validate(value)


def test_invoke_accepts_only_the_recursive_protocol_json_subset() -> None:
    validate = cast(Callable[..., None], _load_script("invoke.py")["_validate_json_value"])

    validate(
        {
            "boolean": True,
            "float": 1.5,
            "integer": 7,
            "list": [None, "text", {"nested": -2}],
        }
    )


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_invoke_rejects_nonfinite_float_returns(value: float) -> None:
    validate = cast(Callable[..., None], _load_script("invoke.py")["_validate_json_value"])

    with pytest.raises(TypeError, match="finite"):
        validate(value)


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_scorer_frames_records_only_on_ascii_lf(separator: str) -> None:
    namespace = _load_script("score.py")
    extract = cast(
        Callable[[bytes], dict[str, Any]],
        namespace["_extract_candidate_response"],
    )
    prefix = cast(str, namespace["_CANDIDATE_PREFIX"]).encode("ascii")
    payload = {
        "challenge_sha256": "a" * 64,
        "results": [
            {
                "case_id": "case-0001",
                "kind": "returned",
                "value": f"before{separator}after",
            }
        ],
        "schema_version": "guildmind.python-call-response/v1",
    }
    literal_unicode_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    parsed = extract(prefix + literal_unicode_json + b"\n")

    assert parsed == payload


def test_image_protocol_canonical_output_is_ascii_only() -> None:
    invoke_canonical = cast(Callable[[Any], str], _load_script("invoke.py")["_canonical"])
    score_canonical = cast(Callable[[Any], bytes], _load_script("score.py")["_canonical"])
    value = {"separators": "\u0085\u2028\u2029", "snowman": "\u2603"}

    invoke_output = invoke_canonical(value)
    score_output = score_canonical(value)

    assert invoke_output.isascii()
    assert score_output.isascii()
    assert "\\u2028" in invoke_output
    assert b"\\u2029" in score_output


def test_scorer_classifies_unsupported_candidate_json_as_candidate_failure() -> None:
    namespace = _load_script("score.py")
    validate = cast(Callable[..., None], namespace["_validate_json_value"])
    candidate_failure = cast(type[Exception], namespace["CandidateFailure"])

    with pytest.raises(candidate_failure, match="unsupported protocol JSON value"):
        validate((1, 2), candidate_controlled=True)


def test_scorer_accepts_finite_floats_and_rejects_nonfinite_values() -> None:
    namespace = _load_script("score.py")
    validate = cast(Callable[..., None], namespace["_validate_json_value"])
    candidate_failure = cast(type[Exception], namespace["CandidateFailure"])

    validate([1.5, {"nested": -2.25}], candidate_controlled=True)
    with pytest.raises(candidate_failure, match="finite"):
        validate(math.inf, candidate_controlled=True)
    with pytest.raises(RuntimeError, match="finite"):
        validate(math.nan, candidate_controlled=False)


def test_scorer_compares_exact_finite_float_results() -> None:
    validate_protocol = cast(
        Callable[..., tuple[int, int]], _load_script("score.py")["_validate_protocol"]
    )
    challenge_sha256 = "a" * 64
    challenge = {
        "cases": [
            {
                "args": [2, 1.5],
                "case_id": "case-0001",
                "kwargs": {},
            }
        ],
        "entrypoint": {"callable": "schedule", "module": "backoff"},
        "protocol": "python-call-v1",
        "schema_version": "guildmind.python-call-challenge/v1",
    }
    oracle = {
        "cases": [
            {
                "args": [2, 1.5],
                "case_id": "case-0001",
                "expected": {"kind": "returned", "value": [2, 3.0]},
                "kwargs": {},
            }
        ],
        "schema_version": "guildmind.python-call-oracle/v1",
    }
    response = {
        "challenge_sha256": challenge_sha256,
        "results": [{"case_id": "case-0001", "kind": "returned", "value": [2, 3.0]}],
        "schema_version": "guildmind.python-call-response/v1",
    }

    assert validate_protocol(
        challenge,
        oracle,
        response,
        challenge_sha256=challenge_sha256,
        expected_tests=1,
    ) == (1, 0)


def test_scorer_binding_commits_the_expected_case_count() -> None:
    binding = cast(Callable[..., str], _load_script("score.py")["_binding"])
    values = {
        "challenge_sha256": "a" * 64,
        "evaluator_version": "guildmind/container-python-call-v2",
        "image_digest": "sha256:" + "b" * 64,
        "limits_sha256": "c" * 64,
        "oracle_sha256": "d" * 64,
        "patch_sha256": "e" * 64,
        "response_sha256": "f" * 64,
        "source_sha256": "1" * 64,
        "task_content_hash": "2" * 64,
        "task_id": "fixture-001",
    }

    one_case = binding(**values, expected_tests=1)
    two_cases = binding(**values, expected_tests=2)

    assert one_case != two_cases
