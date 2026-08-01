from datetime import UTC, datetime
from pathlib import Path

from guildmind.domain import RunStatus
from guildmind.models import ScriptedPatchModel
from guildmind.runtime import DeterministicClock
from guildmind.runtime.runner import FixtureRunner, FixtureRunResult
from guildmind.storage import EventStore, FileArtifactStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_FIXTURE = _REPOSITORY_ROOT / "fixtures" / "001-python-addition"
_START = datetime(2026, 7, 31, tzinfo=UTC)


def run_fixture(state_directory: Path, run_id: str, *, day_offset: int = 0) -> FixtureRunResult:
    clock = DeterministicClock(
        started_at=_START.replace(day=1 + day_offset),
    )
    return FixtureRunner(state_directory=state_directory, clock=clock).run(
        fixture_root=_FIXTURE,
        model=ScriptedPatchModel(_FIXTURE / "solution.patch"),
        run_id=run_id,
        code_revision="test-revision",
    )


def test_fixture_runner_emits_replayable_content_verified_evidence(tmp_path: Path) -> None:
    result = run_fixture(tmp_path / "state", "run-001")

    assert result.manifest.status is RunStatus.SUCCEEDED
    assert result.evaluation.outcome == "passed"
    assert result.replay.status is RunStatus.SUCCEEDED
    assert result.replay.evaluation_outcome == "passed"
    assert result.replay.budget_used.model_calls == 1
    assert result.replay.budget_reserved.model_calls == 0
    assert result.replay.artifacts["patch"] == result.manifest.artifacts["patch"].sha256

    artifacts = FileArtifactStore(result.artifact_root)
    for reference in result.manifest.artifacts.values():
        artifacts.verify(reference)

    with EventStore(result.database_path) as store:
        assert store.load_manifest("run-001") == result.manifest
        assert tuple(store.list_events("run-001")) == result.events


def test_fixture_runner_has_stable_semantic_digest_across_identity_and_time(tmp_path: Path) -> None:
    first = run_fixture(tmp_path / "first", "run-a")
    second = run_fixture(tmp_path / "second", "run-b", day_offset=1)

    assert first.semantic_digest == second.semantic_digest
    assert first.events != second.events
