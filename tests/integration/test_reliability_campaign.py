import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildmind.cli import main
from guildmind.domain import (
    BudgetUsage,
    CampaignAttemptDisposition,
    ReliabilityCampaignReport,
    RunManifest,
    sha256_bytes,
)
from guildmind.evaluation import LocalEvaluationResult, LocalEvaluationSpec, LocalEvaluator
from guildmind.runtime.campaign import (
    CampaignEvidenceError,
    LoadedReliabilityCampaign,
    campaign_code_source_sha256,
    campaign_fixture_tree_sha256,
    load_reliability_campaign,
    load_reliability_campaign_report,
    reconcile_reliability_campaign,
    run_reliability_campaign,
    write_campaign_evidence_file,
    write_reliability_campaign_report,
)
from guildmind.runtime.clock import DeterministicClock
from guildmind.storage import EventStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE_ID = "fixture-001-python-addition"
_ATTEMPT_ID = "stage1-local-smoke-r001-fixture-001"
_START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


class _RaisingEvaluator:
    @property
    def evaluator_version(self) -> str:
        return LocalEvaluator().evaluator_version

    @property
    def environment_digest(self) -> str:
        return LocalEvaluator().environment_digest

    def evaluate(
        self,
        spec: LocalEvaluationSpec,
        patch_path: Path,
        *,
        expected_patch_sha256: str | None = None,
    ) -> LocalEvaluationResult:
        del spec, patch_path, expected_patch_sha256
        raise RuntimeError("injected evaluator failure")


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


def _manifest_values(
    repository: Path,
    *,
    maximum_total_tokens: int = 128,
) -> dict[str, object]:
    evaluator = LocalEvaluator()
    fixture = repository / "fixtures" / "001-python-addition"
    return {
        "schema_version": "guildmind.reliability-campaign/v1",
        "report_schema_version": "guildmind.reliability-campaign-report/v1",
        "campaign_id": "stage1-local-smoke-v1",
        "evidence_tier": "development",
        "experiment_id": "experiment-0001",
        "candidate_id": "scripted-solo-v0",
        "code_source_sha256": campaign_code_source_sha256(repository),
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
            "max_total_tokens": maximum_total_tokens,
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
                "fixture_tree_sha256": campaign_fixture_tree_sha256(fixture),
                "solution_patch_sha256": sha256_bytes((fixture / "solution.patch").read_bytes()),
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


def _load_campaign(
    tmp_path: Path,
    *,
    maximum_total_tokens: int = 128,
) -> LoadedReliabilityCampaign:
    repository = _copy_campaign_repository(tmp_path)
    manifest = repository / "campaigns" / "stage1-local-smoke-v1.json"
    manifest.write_text(
        json.dumps(
            _manifest_values(repository, maximum_total_tokens=maximum_total_tokens),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return load_reliability_campaign(manifest, repository_root=repository)


def _run_campaign(
    campaign: LoadedReliabilityCampaign,
    state: Path,
    *,
    evaluator: _RaisingEvaluator | None = None,
) -> ReliabilityCampaignReport:
    return run_reliability_campaign(
        campaign,
        state_directory=state,
        evaluator=evaluator,
        clock_factory=lambda _: DeterministicClock(started_at=_START),
        git_revision="integration-test-revision",
        recorded_at=_START + timedelta(hours=1),
    )


def _prepare_reconciliation_state(
    campaign: LoadedReliabilityCampaign,
    state: Path,
) -> Path:
    state.mkdir()
    attempts = state / "attempts"
    attempts.mkdir()
    write_campaign_evidence_file(
        json.dumps(
            campaign.manifest.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        state / "campaign-manifest.json",
    )
    return attempts


def _pending_manifest(
    campaign: LoadedReliabilityCampaign,
    run_id: str,
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        experiment_id=campaign.manifest.experiment_id,
        task_id=_FIXTURE_ID,
        candidate_id=campaign.manifest.candidate_id,
        requested_model=campaign.manifest.model.model_id,
        seed=0,
        environment_digest=campaign.manifest.evaluator.environment_digest,
        code_revision=f"source-sha256:{campaign.manifest.code_source_sha256}",
        budget_limits=campaign.manifest.budget_limits,
        created_at=_START,
    )


def test_one_fixture_campaign_reconciles_and_publishes_canonical_report(
    tmp_path: Path,
) -> None:
    campaign = _load_campaign(tmp_path)
    report = _run_campaign(campaign, tmp_path / "state")

    assert report.body.campaign_passed is True
    assert report.body.complete is True
    assert report.body.all_expected is True
    assert report.body.intended_attempt_count == 1
    assert report.body.infrastructure_error_count == 0
    assert report.body.infrastructure_error_rate == 0.0
    evidence = report.body.attempts[0]
    assert evidence.disposition is CampaignAttemptDisposition.EXPECTED
    assert evidence.observed_run_ids == (_ATTEMPT_ID,)
    assert evidence.terminal is not None
    assert evidence.terminal.evaluator_version == LocalEvaluator().evaluator_version
    assert evidence.terminal.environment_digest == LocalEvaluator().environment_digest
    assert evidence.terminal.requested_model == "guildmind/fake-scripted-patch-v1"
    assert evidence.terminal.returned_model == "guildmind/fake-scripted-patch-v1"
    assert evidence.terminal.budget_used == BudgetUsage(
        uncached_input_tokens=24,
        output_tokens=16,
        model_calls=1,
    )
    assert evidence.terminal.budget_reserved == BudgetUsage()

    output = tmp_path / "report.json"
    write_reliability_campaign_report(report, output)
    assert output.read_bytes() == report.canonical_bytes() + b"\n"
    assert load_reliability_campaign_report(output) == report
    with pytest.raises(CampaignEvidenceError, match="already exists"):
        write_reliability_campaign_report(report, output)


def test_report_loader_rejects_derived_claim_and_body_hash_tampering(tmp_path: Path) -> None:
    campaign = _load_campaign(tmp_path)
    report = _run_campaign(campaign, tmp_path / "state")
    raw = report.model_dump(mode="json")
    body = raw["body"]
    assert isinstance(body, dict)
    body["campaign_passed"] = False
    tampered_claim = tmp_path / "tampered-claim.json"
    tampered_claim.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CampaignEvidenceError, match="aggregate claims must be derived"):
        load_reliability_campaign_report(tampered_claim)

    raw = report.model_dump(mode="json")
    raw["body_sha256"] = "0" * 64
    tampered_hash = tmp_path / "tampered-hash.json"
    tampered_hash.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CampaignEvidenceError, match="body hash mismatch"):
        load_reliability_campaign_report(tampered_hash)

    duplicate_key = tmp_path / "duplicate-key.json"
    duplicate_key.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(CampaignEvidenceError, match="duplicate key 'schema_version'"):
        load_reliability_campaign_report(duplicate_key)


def test_execution_exception_is_recovered_but_remains_infrastructure_error(
    tmp_path: Path,
) -> None:
    campaign = _load_campaign(tmp_path)
    report = _run_campaign(
        campaign,
        tmp_path / "state",
        evaluator=_RaisingEvaluator(),
    )

    evidence = report.body.attempts[0]
    assert evidence.disposition is CampaignAttemptDisposition.INFRASTRUCTURE_ERROR
    assert evidence.execution_error_type == "RuntimeError"
    assert evidence.recovery_attempted is True
    assert evidence.recovery_succeeded is True
    assert evidence.terminal is not None
    assert evidence.terminal.run_status.value == "infrastructure_error"
    assert report.body.complete is True
    assert report.body.infrastructure_error_count == 1
    assert report.body.infrastructure_error_rate == 1.0
    assert report.body.campaign_passed is False


def test_budget_refusal_is_complete_unexpected_result_not_infrastructure_error(
    tmp_path: Path,
) -> None:
    campaign = _load_campaign(tmp_path, maximum_total_tokens=127)
    report = _run_campaign(campaign, tmp_path / "state")

    evidence = report.body.attempts[0]
    assert evidence.disposition is CampaignAttemptDisposition.UNEXPECTED_RESULT
    assert evidence.terminal is not None
    assert evidence.terminal.run_status.value == "budget_exhausted"
    assert report.body.complete is True
    assert report.body.infrastructure_error_count == 0
    assert report.body.campaign_passed is False


def test_reconciliation_classifies_missing_nonterminal_and_invalid_run_sets(
    tmp_path: Path,
) -> None:
    missing_campaign = _load_campaign(tmp_path / "missing")
    missing_state = tmp_path / "missing-state"
    _prepare_reconciliation_state(missing_campaign, missing_state)
    missing = reconcile_reliability_campaign(
        missing_campaign,
        state_directory=missing_state,
        git_revision="integration-test-revision",
        recorded_at=_START,
    )
    assert missing.body.attempts[0].disposition is CampaignAttemptDisposition.EVIDENCE_MISSING
    assert missing.body.complete is False
    assert missing.body.infrastructure_error_count == 1

    nonterminal_campaign = _load_campaign(tmp_path / "nonterminal")
    nonterminal_state = tmp_path / "nonterminal-state"
    attempts = _prepare_reconciliation_state(nonterminal_campaign, nonterminal_state)
    attempt_state = attempts / _ATTEMPT_ID
    attempt_state.mkdir()
    with EventStore(
        attempt_state / "runs.db",
        clock=DeterministicClock(started_at=_START),
    ) as store:
        store.create_run(_pending_manifest(nonterminal_campaign, _ATTEMPT_ID))
    nonterminal = reconcile_reliability_campaign(
        nonterminal_campaign,
        state_directory=nonterminal_state,
        git_revision="integration-test-revision",
        recorded_at=_START,
    )
    assert (
        nonterminal.body.attempts[0].disposition is CampaignAttemptDisposition.EVIDENCE_NONTERMINAL
    )

    duplicate_campaign = _load_campaign(tmp_path / "duplicate")
    duplicate_state = tmp_path / "duplicate-state"
    attempts = _prepare_reconciliation_state(duplicate_campaign, duplicate_state)
    attempt_state = attempts / _ATTEMPT_ID
    attempt_state.mkdir()
    with EventStore(
        attempt_state / "runs.db",
        clock=DeterministicClock(started_at=_START),
    ) as store:
        store.create_run(_pending_manifest(duplicate_campaign, _ATTEMPT_ID))
        store.create_run(_pending_manifest(duplicate_campaign, "rogue run"))
    duplicate = reconcile_reliability_campaign(
        duplicate_campaign,
        state_directory=duplicate_state,
        git_revision="integration-test-revision",
        recorded_at=_START,
    )
    assert (
        duplicate.body.attempts[0].disposition
        is CampaignAttemptDisposition.EVIDENCE_RUN_SET_INVALID
    )
    assert duplicate.body.attempts[0].observed_run_ids == (
        "rogue run",
        _ATTEMPT_ID,
    )


def test_reconciliation_classifies_corrupt_replay_evidence(tmp_path: Path) -> None:
    campaign = _load_campaign(tmp_path)
    state = tmp_path / "state"
    _run_campaign(campaign, state)
    database = state / "attempts" / _ATTEMPT_ID / "runs.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE events SET event_hash = ? WHERE run_id = ? AND sequence = 0",
            ("f" * 64, _ATTEMPT_ID),
        )

    report = reconcile_reliability_campaign(
        campaign,
        state_directory=state,
        git_revision="integration-test-revision",
        recorded_at=_START,
    )

    assert report.body.attempts[0].disposition is CampaignAttemptDisposition.EVIDENCE_INVALID
    assert report.body.attempts[0].terminal is None
    assert report.body.complete is False
    assert report.body.infrastructure_error_count == 1


def test_campaign_cli_runs_and_publishes_one_development_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    campaign = _load_campaign(tmp_path)
    state = tmp_path / "cli-state"
    output = tmp_path / "cli-report.json"

    exit_code = main(
        [
            "campaign",
            "run",
            str(campaign.manifest_path),
            "--repository-root",
            str(campaign.repository_root),
            "--state-dir",
            str(state),
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["schema_version"] == "guildmind.reliability-campaign-result/v1"
    assert response["campaign_passed"] is True
    assert response["attempt_dispositions"] == ["expected"]
    assert response["output"] == str(output)
    assert load_reliability_campaign_report(output).body.campaign_passed is True


def test_campaign_cli_writes_valid_failed_gate_with_exit_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    campaign = _load_campaign(tmp_path, maximum_total_tokens=127)
    output = tmp_path / "failed-report.json"

    exit_code = main(
        [
            "campaign",
            "run",
            str(campaign.manifest_path),
            "--repository-root",
            str(campaign.repository_root),
            "--state-dir",
            str(tmp_path / "failed-state"),
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert response["campaign_passed"] is False
    assert response["attempt_dispositions"] == ["unexpected_result"]
    assert response["infrastructure_error_count"] == 0
    assert load_reliability_campaign_report(output).body.complete is True


def test_campaign_cli_refuses_existing_output_before_creating_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    campaign = _load_campaign(tmp_path)
    state = tmp_path / "must-not-exist"
    output = tmp_path / "occupied.json"
    output.write_text("owner data", encoding="utf-8")

    exit_code = main(
        [
            "campaign",
            "run",
            str(campaign.manifest_path),
            "--repository-root",
            str(campaign.repository_root),
            "--state-dir",
            str(state),
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.reliability-campaign-error/v1"
    assert "already exists" in response["error"]
    assert output.read_text(encoding="utf-8") == "owner data"
    assert not state.exists()
