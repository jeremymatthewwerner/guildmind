"""Small, inspectable command-line surface for the measurement substrate."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from guildmind.domain import export_json_schemas
from guildmind.evaluation import LocalEvaluator, load_fixture, require_tracked_clean_revision
from guildmind.models import ScriptedPatchModel
from guildmind.runtime.campaign import (
    CampaignConfigurationError,
    CampaignEvidenceError,
    ensure_campaign_output_available,
    load_reliability_campaign,
    run_reliability_campaign,
    write_reliability_campaign_report,
)
from guildmind.runtime.recovery import (
    RecoveryDeniedError,
    RecoveryPostCommitMaintenanceError,
    recover_existing_fixture_run,
)
from guildmind.runtime.replay import ReplayIntegrityError, replay_events, semantic_digest
from guildmind.runtime.runner import FixtureRunner, FixtureRunPostCommitMaintenanceError
from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxConfigurationError,
    SandboxUnavailableError,
    run_containment_probe_suite,
    run_resource_probe_suite,
    run_sandbox_self_test,
)
from guildmind.storage import (
    EventStore,
    QuarantineDeniedError,
    QuarantineFinalizationError,
    QuarantineIncompleteError,
    StoreIntegrityError,
    quarantine_orphans,
)

_INSPECTION_DENIAL_SCHEMA_VERSION = "guildmind.inspection-denial/v1"


class _InspectionDeniedError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    handler = arguments.handler
    return int(handler(arguments))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guildmind",
        description="Run and inspect reproducible Guildmind experiments.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    doctor = subcommands.add_parser("doctor", help="inspect local execution prerequisites")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument(
        "--production",
        action="store_true",
        help="fail unless the complete reference-host sandbox probe passes",
    )
    doctor.add_argument(
        "--evaluator-image",
        default=os.environ.get("GUILDMIND_REFERENCE_EVALUATOR_IMAGE"),
        help="digest-pinned evaluator image already present on the Docker host",
    )
    doctor.set_defaults(handler=_doctor)

    containment_probe = subcommands.add_parser(
        "probe-containment",
        help="emit active, machine-readable evaluator containment evidence",
    )
    containment_probe.add_argument(
        "--development",
        action="store_true",
        help="label results development-only and allow the relaxed development host policy",
    )
    containment_probe.add_argument(
        "--evaluator-image",
        default=None,
        help="digest-pinned evaluator image already present on the selected Docker host",
    )
    containment_probe.add_argument(
        "--probe-id",
        default=None,
        help="stable evidence identifier (a random identifier is generated when omitted)",
    )
    containment_probe.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write canonical evidence to a new file without overwriting existing evidence",
    )
    containment_probe.set_defaults(handler=_probe_containment)

    resource_probe = subcommands.add_parser(
        "probe-resources",
        help="emit active, machine-readable Docker resource-enforcement evidence",
    )
    resource_probe.add_argument(
        "--development",
        action="store_true",
        help="label results development-only and allow the relaxed development host policy",
    )
    resource_probe.add_argument(
        "--evaluator-image",
        default=None,
        help="digest-pinned evaluator image already present on the selected Docker host",
    )
    resource_probe.add_argument(
        "--probe-id",
        default=None,
        help="stable evidence identifier (a random identifier is generated when omitted)",
    )
    resource_probe.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write canonical evidence to a new file without overwriting existing evidence",
    )
    resource_probe.set_defaults(handler=_probe_resources)

    run = subcommands.add_parser("run", help="run the scripted deterministic fixture")
    run.add_argument(
        "fixture",
        nargs="?",
        type=Path,
        default=Path("fixtures/001-python-addition"),
    )
    run.add_argument("--state-dir", type=Path, default=Path(".guildmind"))
    run.add_argument("--run-id", default=None)
    run.set_defaults(handler=_run)

    campaign = subcommands.add_parser(
        "campaign",
        help="run a frozen fixture-reliability campaign",
    )
    campaign_commands = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_run = campaign_commands.add_parser(
        "run",
        help="run one development-only scripted-patch campaign",
    )
    campaign_run.add_argument("manifest", type=Path)
    campaign_run.add_argument(
        "--repository-root",
        type=Path,
        default=Path("."),
        help="repository containing the manifest, source identity, and fixtures",
    )
    campaign_run.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="new directory in which isolated attempt evidence will be created",
    )
    campaign_run.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new canonical report file; existing paths are never overwritten",
    )
    campaign_run.set_defaults(handler=_run_campaign)

    evaluate = subcommands.add_parser("evaluate", help="evaluate a patch on a local fixture")
    evaluate.add_argument("fixture", type=Path)
    evaluate.add_argument("patch", type=Path)
    evaluate.set_defaults(handler=_evaluate)

    replay = subcommands.add_parser("replay", help="verify and replay a stored event stream")
    replay.add_argument("run_id")
    replay.add_argument("--state-dir", type=Path, default=Path(".guildmind"))
    replay.set_defaults(handler=_replay)

    recover = subcommands.add_parser(
        "recover",
        help="terminalize one interrupted run without redispatching work",
    )
    recover.add_argument("run_id")
    recover.add_argument("--state-dir", type=Path, default=Path(".guildmind"))
    recover.set_defaults(handler=_recover)

    quarantine = subcommands.add_parser(
        "quarantine",
        help="quarantine authorized ownerless artifacts with resumable evidence",
    )
    quarantine.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="existing Guildmind state directory to maintain",
    )
    quarantine.set_defaults(handler=_quarantine)

    report = subcommands.add_parser("report", help="print a stored run summary")
    report.add_argument("run_id")
    report.add_argument("--state-dir", type=Path, default=Path(".guildmind"))
    report.set_defaults(handler=_report)

    schemas = subcommands.add_parser("schemas", help="manage public JSON Schemas")
    schema_commands = schemas.add_subparsers(dest="schema_command", required=True)
    export = schema_commands.add_parser("export", help="write versioned public schemas")
    export.add_argument("--output", type=Path, default=Path("schemas"))
    export.set_defaults(handler=_export_schemas)
    return parser


def _doctor(arguments: argparse.Namespace) -> int:
    python_ok = sys.version_info[:2] == (3, 12)
    git_path = shutil.which("git")
    uv_path = shutil.which("uv")
    docker_path = shutil.which("docker")
    docker_server = False
    docker_diagnostic = "docker executable not found"
    docker_reference_ready = False
    docker_failures: tuple[str, ...] = ("docker_executable_not_found",)
    docker_warnings: tuple[str, ...] = ()
    evaluator_image_id: str | None = None
    evaluator_image_diagnostic = "evaluator image is not configured"
    self_test_passed = False
    self_test_checks: dict[str, bool] = {}
    self_test_diagnostic = "reference host or evaluator image is not ready"
    if docker_path is not None:
        try:
            sandbox = DockerSandbox(docker_executable=docker_path)
            assessment = sandbox.assess_host()
            docker_server = True
            docker_reference_ready = assessment.reference_ready
            docker_failures = assessment.failures
            docker_warnings = assessment.warnings
            docker_diagnostic = "Docker server responded"
            if arguments.evaluator_image is not None:
                try:
                    evaluator_image_id = sandbox.verify_image(arguments.evaluator_image)
                    evaluator_image_diagnostic = "digest-pinned evaluator image verified locally"
                except (SandboxConfigurationError, SandboxUnavailableError) as error:
                    evaluator_image_diagnostic = str(error)
            if docker_reference_ready and evaluator_image_id is not None:
                report = run_sandbox_self_test(sandbox, image=arguments.evaluator_image)
                self_test_passed = report.passed
                self_test_checks = report.checks
                self_test_diagnostic = report.diagnostic or "all active controls passed"
        except (SandboxConfigurationError, SandboxUnavailableError) as error:
            docker_diagnostic = str(error)
    local_ready = python_ok and git_path is not None
    production_sandbox_ready = (
        local_ready
        and docker_server
        and docker_reference_ready
        and evaluator_image_id is not None
        and self_test_passed
    )
    result = {
        "local_fixture_ready": local_ready,
        "production_sandbox_ready": production_sandbox_ready,
        "checks": {
            "docker_server": docker_server,
            "docker_diagnostic": docker_diagnostic,
            "docker_reference_failures": docker_failures,
            "docker_reference_ready": docker_reference_ready,
            "docker_reference_warnings": docker_warnings,
            "evaluator_image_diagnostic": evaluator_image_diagnostic,
            "evaluator_image_id": evaluator_image_id,
            "evaluator_image_reference": arguments.evaluator_image,
            "git": git_path,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_3_12": python_ok,
            "sandbox_self_test": self_test_checks,
            "sandbox_self_test_diagnostic": self_test_diagnostic,
            "sandbox_self_test_passed": self_test_passed,
            "uv": uv_path,
        },
        "warning": (
            "Production readiness requires the rootless x86_64 Linux reference host, "
            "a local digest-pinned image, and the active sandbox control probe."
        ),
    }
    if arguments.as_json:
        _print_json(result)
    else:
        print(f"Local fixture ready: {local_ready}")
        print(f"Production sandbox ready: {production_sandbox_ready}")
        print(result["warning"])
    required_ready = production_sandbox_ready if arguments.production else local_ready
    return 0 if required_ready else 1


def _probe_containment(arguments: argparse.Namespace) -> int:
    environment_variable = (
        "GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE"
        if arguments.development
        else "GUILDMIND_REFERENCE_EVALUATOR_IMAGE"
    )
    image = arguments.evaluator_image or os.environ.get(environment_variable)
    error_schema = "guildmind.containment-probe-error/v1"
    if image is None:
        _print_json(
            {
                "error": f"--evaluator-image or {environment_variable} is required",
                "schema_version": error_schema,
            }
        )
        return 1
    probe_id = arguments.probe_id or f"containment-probe-{uuid.uuid4().hex}"
    if not probe_id.strip():
        _print_json(
            {
                "error": "--probe-id must contain non-whitespace characters",
                "schema_version": error_schema,
            }
        )
        return 1
    if arguments.output is not None:
        if os.path.lexists(arguments.output):
            _print_json(
                {
                    "error": f"containment probe evidence already exists: {arguments.output}",
                    "schema_version": error_schema,
                }
            )
            return 1
        if not arguments.output.parent.is_dir():
            _print_json(
                {
                    "error": f"containment probe evidence directory is unavailable: "
                    f"{arguments.output.parent}",
                    "schema_version": error_schema,
                }
            )
            return 1
    policy = DockerHostPolicy.development_only() if arguments.development else DockerHostPolicy()
    sandbox = DockerSandbox(host_policy=policy)
    try:
        report = run_containment_probe_suite(
            sandbox,
            image=image,
            code_revision=_code_revision(),
            probe_id=probe_id,
        )
    except (OSError, ValueError, SandboxUnavailableError) as error:
        _print_json({"error": str(error), "schema_version": error_schema})
        return 1
    if arguments.output is not None:
        try:
            with arguments.output.open("xb") as stream:
                stream.write(report.canonical_bytes() + b"\n")
        except OSError as error:
            _print_json(
                {
                    "error": f"cannot write containment probe evidence: {error}",
                    "schema_version": error_schema,
                }
            )
            return 1
    _print_json(report.model_dump(mode="json"))
    passed = report.all_contained if arguments.development else report.reference_passed
    return 0 if passed else 1


def _probe_resources(arguments: argparse.Namespace) -> int:
    environment_variable = (
        "GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE"
        if arguments.development
        else "GUILDMIND_REFERENCE_EVALUATOR_IMAGE"
    )
    image = arguments.evaluator_image or os.environ.get(environment_variable)
    if image is None:
        _print_json(
            {
                "error": f"--evaluator-image or {environment_variable} is required",
                "schema_version": "guildmind.resource-probe-error/v1",
            }
        )
        return 1
    probe_id = arguments.probe_id or f"resource-probe-{uuid.uuid4().hex}"
    if not probe_id.strip():
        _print_json(
            {
                "error": "--probe-id must contain non-whitespace characters",
                "schema_version": "guildmind.resource-probe-error/v1",
            }
        )
        return 1
    if arguments.output is not None:
        if os.path.lexists(arguments.output):
            _print_json(
                {
                    "error": f"resource probe evidence already exists: {arguments.output}",
                    "schema_version": "guildmind.resource-probe-error/v1",
                }
            )
            return 1
        if not arguments.output.parent.is_dir():
            _print_json(
                {
                    "error": f"resource probe evidence directory is unavailable: "
                    f"{arguments.output.parent}",
                    "schema_version": "guildmind.resource-probe-error/v1",
                }
            )
            return 1
    policy = DockerHostPolicy.development_only() if arguments.development else DockerHostPolicy()
    sandbox = DockerSandbox(host_policy=policy)
    try:
        report = run_resource_probe_suite(
            sandbox,
            image=image,
            code_revision=_code_revision(),
            probe_id=probe_id,
        )
    except (ValueError, SandboxUnavailableError) as error:
        _print_json(
            {
                "error": str(error),
                "schema_version": "guildmind.resource-probe-error/v1",
            }
        )
        return 1
    if arguments.output is not None:
        try:
            with arguments.output.open("xb") as stream:
                stream.write(report.canonical_bytes() + b"\n")
        except OSError as error:
            _print_json(
                {
                    "error": f"cannot write resource probe evidence: {error}",
                    "schema_version": "guildmind.resource-probe-error/v1",
                }
            )
            return 1
    _print_json(report.model_dump(mode="json"))
    passed = report.all_enforced if arguments.development else report.reference_passed
    return 0 if passed else 1


def _run(arguments: argparse.Namespace) -> int:
    fixture = arguments.fixture.resolve()
    run_id = arguments.run_id or f"run-{uuid.uuid4().hex}"
    model = ScriptedPatchModel(fixture / "solution.patch")
    try:
        result = FixtureRunner(state_directory=arguments.state_dir).run(
            fixture_root=fixture,
            model=model,
            run_id=run_id,
            code_revision=_code_revision(),
        )
    except FixtureRunPostCommitMaintenanceError as error:
        result = error.result
        _print_json(
            {
                "artifacts": str(result.artifact_root),
                "database": str(result.database_path),
                "error": "run_committed_maintenance_release_failed",
                "evaluation_outcome": result.evaluation.outcome,
                "run_id": result.manifest.run_id,
                "schema_version": "guildmind.run-postcommit/v1",
                "semantic_digest": result.semantic_digest,
                "status": result.manifest.status.value,
            }
        )
        return 1
    _print_json(
        {
            "artifacts": str(result.artifact_root),
            "database": str(result.database_path),
            "evaluation_outcome": result.evaluation.outcome,
            "run_id": result.manifest.run_id,
            "semantic_digest": result.semantic_digest,
            "status": result.manifest.status.value,
        }
    )
    return 0 if result.evaluation.outcome == "passed" else 2


def _run_campaign(arguments: argparse.Namespace) -> int:
    error_schema = "guildmind.reliability-campaign-error/v1"
    try:
        output = ensure_campaign_output_available(arguments.output)
        campaign = load_reliability_campaign(
            arguments.manifest,
            repository_root=arguments.repository_root,
        )
        report = run_reliability_campaign(
            campaign,
            state_directory=arguments.state_dir,
            git_revision=_campaign_code_revision(campaign.repository_root),
        )
        write_reliability_campaign_report(report, output)
    except (CampaignConfigurationError, CampaignEvidenceError) as error:
        _print_json(
            {
                "error": str(error),
                "schema_version": error_schema,
            }
        )
        return 1

    body = report.body
    _print_json(
        {
            "all_expected": body.all_expected,
            "attempt_dispositions": [evidence.disposition.value for evidence in body.attempts],
            "campaign_id": body.manifest.campaign_id,
            "campaign_manifest_sha256": body.campaign_manifest_sha256,
            "campaign_passed": body.campaign_passed,
            "code_identity_verified": body.code_identity_verified,
            "complete": body.complete,
            "evidence_tier": body.manifest.evidence_tier.value,
            "expected_attempt_count": body.expected_attempt_count,
            "infrastructure_error_count": body.infrastructure_error_count,
            "infrastructure_error_rate": body.infrastructure_error_rate,
            "intended_attempt_count": body.intended_attempt_count,
            "output": str(output),
            "report_sha256": report.content_sha256,
            "schema_version": "guildmind.reliability-campaign-result/v1",
            "source_manifest_sha256": body.source_manifest_sha256,
            "state_manifest_verified": body.state_manifest_verified,
            "threshold_met": body.threshold_met,
        }
    )
    return 0 if body.campaign_passed else 2


def _evaluate(arguments: argparse.Namespace) -> int:
    spec = load_fixture(arguments.fixture.resolve())
    result = LocalEvaluator().evaluate(spec, arguments.patch.resolve())
    _print_json(
        {
            "exit_code": result.exit_code,
            "output_truncated": result.output_truncated,
            "passed": result.passed,
            "status": result.status.value,
            "stderr": result.stderr,
            "stdout": result.stdout,
            "task_id": result.task_id,
        }
    )
    return 0 if result.passed else 2


def _replay(arguments: argparse.Namespace) -> int:
    try:
        with _verified_inspection_store(arguments.state_dir, arguments.run_id) as store:
            events = store.list_events(arguments.run_id)
            state = replay_events(events)
    except _InspectionDeniedError as error:
        return _print_inspection_denial("replay", arguments.run_id, error.reason)
    except KeyError:
        return _print_inspection_denial("replay", arguments.run_id, "run_not_found")
    except StoreIntegrityError as error:
        reason = (
            "state_changed"
            if "changed while" in str(error)
            else "storage_integrity_validation_failed"
        )
        return _print_inspection_denial("replay", arguments.run_id, reason)
    except (OSError, ReplayIntegrityError, ValueError, sqlite3.DatabaseError):
        return _print_inspection_denial(
            "replay",
            arguments.run_id,
            "storage_integrity_validation_failed",
        )
    _print_json(
        {
            "artifacts": state.artifacts,
            "budget_reserved": state.budget_reserved.model_dump(mode="json"),
            "budget_used": state.budget_used.model_dump(mode="json"),
            "evaluation_outcome": state.evaluation_outcome,
            "event_count": state.event_count,
            "run_id": state.run_id,
            "semantic_digest": semantic_digest(events),
            "status": state.status.value,
        }
    )
    return 0


def _recover(arguments: argparse.Namespace) -> int:
    try:
        result = recover_existing_fixture_run(
            state_directory=arguments.state_dir,
            run_id=arguments.run_id,
        )
    except RecoveryDeniedError as error:
        _print_json(
            {
                "error": "recovery_denied",
                "reason": error.reason.value,
                "run_id": arguments.run_id,
                "schema_version": "guildmind.recovery-denial/v1",
                "storage_state": (
                    None if error.storage_state is None else error.storage_state.value
                ),
            }
        )
        return 1
    except RecoveryPostCommitMaintenanceError as error:
        events = list(error.result.events)
        state = replay_events(events, require_terminal=True)
        _print_json(
            {
                "error": "recovery_committed_maintenance_release_failed",
                "event_count": state.event_count,
                "run_id": error.result.manifest.run_id,
                "schema_version": "guildmind.recovery-postcommit/v1",
                "semantic_digest": semantic_digest(events),
                "status": error.result.manifest.status.value,
                "terminal_reason": error.result.manifest.terminal_reason,
            }
        )
        return 1
    manifest = result.manifest
    events = list(result.events)
    state = replay_events(events, require_terminal=True)
    _print_json(
        {
            "event_count": state.event_count,
            "run_id": manifest.run_id,
            "semantic_digest": semantic_digest(events),
            "status": manifest.status.value,
            "terminal_reason": manifest.terminal_reason,
        }
    )
    return 0


def _quarantine(arguments: argparse.Namespace) -> int:
    try:
        result = quarantine_orphans(arguments.state_dir)
    except QuarantineDeniedError as error:
        _print_json(
            {
                "error": "quarantine_denied",
                "reason": error.reason.value,
                "schema_version": "guildmind.quarantine-denial/v1",
            }
        )
        return 1
    except QuarantineIncompleteError as error:
        _print_json(
            {
                "error": "quarantine_incomplete",
                "reason": error.reason.value,
                "schema_version": "guildmind.quarantine-incomplete/v1",
                "transaction_id": error.transaction_id,
            }
        )
        return 1
    except QuarantineFinalizationError as error:
        response = error.result.model_dump(mode="json")
        response.update(
            {
                "error": "quarantine_finalization_failed",
                "schema_version": "guildmind.quarantine-finalization-failure/v1",
            }
        )
        _print_json(response)
        return 1
    _print_json(result.model_dump(mode="json"))
    return 0


def _report(arguments: argparse.Namespace) -> int:
    try:
        with _verified_inspection_store(arguments.state_dir, arguments.run_id) as store:
            manifest = store.load_manifest(arguments.run_id)
            used, reserved = store.load_budget_state(arguments.run_id)
            events = store.list_events(arguments.run_id)
    except _InspectionDeniedError as error:
        return _print_inspection_denial("report", arguments.run_id, error.reason)
    except KeyError:
        return _print_inspection_denial("report", arguments.run_id, "run_not_found")
    except StoreIntegrityError as error:
        reason = (
            "state_changed"
            if "changed while" in str(error)
            else "storage_integrity_validation_failed"
        )
        return _print_inspection_denial("report", arguments.run_id, reason)
    except (OSError, ReplayIntegrityError, ValueError, sqlite3.DatabaseError):
        return _print_inspection_denial(
            "report",
            arguments.run_id,
            "storage_integrity_validation_failed",
        )
    _print_json(
        {
            "budget_reserved": reserved.model_dump(mode="json"),
            "budget_used": used.model_dump(mode="json"),
            "event_count": len(events),
            "manifest": manifest.model_dump(mode="json"),
            "semantic_digest": semantic_digest(events),
        }
    )
    return 0


@contextmanager
def _verified_inspection_store(
    configured_state_directory: Path,
    run_id: str,
) -> Iterator[EventStore]:
    lexical_state_directory = Path(os.path.abspath(configured_state_directory))
    if lexical_state_directory == lexical_state_directory.parent:
        raise _InspectionDeniedError("filesystem_root_state_forbidden")
    try:
        trusted_parent = lexical_state_directory.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _InspectionDeniedError("state_directory_missing") from error
    trusted_parent_identity = _validate_inspection_real_directory(
        trusted_parent,
        unavailable_reason="state_directory_unavailable",
    )
    state_directory = trusted_parent / lexical_state_directory.name
    state_identity = _validate_inspection_state_directory(state_directory)
    database = state_directory / "runs.db"
    database_identity = _validate_inspection_database_leaf(database)
    with EventStore.open_existing_read_only(
        database,
        trusted_base=trusted_parent,
    ) as store:
        _validate_inspection_real_directory(
            trusted_parent,
            expected_identity=trusted_parent_identity,
            unavailable_reason="state_changed",
        )
        _validate_inspection_state_directory(state_directory, expected_identity=state_identity)
        _validate_inspection_database_leaf(database, expected_identity=database_identity)
        with store.verified_snapshot() as roots:
            _validate_inspection_real_directory(
                trusted_parent,
                expected_identity=trusted_parent_identity,
                unavailable_reason="state_changed",
            )
            _validate_inspection_state_directory(
                state_directory,
                expected_identity=state_identity,
            )
            _validate_inspection_database_leaf(database, expected_identity=database_identity)
            if all(root.manifest.run_id != run_id for root in roots):
                raise KeyError(f"unknown run: {run_id}")
            try:
                yield store
            finally:
                _validate_inspection_real_directory(
                    trusted_parent,
                    expected_identity=trusted_parent_identity,
                    unavailable_reason="state_changed",
                )
                _validate_inspection_state_directory(
                    state_directory,
                    expected_identity=state_identity,
                )
                _validate_inspection_database_leaf(
                    database,
                    expected_identity=database_identity,
                )
        _validate_inspection_real_directory(
            trusted_parent,
            expected_identity=trusted_parent_identity,
            unavailable_reason="state_changed",
        )
        _validate_inspection_state_directory(state_directory, expected_identity=state_identity)
        _validate_inspection_database_leaf(database, expected_identity=database_identity)


def _validate_inspection_real_directory(
    directory: Path,
    *,
    unavailable_reason: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        metadata = directory.lstat()
    except OSError as error:
        raise _InspectionDeniedError(unavailable_reason) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _InspectionDeniedError(unavailable_reason)
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise _InspectionDeniedError("state_changed")
    return identity


def _validate_inspection_state_directory(
    state_directory: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        metadata = state_directory.lstat()
    except FileNotFoundError as error:
        reason = "state_changed" if expected_identity is not None else "state_directory_missing"
        raise _InspectionDeniedError(reason) from error
    except OSError as error:
        raise _InspectionDeniedError("state_directory_unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        reason = "state_changed" if expected_identity is not None else "state_directory_not_real"
        raise _InspectionDeniedError(reason)
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise _InspectionDeniedError("state_changed")
    return identity


def _validate_inspection_database_leaf(
    database: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    try:
        metadata = database.lstat()
    except FileNotFoundError as error:
        reason = "state_changed" if expected_identity is not None else "database_missing"
        raise _InspectionDeniedError(reason) from error
    except OSError as error:
        reason = "state_changed" if expected_identity is not None else "database_unavailable"
        raise _InspectionDeniedError(reason) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        reason = "state_changed" if expected_identity is not None else "database_not_real_file"
        raise _InspectionDeniedError(reason)
    identity = (metadata.st_dev, metadata.st_ino)
    if expected_identity is not None and identity != expected_identity:
        raise _InspectionDeniedError("state_changed")
    return identity


def _print_inspection_denial(command: str, run_id: str, reason: str) -> int:
    _print_json(
        {
            "command": command,
            "error": "inspection_denied",
            "reason": reason,
            "run_id": run_id,
            "schema_version": _INSPECTION_DENIAL_SCHEMA_VERSION,
        }
    )
    return 1


def _export_schemas(arguments: argparse.Namespace) -> int:
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for model_name, schema in export_json_schemas().items():
        slug = re.sub(r"(?<!^)(?=[A-Z])", "-", model_name).lower()
        path = output / f"{slug}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        paths.append(str(path))
    _print_json({"exported": paths})
    return 0


def _code_revision() -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return f"{revision}+dirty" if dirty else revision


def _campaign_code_revision(repository: Path) -> str:
    """Bind campaign provenance to its declared tracked-clean repository."""

    try:
        return require_tracked_clean_revision(repository)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise CampaignConfigurationError(
            f"campaign repository revision is unavailable: {error}"
        ) from error


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))
