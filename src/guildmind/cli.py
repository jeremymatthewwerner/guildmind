"""Small, inspectable command-line surface for the measurement substrate."""

from __future__ import annotations

import argparse
import json
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
    doctor.set_defaults(handler=_doctor)

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
    if docker_path is not None:
        try:
            completed = subprocess.run(
                [docker_path, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            docker_server = completed.returncode == 0 and bool(completed.stdout.strip())
            docker_diagnostic = (
                completed.stdout.strip() if docker_server else completed.stderr.strip()
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            docker_diagnostic = str(error)
    is_linux = sys.platform.startswith("linux")
    cgroup_v2 = is_linux and Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    local_ready = python_ok and git_path is not None
    production_sandbox_ready = local_ready and docker_server and is_linux and cgroup_v2
    result = {
        "local_fixture_ready": local_ready,
        "production_sandbox_ready": production_sandbox_ready,
        "checks": {
            "cgroup_v2": cgroup_v2 if is_linux else "not-applicable-on-this-host",
            "docker_server": docker_server,
            "docker_diagnostic": docker_diagnostic,
            "git": git_path,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_3_12": python_ok,
            "uv": uv_path,
        },
        "warning": (
            "The local fixture adapter is trusted-code engineering infrastructure, "
            "not the production Linux isolation boundary."
        ),
    }
    if arguments.as_json:
        _print_json(result)
    else:
        print(f"Local fixture ready: {local_ready}")
        print(f"Production sandbox ready: {production_sandbox_ready}")
        print(result["warning"])
    return 0 if local_ready else 1


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
