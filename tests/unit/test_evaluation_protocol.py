from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from guildmind.evaluation import (
    FixtureConfigurationError,
    PythonCallProtocol,
    load_fixture,
    load_python_call_bundle,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


def test_python_call_bundle_strips_expected_values_from_candidate_challenge() -> None:
    spec = load_fixture(_FIXTURE)
    assert spec.python_call_protocol is not None

    bundle = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    replay = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )

    assert bundle == replay
    assert bundle.case_count == 5
    assert b'"expected"' not in bundle.challenge_bytes
    assert b'"expected"' in bundle.oracle_bytes
    assert len(bundle.challenge_sha256) == 64
    assert len(bundle.oracle_sha256) == 64
    assert bundle.challenge_sha256 != bundle.oracle_sha256


@pytest.mark.parametrize(
    "oracle",
    [
        b'{"schema_version":"guildmind.python-call-oracle/v1","cases":[],"cases":[]}',
        (
            b'{"schema_version":"guildmind.python-call-oracle/v1","cases":['
            b'{"case_id":"case-0002","args":[1],"kwargs":{},'
            b'"expected":{"kind":"returned","value":1}}]}'
        ),
        (
            b'{"schema_version":"guildmind.python-call-oracle/v1","cases":['
            b'{"case_id":"case-0001","args":[1e999],"kwargs":{},'
            b'"expected":{"kind":"returned","value":1}}]}'
        ),
        b'{"schema_version":"guildmind.python-call-oracle/v1","cases":[NaN]}',
    ],
)
def test_python_call_bundle_rejects_ambiguous_or_unsupported_json(
    tmp_path: Path,
    oracle: bytes,
) -> None:
    cases = tmp_path / "cases.json"
    cases.write_bytes(oracle)
    protocol = PythonCallProtocol(module="addition", callable_name="add", cases_file=cases)

    with pytest.raises(FixtureConfigurationError):
        load_python_call_bundle(protocol, expected_case_count=1)


def test_python_call_bundle_accepts_and_canonicalizes_finite_numbers(tmp_path: Path) -> None:
    cases = tmp_path / "cases.json"
    cases.write_bytes(
        b'{"schema_version":"guildmind.python-call-oracle/v1","cases":['
        b'{"case_id":"case-0001","args":[2,1.5],"kwargs":{},'
        b'"expected":{"kind":"returned","value":[2,3.0]}}]}'
    )
    protocol = PythonCallProtocol(module="backoff", callable_name="schedule", cases_file=cases)

    bundle = load_python_call_bundle(protocol, expected_case_count=1)

    assert b'"args":[2,1.5]' in bundle.challenge_bytes
    assert b'"value":[2,3.0]' in bundle.oracle_bytes


@pytest.mark.parametrize(
    "argument",
    [
        b'"\\ud800"',
        b'{"nested":{"\\udfff":1}}',
    ],
)
def test_python_call_bundle_rejects_surrogates_before_canonical_encoding(
    tmp_path: Path,
    argument: bytes,
) -> None:
    cases = tmp_path / "cases.json"
    cases.write_bytes(
        b'{"schema_version":"guildmind.python-call-oracle/v1","cases":['
        b'{"case_id":"case-0001","args":['
        + argument
        + b'],"kwargs":{},"expected":{"kind":"returned","value":1}}]}'
    )
    protocol = PythonCallProtocol(module="addition", callable_name="add", cases_file=cases)

    with pytest.raises(FixtureConfigurationError, match="surrogates"):
        load_python_call_bundle(protocol, expected_case_count=1)


def test_container_protocol_case_count_is_bound_to_fixture_manifest() -> None:
    spec = load_fixture(_FIXTURE)
    assert spec.python_call_protocol is not None

    with pytest.raises(FixtureConfigurationError, match="does not match"):
        load_python_call_bundle(
            spec.python_call_protocol,
            expected_case_count=spec.expected_test_count + 1,
        )


def test_python_call_protocol_rejects_dynamic_entrypoint_names() -> None:
    spec = load_fixture(_FIXTURE)
    assert spec.python_call_protocol is not None

    with pytest.raises(FixtureConfigurationError, match="dotted Python name"):
        replace(spec.python_call_protocol, module="addition; import os")


def test_loaded_fixture_seals_canonical_oracle_bytes(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(_FIXTURE, fixture)
    spec = load_fixture(fixture)
    assert spec.python_call_protocol is not None

    first = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    assert spec.python_call_protocol.sealed_cases_bytes == first.oracle_bytes

    spec.python_call_protocol.cases_file.unlink()
    replay = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )

    assert replay == first
