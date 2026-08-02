"""Small, inspectable command-line surface for the measurement substrate."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from guildmind.domain import export_json_schemas
from guildmind.evaluation import LocalEvaluator, load_fixture
from guildmind.models import ScriptedPatchModel
from guildmind.runtime.replay import replay_events, semantic_digest
from guildmind.runtime.runner import FixtureRunner
from guildmind.sandbox import (
    DockerHostPolicy,
    DockerSandbox,
    SandboxConfigurationError,
    SandboxUnavailableError,
    run_resource_probe_suite,
    run_sandbox_self_test,
)
from guildmind.storage import EventStore


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
    _print_json(report.model_dump(mode="json"))
    passed = report.all_enforced if arguments.development else report.reference_passed
    return 0 if passed else 1


def _run(arguments: argparse.Namespace) -> int:
    fixture = arguments.fixture.resolve()
    run_id = arguments.run_id or f"run-{uuid.uuid4().hex}"
    model = ScriptedPatchModel(fixture / "solution.patch")
    result = FixtureRunner(state_directory=arguments.state_dir).run(
        fixture_root=fixture,
        model=model,
        run_id=run_id,
        code_revision=_code_revision(),
    )
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
    database = arguments.state_dir.resolve() / "runs.db"
    with EventStore(database) as store:
        events = store.list_events(arguments.run_id)
    state = replay_events(events)
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
    state_directory = arguments.state_dir.resolve()
    manifest = FixtureRunner(state_directory=state_directory).recover(arguments.run_id)
    with EventStore(state_directory / "runs.db") as store:
        events = store.list_events(arguments.run_id)
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


def _report(arguments: argparse.Namespace) -> int:
    database = arguments.state_dir.resolve() / "runs.db"
    with EventStore(database) as store:
        manifest = store.load_manifest(arguments.run_id)
        used, reserved = store.load_budget_state(arguments.run_id)
        events = store.list_events(arguments.run_id)
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


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))
