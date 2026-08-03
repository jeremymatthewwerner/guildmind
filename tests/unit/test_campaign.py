import copy
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from guildmind.domain import ReliabilityCampaignManifest, canonical_sha256, sha256_bytes
from guildmind.evaluation import LocalEvaluator
from guildmind.runtime.campaign import (
    CampaignConfigurationError,
    campaign_code_source_sha256,
    campaign_fixture_tree_sha256,
    load_reliability_campaign,
    load_reliability_campaign_manifest,
    load_reliability_campaign_report,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE_ID = "fixture-001-python-addition"
_ATTEMPT_ID = "stage1-local-smoke-r001-fixture-001"


def _manifest_values() -> dict[str, object]:
    evaluator = LocalEvaluator()
    return {
        "schema_version": "guildmind.reliability-campaign/v1",
        "report_schema_version": "guildmind.reliability-campaign-report/v1",
        "campaign_id": "stage1-local-smoke-v1",
        "evidence_tier": "development",
        "experiment_id": "experiment-0001",
        "candidate_id": "scripted-solo-v0",
        "code_source_sha256": "a" * 64,
        "evaluator": {
            "kind": "local",
            "evaluator_version": evaluator.evaluator_version,
            "environment_digest": evaluator.environment_digest,
        },
        "model": {
            "kind": "scripted_patch",
            "model_id": "guildmind/fake-scripted-patch-v1",
        },
        "budget_limits": {
            "schema_version": "0.1",
            "max_total_tokens": 128,
            "max_model_calls": 1,
            "max_model_retries": 0,
            "max_tool_calls": 0,
        },
        "rounds": 1,
        "retry_limit": 0,
        "maximum_infrastructure_error_rate": 0.01,
        "fixtures": [
            {
                "fixture_id": _FIXTURE_ID,
                "fixture_path": "fixtures/001-python-addition",
                "fixture_tree_sha256": "b" * 64,
                "solution_patch_sha256": "c" * 64,
                "expected_run_status": "succeeded",
                "expected_evaluation_outcome": "passed",
            }
        ],
        "attempts": [
            {
                "attempt_id": _ATTEMPT_ID,
                "fixture_id": _FIXTURE_ID,
                "round_index": 0,
                "seed": 0,
            }
        ],
    }


def _copy_campaign_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    shutil.copytree(
        _REPOSITORY_ROOT / "src" / "guildmind",
        repository / "src" / "guildmind",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(_REPOSITORY_ROOT / "pyproject.toml", repository / "pyproject.toml")
    shutil.copy2(_REPOSITORY_ROOT / "uv.lock", repository / "uv.lock")
    shutil.copytree(
        _REPOSITORY_ROOT / "fixtures" / "001-python-addition",
        repository / "fixtures" / "001-python-addition",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    (repository / "campaigns").mkdir()
    return repository


def _repository_manifest_values(repository: Path) -> dict[str, object]:
    values = _manifest_values()
    fixture = repository / "fixtures" / "001-python-addition"
    values["code_source_sha256"] = campaign_code_source_sha256(repository)
    fixture_values = values["fixtures"]
    assert isinstance(fixture_values, list)
    fixture_record = fixture_values[0]
    assert isinstance(fixture_record, dict)
    fixture_record["fixture_tree_sha256"] = campaign_fixture_tree_sha256(fixture)
    fixture_record["solution_patch_sha256"] = sha256_bytes(
        (fixture / "solution.patch").read_bytes()
    )
    return values


def _write_manifest(repository: Path, values: dict[str, object]) -> Path:
    path = repository / "campaigns" / "stage1-local-smoke-v1.json"
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_manifest_accepts_one_complete_content_bound_attempt() -> None:
    manifest = ReliabilityCampaignManifest.model_validate(_manifest_values())

    assert manifest.retry_limit == 0
    assert manifest.budget_limits.max_model_retries == 0
    assert tuple(item.attempt_id for item in manifest.attempts) == (_ATTEMPT_ID,)
    assert len(manifest.content_sha256) == 64


@pytest.mark.parametrize(
    "fixture_path",
    (
        "/fixtures/001-python-addition",
        "../fixtures/001-python-addition",
        "fixtures\\001-python-addition",
        "fixtures//001-python-addition",
    ),
)
def test_manifest_rejects_unsafe_or_noncanonical_fixture_paths(fixture_path: str) -> None:
    values = _manifest_values()
    fixtures = values["fixtures"]
    assert isinstance(fixtures, list)
    assert isinstance(fixtures[0], dict)
    fixtures[0]["fixture_path"] = fixture_path

    with pytest.raises(ValidationError, match="plain POSIX relative path"):
        ReliabilityCampaignManifest.model_validate(values)


def test_manifest_rejects_unknown_fields_and_any_retry_authority() -> None:
    unknown = _manifest_values()
    unknown["undeclared_policy"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ReliabilityCampaignManifest.model_validate(unknown)

    retry = _manifest_values()
    retry["retry_limit"] = 1
    with pytest.raises(ValidationError):
        ReliabilityCampaignManifest.model_validate(retry)

    model_retry = _manifest_values()
    limits = model_retry["budget_limits"]
    assert isinstance(limits, dict)
    limits["max_model_retries"] = 1
    with pytest.raises(ValidationError, match="disable model retries"):
        ReliabilityCampaignManifest.model_validate(model_retry)


def test_manifest_rejects_duplicate_fixture_ids_paths_and_attempt_ids() -> None:
    duplicate_id = _manifest_values()
    fixtures = duplicate_id["fixtures"]
    assert isinstance(fixtures, list)
    fixtures.append(copy.deepcopy(fixtures[0]))
    with pytest.raises(ValidationError, match="unique and ordered"):
        ReliabilityCampaignManifest.model_validate(duplicate_id)

    duplicate_path = _manifest_values()
    fixtures = duplicate_path["fixtures"]
    attempts = duplicate_path["attempts"]
    assert isinstance(fixtures, list)
    assert isinstance(attempts, list)
    second_fixture = copy.deepcopy(fixtures[0])
    assert isinstance(second_fixture, dict)
    second_fixture["fixture_id"] = "fixture-002-duplicate-path"
    fixtures.append(second_fixture)
    attempts.append(
        {
            "attempt_id": "stage1-local-smoke-r001-fixture-002",
            "fixture_id": "fixture-002-duplicate-path",
            "round_index": 0,
            "seed": 1,
        }
    )
    with pytest.raises(ValidationError, match="fixture paths must be unique"):
        ReliabilityCampaignManifest.model_validate(duplicate_path)

    duplicate_attempt = _manifest_values()
    duplicate_attempt["rounds"] = 2
    attempts = duplicate_attempt["attempts"]
    assert isinstance(attempts, list)
    second_attempt = copy.deepcopy(attempts[0])
    assert isinstance(second_attempt, dict)
    second_attempt["round_index"] = 1
    attempts.append(second_attempt)
    with pytest.raises(ValidationError, match="attempt IDs must be unique"):
        ReliabilityCampaignManifest.model_validate(duplicate_attempt)


def test_manifest_rejects_incomplete_or_noncanonical_schedule() -> None:
    incomplete = _manifest_values()
    incomplete["rounds"] = 2
    with pytest.raises(ValidationError, match="complete round-major"):
        ReliabilityCampaignManifest.model_validate(incomplete)

    out_of_order = _manifest_values()
    fixtures = out_of_order["fixtures"]
    attempts = out_of_order["attempts"]
    assert isinstance(fixtures, list)
    assert isinstance(attempts, list)
    second_fixture = copy.deepcopy(fixtures[0])
    assert isinstance(second_fixture, dict)
    second_fixture["fixture_id"] = "fixture-002-other"
    second_fixture["fixture_path"] = "fixtures/002-other"
    fixtures.append(second_fixture)
    attempts.insert(
        0,
        {
            "attempt_id": "stage1-local-smoke-r001-fixture-002",
            "fixture_id": "fixture-002-other",
            "round_index": 0,
            "seed": 1,
        },
    )
    with pytest.raises(ValidationError, match="complete round-major"):
        ReliabilityCampaignManifest.model_validate(out_of_order)


def test_reference_manifest_requires_container_evaluator() -> None:
    values = _manifest_values()
    values["evidence_tier"] = "reference"

    with pytest.raises(ValidationError, match="require the container evaluator"):
        ReliabilityCampaignManifest.model_validate(values)


def test_loader_accepts_verified_repository_and_rejects_duplicate_json_key(
    tmp_path: Path,
) -> None:
    repository = _copy_campaign_repository(tmp_path)
    values = _repository_manifest_values(repository)
    path = _write_manifest(repository, values)

    loaded = load_reliability_campaign(path, repository_root=repository)

    assert loaded.manifest.campaign_id == "stage1-local-smoke-v1"
    assert loaded.source_manifest_sha256 == sha256_bytes(path.read_bytes())
    assert loaded.fixtures[0].root == repository / "fixtures" / "001-python-addition"

    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace('"rounds": 1', '"rounds": 1, "rounds": 1'), encoding="utf-8")
    with pytest.raises(CampaignConfigurationError, match="duplicate key 'rounds'"):
        load_reliability_campaign(path, repository_root=repository)


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("code_source_sha256", "code source digest mismatch"),
        ("fixture_tree_sha256", "fixture tree digest mismatch"),
        ("solution_patch_sha256", "solution patch digest mismatch"),
    ),
)
def test_loader_rejects_mismatched_content_identity(
    tmp_path: Path,
    field: str,
    message: str,
) -> None:
    repository = _copy_campaign_repository(tmp_path)
    values = _repository_manifest_values(repository)
    if field == "code_source_sha256":
        values[field] = "0" * 64
    else:
        fixtures = values["fixtures"]
        assert isinstance(fixtures, list)
        assert isinstance(fixtures[0], dict)
        fixtures[0][field] = "0" * 64
    path = _write_manifest(repository, values)

    with pytest.raises(CampaignConfigurationError, match=message):
        load_reliability_campaign(path, repository_root=repository)


def test_loader_rejects_fixture_changed_after_manifest_was_frozen(tmp_path: Path) -> None:
    repository = _copy_campaign_repository(tmp_path)
    values = _repository_manifest_values(repository)
    path = _write_manifest(repository, values)
    addition = repository / "fixtures" / "001-python-addition" / "workspace" / "addition.py"
    addition.write_text(addition.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(CampaignConfigurationError, match="fixture tree digest mismatch"):
        load_reliability_campaign(path, repository_root=repository)


@pytest.mark.parametrize(
    ("manifest_name", "campaign_id", "fixture_count"),
    [
        ("stage1-local-smoke-v1.json", "stage1-local-smoke-v1", 1),
        ("stage1-local-batch-001-v1.json", "stage1-local-batch-001-v1", 5),
        ("stage1-local-batch-002-v1.json", "stage1-local-batch-002-v1", 9),
        ("stage1-local-batch-003-v1.json", "stage1-local-batch-003-v1", 13),
        ("stage1-local-batch-004-v1.json", "stage1-local-batch-004-v1", 17),
    ],
)
def test_checked_in_campaign_manifest_is_a_valid_contract(
    manifest_name: str,
    campaign_id: str,
    fixture_count: int,
) -> None:
    manifest_path = _REPOSITORY_ROOT / "campaigns" / manifest_name
    manifest = load_reliability_campaign_manifest(manifest_path)

    assert manifest.campaign_id == campaign_id
    assert len(manifest.fixtures) == fixture_count
    assert len(manifest.attempts) == fixture_count * manifest.rounds
    assert manifest.content_sha256 == canonical_sha256(manifest)


@pytest.mark.parametrize(
    "manifest_name",
    [
        "stage1-local-smoke-v1.json",
        "stage1-local-batch-001-v1.json",
        "stage1-local-batch-002-v1.json",
        "stage1-local-batch-003-v1.json",
    ],
)
def test_checked_in_historical_manifest_cannot_run_after_source_drift(
    manifest_name: str,
) -> None:
    manifest_path = _REPOSITORY_ROOT / "campaigns" / manifest_name
    historical = load_reliability_campaign_manifest(manifest_path)
    assert historical.code_source_sha256 != campaign_code_source_sha256(_REPOSITORY_ROOT)

    with pytest.raises(CampaignConfigurationError, match="code source digest mismatch"):
        load_reliability_campaign(manifest_path, repository_root=_REPOSITORY_ROOT)


@pytest.mark.parametrize(
    ("manifest_name", "report_relative", "attempt_count"),
    [
        (
            "stage1-local-smoke-v1.json",
            "2026-08-03-one-fixture-smoke/report.json",
            1,
        ),
        (
            "stage1-local-batch-001-v1.json",
            "2026-08-03-batch-001-local-calibration/report.json",
            5,
        ),
        (
            "stage1-local-batch-002-v1.json",
            "2026-08-03-batch-002-local-calibration/report.json",
            9,
        ),
        (
            "stage1-local-batch-003-v1.json",
            "2026-08-03-batch-003-local-calibration/report.json",
            13,
        ),
        (
            "stage1-local-batch-004-v1.json",
            "2026-08-03-batch-004-local-calibration/report.json",
            17,
        ),
    ],
)
def test_checked_in_campaign_report_matches_its_source_manifest(
    manifest_name: str,
    report_relative: str,
    attempt_count: int,
) -> None:
    manifest_path = _REPOSITORY_ROOT / "campaigns" / manifest_name
    manifest = load_reliability_campaign_manifest(manifest_path)
    report = load_reliability_campaign_report(
        _REPOSITORY_ROOT / "docs" / "evidence" / "reliability-campaigns" / report_relative
    )

    assert report.body.manifest == manifest
    assert report.body.source_manifest_sha256 == sha256_bytes(manifest_path.read_bytes())
    assert report.body.campaign_manifest_sha256 == manifest.content_sha256
    assert report.body.intended_attempt_count == attempt_count
    assert report.body.expected_attempt_count == attempt_count
    assert report.body.infrastructure_error_count == 0
    assert report.body.campaign_passed is True
