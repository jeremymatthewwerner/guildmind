"""Strict, canonical data model for the isolated Python-call evaluator protocol."""

from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from guildmind.domain import canonical_json, sha256_bytes
from guildmind.evaluation.local import FixtureConfigurationError, PythonCallProtocol

_ORACLE_SCHEMA = "guildmind.python-call-oracle/v1"
_CHALLENGE_SCHEMA = "guildmind.python-call-challenge/v1"
_PROTOCOL = "python-call-v1"
_CASE_ID = re.compile(r"^case-[0-9]{4}$")
_KEYWORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ORACLE_BYTES = 1_048_576
_MAX_CASES = 10_000
_MAX_ARGUMENTS = 64
_MAX_JSON_DEPTH = 32


@dataclass(frozen=True, slots=True)
class PythonCallBundle:
    """Canonical challenge/oracle bytes and their stable identities."""

    challenge: dict[str, JsonValue]
    challenge_bytes: bytes
    challenge_sha256: str
    oracle: dict[str, JsonValue]
    oracle_bytes: bytes
    oracle_sha256: str
    case_count: int


def load_python_call_bundle(
    protocol: PythonCallProtocol,
    *,
    expected_case_count: int,
) -> PythonCallBundle:
    """Load and validate a sealed oracle, then derive its candidate-visible challenge."""

    raw_bytes = protocol.sealed_cases_bytes
    if raw_bytes is None:
        try:
            mode = protocol.cases_file.lstat().st_mode
            raw_bytes = protocol.cases_file.read_bytes()
        except OSError as error:
            raise FixtureConfigurationError(
                f"cannot read evaluation cases: {protocol.cases_file}"
            ) from error
        if protocol.cases_file.is_symlink() or not stat.S_ISREG(mode):
            raise FixtureConfigurationError("evaluation cases must be a regular non-symlink file")
    if len(raw_bytes) > _MAX_ORACLE_BYTES:
        raise FixtureConfigurationError("evaluation cases exceed the protocol byte limit")

    raw = _strict_json_object(raw_bytes, label="evaluation cases")
    if set(raw) != {"schema_version", "cases"}:
        raise FixtureConfigurationError(
            "evaluation cases must contain exactly schema_version and cases"
        )
    if raw.get("schema_version") != _ORACLE_SCHEMA:
        raise FixtureConfigurationError("evaluation cases use an unsupported schema")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > _MAX_CASES:
        raise FixtureConfigurationError("evaluation cases must be a non-empty bounded array")
    if len(raw_cases) != expected_case_count:
        raise FixtureConfigurationError("evaluation case count does not match expected_test_count")

    oracle_cases: list[JsonValue] = []
    challenge_cases: list[JsonValue] = []
    seen_case_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise FixtureConfigurationError("each evaluation case must be an object")
        if set(raw_case) != {"case_id", "args", "kwargs", "expected"}:
            raise FixtureConfigurationError(
                "each evaluation case must contain case_id, args, kwargs, and expected"
            )
        case_id = raw_case.get("case_id")
        expected_id = f"case-{index:04d}"
        if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
            raise FixtureConfigurationError("evaluation case IDs must use case-NNNN")
        if case_id != expected_id:
            raise FixtureConfigurationError("evaluation case IDs must be contiguous and ordered")
        if case_id in seen_case_ids:
            raise FixtureConfigurationError("evaluation case IDs must be unique")
        seen_case_ids.add(case_id)

        args = raw_case.get("args")
        kwargs = raw_case.get("kwargs")
        expected = raw_case.get("expected")
        if not isinstance(args, list) or len(args) > _MAX_ARGUMENTS:
            raise FixtureConfigurationError("evaluation args must be a bounded array")
        if not isinstance(kwargs, dict) or len(kwargs) > _MAX_ARGUMENTS:
            raise FixtureConfigurationError("evaluation kwargs must be a bounded object")
        if any(not isinstance(key, str) or _KEYWORD.fullmatch(key) is None for key in kwargs):
            raise FixtureConfigurationError("evaluation keyword names must be Python identifiers")
        if not isinstance(expected, dict) or set(expected) != {"kind", "value"}:
            raise FixtureConfigurationError("expected outcome must contain kind and value")
        if expected.get("kind") != "returned":
            raise FixtureConfigurationError("python-call-v1 supports returned values only")
        _validate_json_value(args)
        _validate_json_value(kwargs)
        _validate_json_value(expected.get("value"))

        oracle_case: dict[str, JsonValue] = {
            "args": cast(JsonValue, args),
            "case_id": case_id,
            "expected": cast(JsonValue, expected),
            "kwargs": cast(JsonValue, kwargs),
        }
        challenge_case: dict[str, JsonValue] = {
            "args": cast(JsonValue, args),
            "case_id": case_id,
            "kwargs": cast(JsonValue, kwargs),
        }
        oracle_cases.append(cast(JsonValue, oracle_case))
        challenge_cases.append(cast(JsonValue, challenge_case))

    oracle: dict[str, JsonValue] = {
        "cases": oracle_cases,
        "schema_version": _ORACLE_SCHEMA,
    }
    challenge: dict[str, JsonValue] = {
        "cases": challenge_cases,
        "entrypoint": {
            "callable": protocol.callable_name,
            "module": protocol.module,
        },
        "protocol": _PROTOCOL,
        "schema_version": _CHALLENGE_SCHEMA,
    }
    oracle_bytes = canonical_json(oracle).encode("utf-8")
    challenge_bytes = canonical_json(challenge).encode("utf-8")
    return PythonCallBundle(
        challenge=challenge,
        challenge_bytes=challenge_bytes,
        challenge_sha256=sha256_bytes(challenge_bytes),
        oracle=oracle,
        oracle_bytes=oracle_bytes,
        oracle_sha256=sha256_bytes(oracle_bytes),
        case_count=len(oracle_cases),
    )


def _strict_json_object(data: bytes, *, label: str) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            data,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise FixtureConfigurationError(f"{label} must be strict UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise FixtureConfigurationError(f"{label} must contain a JSON object")
    return cast(dict[str, object], value)


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise FixtureConfigurationError("evaluation JSON exceeds the nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _validate_json_string(value)
        return
    if isinstance(value, float):
        raise FixtureConfigurationError("python-call-v1 does not support floating-point values")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise FixtureConfigurationError("evaluation JSON object keys must be strings")
            _validate_json_string(key)
            _validate_json_value(item, depth=depth + 1)
        return
    raise FixtureConfigurationError(
        f"evaluation JSON contains unsupported value: {type(value).__name__}"
    )


def _validate_json_string(value: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise FixtureConfigurationError("evaluation JSON strings must not contain surrogates")
