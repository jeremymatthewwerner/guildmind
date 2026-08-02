import json
from pathlib import Path

import pytest

from guildmind.cli import main
from guildmind.sandbox import (
    DockerHostAssessment,
    DockerHostMode,
    DockerHostPolicy,
    SandboxSelfTestReport,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"


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
