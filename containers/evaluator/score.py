"""Trusted scorer for untrusted ``python-call-v1`` candidate responses."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

_CHALLENGE = Path("/inputs/challenge.json")
_ORACLE = Path("/inputs/grader/oracle.json")
_RESPONSE = Path("/inputs/response.txt")
_CANDIDATE_PREFIX = "GUILDMIND_CANDIDATE_RESPONSE="
_CANDIDATE_PREFIX_BYTES = _CANDIDATE_PREFIX.encode("ascii")
_RESULT_PREFIX = "GUILDMIND_EVALUATION_RESULT="
_COMPLETION_SCHEMA = "guildmind.evaluator-completion/v2"
_MAX_JSON_DEPTH = 32


class CandidateFailure(ValueError):
    """The untrusted response is malformed or functionally incorrect."""


def _strict_json(data: bytes, *, candidate_controlled: bool) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        exception = CandidateFailure if candidate_controlled else RuntimeError
        raise exception(f"invalid strict JSON: {error}") from error


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise RuntimeError(f"missing scorer environment: {name}")
    return value


def _extract_candidate_response(data: bytes) -> dict[str, Any]:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateFailure("candidate response is not UTF-8") from error
    lines = data.split(b"\n")
    indexes = [
        index for index, line in enumerate(lines) if line.startswith(_CANDIDATE_PREFIX_BYTES)
    ]
    if len(indexes) != 1:
        raise CandidateFailure("candidate emitted zero or multiple response records")
    index = indexes[0]
    if any(line.strip() for line in lines[index + 1 :]):
        raise CandidateFailure("candidate response record was not final")
    response = _strict_json(
        lines[index][len(_CANDIDATE_PREFIX_BYTES) :],
        candidate_controlled=True,
    )
    if not isinstance(response, dict):
        raise CandidateFailure("candidate response must be a JSON object")
    return response


def _validate_json_value(
    value: Any,
    *,
    candidate_controlled: bool,
    depth: int = 0,
) -> None:
    """Require exact recursive JSON types and a bounded nesting depth."""

    exception = CandidateFailure if candidate_controlled else RuntimeError
    if depth > _MAX_JSON_DEPTH:
        raise exception("protocol JSON exceeds the nesting limit")
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is float:
        if not math.isfinite(value):
            raise exception("protocol JSON numbers must be finite")
        return
    if value_type is list:
        for item in value:
            _validate_json_value(
                item,
                candidate_controlled=candidate_controlled,
                depth=depth + 1,
            )
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise exception("protocol JSON object keys must be exact strings")
            _validate_json_value(
                item,
                candidate_controlled=candidate_controlled,
                depth=depth + 1,
            )
        return
    raise exception(f"unsupported protocol JSON value: {value_type.__name__}")


def _binding(
    *,
    task_id: str,
    patch_sha256: str,
    challenge_sha256: str,
    response_sha256: str,
    oracle_sha256: str,
    image_digest: str,
    evaluator_version: str,
    expected_tests: int,
    limits_sha256: str,
    source_sha256: str,
    task_content_hash: str,
) -> str:
    return _sha256(
        _canonical(
            {
                "challenge_sha256": challenge_sha256,
                "evaluator_version": evaluator_version,
                "expected_tests": expected_tests,
                "image_digest": image_digest,
                "limits_sha256": limits_sha256,
                "oracle_sha256": oracle_sha256,
                "patch_sha256": patch_sha256,
                "protocol": "python-call-v1",
                "response_sha256": response_sha256,
                "source_sha256": source_sha256,
                "task_content_hash": task_content_hash,
                "task_id": task_id,
            }
        )
    )


def _base_payload(
    *,
    task_id: str,
    patch_sha256: str,
    challenge_sha256: str,
    response_sha256: str,
    oracle_sha256: str,
    image_digest: str,
    expected_tests: int,
    evaluator_version: str,
    limits_sha256: str,
    source_sha256: str,
    task_content_hash: str,
) -> dict[str, Any]:
    return {
        "challenge_sha256": challenge_sha256,
        "evaluation_binding_sha256": _binding(
            task_id=task_id,
            patch_sha256=patch_sha256,
            challenge_sha256=challenge_sha256,
            response_sha256=response_sha256,
            oracle_sha256=oracle_sha256,
            image_digest=image_digest,
            evaluator_version=evaluator_version,
            expected_tests=expected_tests,
            limits_sha256=limits_sha256,
            source_sha256=source_sha256,
            task_content_hash=task_content_hash,
        ),
        "evaluator_version": evaluator_version,
        "expected_tests": expected_tests,
        "image_digest": image_digest,
        "limits_sha256": limits_sha256,
        "oracle_sha256": oracle_sha256,
        "patch_sha256": patch_sha256,
        "protocol": "python-call-v1",
        "response_sha256": response_sha256,
        "schema_version": _COMPLETION_SCHEMA,
        "source_sha256": source_sha256,
        "task_content_hash": task_content_hash,
        "task_id": task_id,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(f"{_RESULT_PREFIX}{_canonical(payload).decode('utf-8')}", flush=True)


def _validate_protocol(
    challenge: Any,
    oracle: Any,
    response: dict[str, Any],
    *,
    challenge_sha256: str,
    expected_tests: int,
) -> tuple[int, int]:
    if not isinstance(challenge, dict) or set(challenge) != {
        "cases",
        "entrypoint",
        "protocol",
        "schema_version",
    }:
        raise RuntimeError("challenge fields are invalid")
    if challenge.get("schema_version") != "guildmind.python-call-challenge/v1":
        raise RuntimeError("challenge schema is invalid")
    if challenge.get("protocol") != "python-call-v1":
        raise RuntimeError("challenge protocol is invalid")
    if not isinstance(oracle, dict) or set(oracle) != {"cases", "schema_version"}:
        raise RuntimeError("oracle fields are invalid")
    if oracle.get("schema_version") != "guildmind.python-call-oracle/v1":
        raise RuntimeError("oracle schema is invalid")
    if set(response) != {"challenge_sha256", "results", "schema_version"}:
        raise CandidateFailure("candidate response fields are invalid")
    if response.get("schema_version") != "guildmind.python-call-response/v1":
        raise CandidateFailure("candidate response schema is invalid")
    if response.get("challenge_sha256") != challenge_sha256:
        raise CandidateFailure("candidate response challenge hash is invalid")

    challenge_cases = challenge.get("cases")
    oracle_cases = oracle.get("cases")
    results = response.get("results")
    if not isinstance(challenge_cases, list) or not isinstance(oracle_cases, list):
        raise RuntimeError("challenge or oracle cases are invalid")
    if len(challenge_cases) != expected_tests or len(oracle_cases) != expected_tests:
        raise RuntimeError("trusted case count does not match expected tests")
    if not isinstance(results, list) or len(results) != expected_tests:
        raise CandidateFailure("candidate result count does not match expected tests")

    failures = 0
    for challenge_case, oracle_case, result in zip(
        challenge_cases,
        oracle_cases,
        results,
        strict=True,
    ):
        if not isinstance(challenge_case, dict) or set(challenge_case) != {
            "args",
            "case_id",
            "kwargs",
        }:
            raise RuntimeError("challenge case is invalid")
        if not isinstance(oracle_case, dict) or set(oracle_case) != {
            "args",
            "case_id",
            "expected",
            "kwargs",
        }:
            raise RuntimeError("oracle case is invalid")
        if {
            "args": oracle_case.get("args"),
            "case_id": oracle_case.get("case_id"),
            "kwargs": oracle_case.get("kwargs"),
        } != challenge_case:
            raise RuntimeError("challenge does not match sealed oracle")
        _validate_json_value(
            challenge_case.get("args"),
            candidate_controlled=False,
        )
        _validate_json_value(
            challenge_case.get("kwargs"),
            candidate_controlled=False,
        )
        case_id = challenge_case.get("case_id")
        if not isinstance(result, dict) or result.get("case_id") != case_id:
            raise CandidateFailure("candidate result IDs are missing or reordered")
        kind = result.get("kind")
        if kind == "raised":
            if set(result) != {"case_id", "error_type", "kind"} or not isinstance(
                result.get("error_type"), str
            ):
                raise CandidateFailure("candidate raised-result fields are invalid")
            failures += 1
            continue
        if kind != "returned" or set(result) != {"case_id", "kind", "value"}:
            raise CandidateFailure("candidate returned-result fields are invalid")
        expected = oracle_case.get("expected")
        if not isinstance(expected, dict) or set(expected) != {"kind", "value"}:
            raise RuntimeError("oracle expected outcome is invalid")
        if expected.get("kind") != "returned":
            raise RuntimeError("oracle expected kind is unsupported")
        _validate_json_value(expected.get("value"), candidate_controlled=False)
        _validate_json_value(result.get("value"), candidate_controlled=True)
        try:
            matches = _canonical(result.get("value")) == _canonical(expected.get("value"))
        except (TypeError, ValueError, RecursionError, OverflowError) as error:
            raise CandidateFailure(f"candidate value is not protocol JSON: {error}") from error
        if not matches:
            failures += 1
    return len(results), failures


def main() -> int:
    task_id = _required_environment("GUILDMIND_TASK_ID")
    patch_sha256 = _required_environment("GUILDMIND_PATCH_SHA256")
    declared_challenge_sha256 = _required_environment("GUILDMIND_CHALLENGE_SHA256")
    declared_response_sha256 = _required_environment("GUILDMIND_RESPONSE_SHA256")
    declared_oracle_sha256 = _required_environment("GUILDMIND_ORACLE_SHA256")
    image_digest = _required_environment("GUILDMIND_IMAGE_DIGEST")
    evaluator_version = _required_environment("GUILDMIND_EVALUATOR_VERSION")
    limits_sha256 = _required_environment("GUILDMIND_LIMITS_SHA256")
    source_sha256 = _required_environment("GUILDMIND_SOURCE_SHA256")
    task_content_hash = _required_environment("GUILDMIND_TASK_CONTENT_HASH")
    expected_tests = int(_required_environment("GUILDMIND_EXPECTED_TESTS"))
    if expected_tests <= 0:
        raise RuntimeError("expected test count must be positive")

    challenge_bytes = _CHALLENGE.read_bytes()
    oracle_bytes = _ORACLE.read_bytes()
    response_bytes = _RESPONSE.read_bytes()
    challenge_sha256 = _sha256(challenge_bytes)
    oracle_sha256 = _sha256(oracle_bytes)
    response_sha256 = _sha256(response_bytes)
    if challenge_sha256 != declared_challenge_sha256:
        raise RuntimeError("challenge hash does not match control-plane binding")
    if oracle_sha256 != declared_oracle_sha256:
        raise RuntimeError("oracle hash does not match control-plane binding")
    if response_sha256 != declared_response_sha256:
        raise RuntimeError("response hash does not match control-plane binding")

    base = _base_payload(
        task_id=task_id,
        patch_sha256=patch_sha256,
        challenge_sha256=challenge_sha256,
        response_sha256=response_sha256,
        oracle_sha256=oracle_sha256,
        image_digest=image_digest,
        expected_tests=expected_tests,
        evaluator_version=evaluator_version,
        limits_sha256=limits_sha256,
        source_sha256=source_sha256,
        task_content_hash=task_content_hash,
    )
    try:
        challenge = _strict_json(challenge_bytes, candidate_controlled=False)
        oracle = _strict_json(oracle_bytes, candidate_controlled=False)
        response = _extract_candidate_response(response_bytes)
        tests_run, failures = _validate_protocol(
            challenge,
            oracle,
            response,
            challenge_sha256=challenge_sha256,
            expected_tests=expected_tests,
        )
    except CandidateFailure as error:
        _emit(
            {
                **base,
                "classification": "candidate_failed",
                "errors": 0,
                "failures": expected_tests,
                "message": str(error),
                "skipped": 0,
                "successful": False,
                "tests_run": 0,
            }
        )
        return 1

    successful = failures == 0
    _emit(
        {
            **base,
            "classification": "passed" if successful else "candidate_failed",
            "errors": 0,
            "failures": failures,
            "skipped": 0,
            "successful": successful,
            "tests_run": tests_run,
        }
    )
    return 0 if successful else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:
        task_id = os.environ.get("GUILDMIND_TASK_ID", "unknown")
        patch_sha256 = os.environ.get("GUILDMIND_PATCH_SHA256", "unknown")
        challenge_sha256 = os.environ.get("GUILDMIND_CHALLENGE_SHA256", "unknown")
        response_sha256 = os.environ.get("GUILDMIND_RESPONSE_SHA256", "unknown")
        oracle_sha256 = os.environ.get("GUILDMIND_ORACLE_SHA256", "unknown")
        image_digest = os.environ.get("GUILDMIND_IMAGE_DIGEST", "unknown")
        evaluator_version = os.environ.get("GUILDMIND_EVALUATOR_VERSION", "unknown")
        limits_sha256 = os.environ.get("GUILDMIND_LIMITS_SHA256", "unknown")
        source_sha256 = os.environ.get("GUILDMIND_SOURCE_SHA256", "unknown")
        task_content_hash = os.environ.get("GUILDMIND_TASK_CONTENT_HASH", "unknown")
        expected_raw = os.environ.get("GUILDMIND_EXPECTED_TESTS", "0")
        expected_tests = int(expected_raw) if expected_raw.isdecimal() else 0
        try:
            base = _base_payload(
                task_id=task_id,
                patch_sha256=patch_sha256,
                challenge_sha256=challenge_sha256,
                response_sha256=response_sha256,
                oracle_sha256=oracle_sha256,
                image_digest=image_digest,
                expected_tests=expected_tests,
                evaluator_version=evaluator_version,
                limits_sha256=limits_sha256,
                source_sha256=source_sha256,
                task_content_hash=task_content_hash,
            )
            _emit(
                {
                    **base,
                    "classification": "evaluator_error",
                    "error": f"{type(error).__name__}: {error}",
                    "errors": 1,
                    "failures": 0,
                    "skipped": 0,
                    "successful": False,
                    "tests_run": 0,
                }
            )
        finally:
            exit_code = 2
    raise SystemExit(exit_code)
