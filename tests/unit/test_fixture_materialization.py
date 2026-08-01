from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import guildmind.runtime.fixture as fixture_module
from guildmind.evaluation import LocalEvaluationSpec, load_fixture, load_python_call_bundle
from guildmind.storage import FileArtifactStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_ENVIRONMENT_DIGEST = f"sha256:{'d' * 64}"


def test_materialization_uses_only_the_loaded_frozen_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_root = tmp_path / "baseline" / "fixture"
    mutated_root = tmp_path / "mutated" / "fixture"
    shutil.copytree(_FIXTURE, baseline_root)
    shutil.copytree(_FIXTURE, mutated_root)
    baseline_task, _, baseline_problem = fixture_module.materialize_fixture_task(
        baseline_root,
        FileArtifactStore(tmp_path / "baseline-artifacts"),
        evaluator_version="guildmind/test-evaluator-v1",
        environment_digest=_ENVIRONMENT_DIGEST,
    )

    def load_then_remove_sources(fixture_root: Path) -> LocalEvaluationSpec:
        spec = load_fixture(fixture_root)
        shutil.rmtree(spec.pristine_workspace)
        spec.pristine_workspace.write_bytes(b"corrupted workspace materialization")
        for hidden_test in spec.hidden_test_files:
            hidden_test.write_text(
                "raise RuntimeError('corrupted hidden test')\n",
                encoding="utf-8",
            )
        shutil.rmtree(fixture_root / "workspace")
        shutil.rmtree(fixture_root / "grader")
        (fixture_root / "task.json").unlink()
        return spec

    monkeypatch.setattr(fixture_module, "load_fixture", load_then_remove_sources)
    task, spec, problem = fixture_module.materialize_fixture_task(
        mutated_root,
        FileArtifactStore(tmp_path / "mutated-artifacts"),
        evaluator_version="guildmind/test-evaluator-v1",
        environment_digest=_ENVIRONMENT_DIGEST,
    )

    assert task == baseline_task
    assert problem == baseline_problem
    assert spec.pristine_workspace_sha256 == task.repository_snapshot.sha256
    assert spec.task_content_hash == task.task_content_hash
    assert spec.python_call_protocol is not None
    bundle = load_python_call_bundle(
        spec.python_call_protocol,
        expected_case_count=spec.expected_test_count,
    )
    assert spec.python_call_protocol.sealed_cases_bytes == bundle.oracle_bytes
    assert bundle.case_count == 5
