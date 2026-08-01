import json
from pathlib import Path

import pytest

from guildmind.cli import main

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
