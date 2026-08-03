import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import guildmind.cli as cli_module
from guildmind.cli import main
from guildmind.domain import BudgetLimits, RunManifest
from guildmind.sandbox import (
    DockerHostAssessment,
    DockerHostMode,
    DockerHostPolicy,
    SandboxSelfTestReport,
)
from guildmind.storage import EventStore, VerifiedRunRoot

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


def _create_inspection_state(state_directory: Path, run_id: str = "run-inspect") -> Path:
    database = state_directory / "runs.db"
    manifest = RunManifest(
        run_id=run_id,
        experiment_id="experiment-cli-inspection",
        task_id="fixture-cli-inspection",
        candidate_id="scripted-solo",
        requested_model="fake-model-v1",
        seed=0,
        environment_digest=f"sha256:{'a' * 64}",
        code_revision="test-revision",
        budget_limits=BudgetLimits(max_model_calls=1, max_total_tokens=100),
        created_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )
    with EventStore(database) as store:
        store.create_run(manifest)
    return database


def _regular_file_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.is_file() and not path.is_symlink()
    }


def test_schema_export_writes_every_public_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "schemas"

    assert main(["schemas", "export", "--output", str(output)]) == 0

    response = json.loads(capsys.readouterr().out)
    exported = sorted(output.glob("*.schema.json"))
    assert len(exported) == 8
    assert len(response["exported"]) == 8
    assert json.loads((output / "event-record.schema.json").read_text())["title"] == "EventRecord"


def test_evaluate_command_runs_the_known_solution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["evaluate", str(_FIXTURE), str(_FIXTURE / "solution.patch")])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["passed"] is True
    assert response["status"] == "passed"


def test_recover_command_is_idempotent_for_a_terminal_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    assert (
        main(
            [
                "run",
                str(_FIXTURE),
                "--state-dir",
                str(state_directory),
                "--run-id",
                "run-cli-recover",
            ]
        )
        == 0
    )
    capsys.readouterr()

    def unexpected_constructor(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("recovery must not construct a runner, model, or evaluator")

    monkeypatch.setattr(cli_module, "FixtureRunner", unexpected_constructor)
    monkeypatch.setattr(cli_module, "LocalEvaluator", unexpected_constructor)
    monkeypatch.setattr(cli_module, "ScriptedPatchModel", unexpected_constructor)

    assert (
        main(
            [
                "recover",
                "run-cli-recover",
                "--state-dir",
                str(state_directory),
            ]
        )
        == 0
    )

    response = json.loads(capsys.readouterr().out)
    assert response["run_id"] == "run-cli-recover"
    assert response["status"] == "succeeded"
    assert response["terminal_reason"] is None


def test_recover_command_absent_state_returns_stable_denial_without_creation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "missing-state"

    exit_code = main(["recover", "missing-run", "--state-dir", str(state_directory)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": "recovery_denied",
        "reason": "storage_not_recoverable",
        "run_id": "missing-run",
        "schema_version": "guildmind.recovery-denial/v1",
        "storage_state": "uninitialized",
    }
    assert not state_directory.exists()


def test_recover_command_rejects_state_directory_symlink_without_target_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target_state = tmp_path / "target-state"
    _create_inspection_state(target_state, "run-target")
    target_before = _regular_file_bytes(target_state)
    state_directory = tmp_path / "state"
    state_directory.symlink_to(target_state, target_is_directory=True)

    exit_code = main(["recover", "run-target", "--state-dir", str(state_directory)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": "recovery_denied",
        "reason": "storage_not_recoverable",
        "run_id": "run-target",
        "schema_version": "guildmind.recovery-denial/v1",
        "storage_state": "database_invalid",
    }
    assert state_directory.is_symlink()
    assert _regular_file_bytes(target_state) == target_before


def test_recover_command_unknown_run_returns_stable_denial_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / "state"
    _create_inspection_state(state_directory, "known-run")
    before = _regular_file_bytes(state_directory)

    exit_code = main(["recover", "unknown-run", "--state-dir", str(state_directory)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "error": "recovery_denied",
        "reason": "run_not_found",
        "run_id": "unknown-run",
        "schema_version": "guildmind.recovery-denial/v1",
        "storage_state": "healthy",
    }
    assert _regular_file_bytes(state_directory) == before


@pytest.mark.parametrize("command", ["replay", "report"])
def test_read_only_inspection_commands_succeed_without_mutating_storage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    state_directory = tmp_path / "state"
    _create_inspection_state(state_directory)
    before = _regular_file_bytes(state_directory)

    exit_code = main([command, "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    if command == "replay":
        assert response["run_id"] == "run-inspect"
        assert response["status"] == "pending"
    else:
        assert response["manifest"]["run_id"] == "run-inspect"
    assert response["event_count"] == 1
    assert _regular_file_bytes(state_directory) == before


@pytest.mark.parametrize("command", ["replay", "report"])
def test_read_only_inspection_missing_state_is_stable_and_creates_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    state_directory = tmp_path / "missing-state"

    exit_code = main([command, "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response == {
        "command": command,
        "error": "inspection_denied",
        "reason": "state_directory_missing",
        "run_id": "run-inspect",
        "schema_version": "guildmind.inspection-denial/v1",
    }
    assert not state_directory.exists()


@pytest.mark.parametrize("command", ["replay", "report"])
def test_read_only_inspection_missing_database_creates_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()

    exit_code = main([command, "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.inspection-denial/v1"
    assert response["reason"] == "database_missing"
    assert tuple(state_directory.iterdir()) == ()


def test_read_only_inspection_rejects_filesystem_root_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["replay", "run-inspect", "--state-dir", str(Path("/"))])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.inspection-denial/v1"
    assert response["reason"] == "filesystem_root_state_forbidden"


@pytest.mark.parametrize("command", ["replay", "report"])
@pytest.mark.parametrize("linked_path", ["state-directory", "database"])
def test_read_only_inspection_rejects_symlinks_without_mutating_targets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    linked_path: str,
) -> None:
    source_state = tmp_path / "source-state"
    source_database = _create_inspection_state(source_state)
    source_before = _regular_file_bytes(source_state)
    state_directory = tmp_path / "state"
    if linked_path == "state-directory":
        state_directory.symlink_to(source_state, target_is_directory=True)
        expected_reason = "state_directory_not_real"
    else:
        state_directory.mkdir()
        (state_directory / "runs.db").symlink_to(source_database)
        expected_reason = "database_not_real_file"

    exit_code = main([command, "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.inspection-denial/v1"
    assert response["reason"] == expected_reason
    assert state_directory.is_symlink() is (linked_path == "state-directory")
    if linked_path == "database":
        assert (state_directory / "runs.db").is_symlink()
        assert tuple(path.name for path in state_directory.iterdir()) == ("runs.db",)
    assert _regular_file_bytes(source_state) == source_before


def test_read_only_inspection_rejects_state_swap_to_link_before_sqlite_open(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    _create_inspection_state(state_directory)
    displaced_state = tmp_path / "displaced-state"
    target_state = tmp_path / "target-state"
    target_store = EventStore(target_state / "runs.db")
    try:
        target_store.create_run(
            RunManifest(
                run_id="run-inspect",
                experiment_id="experiment-cli-inspection",
                task_id="fixture-cli-inspection",
                candidate_id="scripted-solo",
                requested_model="fake-model-v1",
                seed=0,
                environment_digest=f"sha256:{'b' * 64}",
                code_revision="target-revision",
                budget_limits=BudgetLimits(max_model_calls=1, max_total_tokens=100),
                created_at=datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
            )
        )
        target_before = _regular_file_bytes(target_state)
        validate_state = cli_module._validate_inspection_state_directory
        swapped = False

        def validate_then_swap(
            path: Path,
            *,
            expected_identity: tuple[int, int] | None = None,
        ) -> tuple[int, int]:
            nonlocal swapped
            identity = validate_state(path, expected_identity=expected_identity)
            if expected_identity is None and not swapped:
                state_directory.rename(displaced_state)
                state_directory.symlink_to(target_state, target_is_directory=True)
                swapped = True
            return identity

        monkeypatch.setattr(
            cli_module,
            "_validate_inspection_state_directory",
            validate_then_swap,
        )
        monkeypatch.setattr(
            sqlite3,
            "connect",
            lambda *args, **kwargs: pytest.fail("SQLite must not open through swapped state"),
        )

        exit_code = main(["replay", "run-inspect", "--state-dir", str(state_directory)])

        response = json.loads(capsys.readouterr().out)
        assert exit_code == 1
        assert response["schema_version"] == "guildmind.inspection-denial/v1"
        assert response["reason"] == "storage_integrity_validation_failed"
        assert state_directory.is_symlink()
        assert _regular_file_bytes(target_state) == target_before
    finally:
        target_store.close()


@pytest.mark.parametrize("command", ["replay", "report"])
def test_read_only_inspection_invalid_database_returns_stable_denial(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    database = state_directory / "runs.db"
    database.write_bytes(b"not a SQLite database")
    before = _regular_file_bytes(state_directory)

    exit_code = main([command, "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.inspection-denial/v1"
    assert response["reason"] == "storage_integrity_validation_failed"
    assert _regular_file_bytes(state_directory) == before


def test_read_only_inspection_changed_database_denies_before_printing_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "state"
    database = _create_inspection_state(state_directory)
    replacement_state = tmp_path / "replacement-state"
    replacement = _create_inspection_state(replacement_state)
    displaced = tmp_path / "displaced.db"
    verify_roots = EventStore._verified_roots_locked

    def verify_then_replace(store: EventStore) -> tuple[VerifiedRunRoot, ...]:
        roots = verify_roots(store)
        database.rename(displaced)
        replacement.rename(database)
        return roots

    monkeypatch.setattr(EventStore, "_verified_roots_locked", verify_then_replace)

    exit_code = main(["replay", "run-inspect", "--state-dir", str(state_directory)])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response == {
        "command": "replay",
        "error": "inspection_denied",
        "reason": "state_changed",
        "run_id": "run-inspect",
        "schema_version": "guildmind.inspection-denial/v1",
    }


class _FakeDockerSandbox:
    reference_ready = False

    def __init__(self, *, docker_executable: str) -> None:
        self.docker_executable = docker_executable

    def assess_host(self) -> DockerHostAssessment:
        failures = () if self.reference_ready else ("rootless_required",)
        return DockerHostAssessment(
            accepted=self.reference_ready,
            reference_ready=self.reference_ready,
            failures=failures,
            warnings=(),
        )

    def verify_image(self, reference: str) -> str:
        assert "@sha256:" in reference
        return f"sha256:{'b' * 64}"


def _all_tools_available(command: str) -> str:
    return f"/usr/bin/{command}"


def test_doctor_production_fails_closed_on_a_nonreference_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("guildmind.cli.shutil.which", _all_tools_available)
    monkeypatch.setattr("guildmind.cli.DockerSandbox", _FakeDockerSandbox)

    exit_code = main(
        [
            "doctor",
            "--production",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--json",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["local_fixture_ready"] is True
    assert response["production_sandbox_ready"] is False
    assert response["checks"]["docker_reference_failures"] == ["rootless_required"]


def test_doctor_production_requires_and_runs_the_active_control_probe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _FakeDockerSandbox.reference_ready = True
    monkeypatch.setattr("guildmind.cli.shutil.which", _all_tools_available)
    monkeypatch.setattr("guildmind.cli.DockerSandbox", _FakeDockerSandbox)
    monkeypatch.setattr(
        "guildmind.cli.run_sandbox_self_test",
        lambda sandbox, *, image: SandboxSelfTestReport(
            passed=True,
            checks={"network_disabled": True},
            diagnostic=None,
            image_id=f"sha256:{'b' * 64}",
        ),
    )

    exit_code = main(
        [
            "doctor",
            "--production",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--json",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["production_sandbox_ready"] is True
    assert response["checks"]["sandbox_self_test"] == {"network_disabled": True}
    _FakeDockerSandbox.reference_ready = False


def test_resource_probe_requires_an_explicit_tier_specific_image(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE", raising=False)

    exit_code = main(["probe-resources", "--development"])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.resource-probe-error/v1"
    assert "GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE" in response["error"]


def test_containment_probe_requires_an_explicit_tier_specific_image(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE", raising=False)

    exit_code = main(["probe-containment", "--development"])

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.containment-probe-error/v1"
    assert "GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE" in response["error"]


class _FakeContainmentReport:
    all_contained = True
    reference_passed = False

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "all_contained": self.all_contained,
            "reference_eligible": False,
            "reference_passed": self.reference_passed,
            "schema_version": "guildmind.containment-probe-evidence/v1",
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


def test_containment_probe_labels_development_evidence_and_writes_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "containment-evidence.json"

    class FakeProbeSandbox:
        def __init__(self, *, host_policy: object) -> None:
            captured["host_policy"] = host_policy

    def fake_run(sandbox: object, **arguments: object) -> _FakeContainmentReport:
        captured["sandbox"] = sandbox
        captured.update(arguments)
        return _FakeContainmentReport()

    monkeypatch.setattr("guildmind.cli.DockerSandbox", FakeProbeSandbox)
    monkeypatch.setattr("guildmind.cli.run_containment_probe_suite", fake_run)
    monkeypatch.setattr("guildmind.cli._code_revision", lambda: "revision-123")

    exit_code = main(
        [
            "probe-containment",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--probe-id",
            "containment-123",
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["all_contained"] is True
    assert captured["code_revision"] == "revision-123"
    assert captured["probe_id"] == "containment-123"
    host_policy = captured["host_policy"]
    assert isinstance(host_policy, DockerHostPolicy)
    assert host_policy.mode is DockerHostMode.DEVELOPMENT_ONLY
    assert output.read_bytes() == _FakeContainmentReport().canonical_bytes() + b"\n"


def test_containment_probe_never_overwrites_existing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output = tmp_path / "containment-evidence.json"
    output.write_text("preserve", encoding="utf-8")

    monkeypatch.setattr(
        "guildmind.cli.run_containment_probe_suite",
        lambda *args, **kwargs: _FakeContainmentReport(),
    )

    exit_code = main(
        [
            "probe-containment",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.containment-probe-error/v1"
    assert output.read_text(encoding="utf-8") == "preserve"


def test_containment_probe_rejects_whitespace_id_with_machine_readable_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "probe-containment",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--probe-id",
            "   ",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.containment-probe-error/v1"
    assert "non-whitespace" in response["error"]


def test_resource_probe_rejects_whitespace_id_with_machine_readable_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "probe-resources",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--probe-id",
            "   ",
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.resource-probe-error/v1"
    assert "non-whitespace" in response["error"]


class _FakeProbeReport:
    all_enforced = True
    reference_passed = False

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "all_enforced": self.all_enforced,
            "reference_eligible": False,
            "reference_passed": self.reference_passed,
            "schema_version": "guildmind.resource-probe-evidence/v1",
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


def test_resource_probe_labels_development_evidence_and_uses_fixed_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    output = tmp_path / "resource-evidence.json"

    class FakeProbeSandbox:
        def __init__(self, *, host_policy: object) -> None:
            captured["host_policy"] = host_policy

    def fake_run(sandbox: object, **arguments: object) -> _FakeProbeReport:
        captured["sandbox"] = sandbox
        captured.update(arguments)
        return _FakeProbeReport()

    monkeypatch.setattr("guildmind.cli.DockerSandbox", FakeProbeSandbox)
    monkeypatch.setattr("guildmind.cli.run_resource_probe_suite", fake_run)
    monkeypatch.setattr("guildmind.cli._code_revision", lambda: "revision-123")

    exit_code = main(
        [
            "probe-resources",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--probe-id",
            "probe-123",
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert response["all_enforced"] is True
    assert captured["code_revision"] == "revision-123"
    assert captured["probe_id"] == "probe-123"
    host_policy = captured["host_policy"]
    assert isinstance(host_policy, DockerHostPolicy)
    assert host_policy.mode is DockerHostMode.DEVELOPMENT_ONLY
    assert output.read_bytes() == _FakeProbeReport().canonical_bytes() + b"\n"


def test_resource_probe_never_overwrites_existing_evidence(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output = tmp_path / "resource-evidence.json"
    output.write_text("preserve", encoding="utf-8")

    class FakeProbeSandbox:
        def __init__(self, *, host_policy: object) -> None:
            del host_policy

    monkeypatch.setattr("guildmind.cli.DockerSandbox", FakeProbeSandbox)
    monkeypatch.setattr(
        "guildmind.cli.run_resource_probe_suite",
        lambda *args, **kwargs: _FakeProbeReport(),
    )

    exit_code = main(
        [
            "probe-resources",
            "--development",
            "--evaluator-image",
            f"guildmind/evaluator@sha256:{'a' * 64}",
            "--output",
            str(output),
        ]
    )

    response = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert response["schema_version"] == "guildmind.resource-probe-error/v1"
    assert output.read_text(encoding="utf-8") == "preserve"
