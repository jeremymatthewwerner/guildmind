import json
from pathlib import Path

import pytest

from guildmind.cli import main
from guildmind.sandbox import DockerHostAssessment, SandboxSelfTestReport

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
