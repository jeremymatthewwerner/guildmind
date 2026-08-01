import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from guildmind.domain import BudgetLimits, BudgetUsage, EventRecord, RunManifest, RunStatus
from guildmind.runtime import DeterministicClock, replay_events, semantic_digest
from guildmind.storage import EventStore, StoreIntegrityError

START = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


def pending_manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        experiment_id="experiment-0001",
        task_id="fixture-001",
        candidate_id="scripted-solo",
        requested_model="fake-model-v1",
        seed=0,
        environment_digest=f"sha256:{'a' * 64}",
        code_revision="test-revision",
        budget_limits=BudgetLimits(max_model_calls=1, max_total_tokens=100),
        created_at=START,
    )


def transition(manifest: RunManifest, **changes: object) -> RunManifest:
    return RunManifest.model_validate({**manifest.model_dump(), **changes})


def populate_run(store: EventStore, run_id: str) -> list[EventRecord]:
    pending = pending_manifest(run_id)
    store.create_run(pending)
    running = transition(pending, status=RunStatus.RUNNING, started_at=START + timedelta(seconds=1))
    store.append_event(
        run_id=run_id,
        event_type="run.started",
        payload={},
        manifest=running,
    )
    used = BudgetUsage(output_tokens=8, model_calls=1)
    store.append_event(
        run_id=run_id,
        event_type="budget.snapshot",
        payload={
            "used": used.model_dump(mode="json"),
            "reserved": BudgetUsage().model_dump(mode="json"),
        },
        budget_used=used,
        budget_reserved=BudgetUsage(),
    )
    store.append_event(
        run_id=run_id,
        event_type="artifact.recorded",
        payload={"name": "patch", "sha256": "b" * 64},
    )
    store.append_event(
        run_id=run_id,
        event_type="evaluation.completed",
        payload={"outcome": "passed"},
    )
    succeeded = transition(
        running,
        status=RunStatus.SUCCEEDED,
        finished_at=START + timedelta(seconds=2),
    )
    store.append_event(
        run_id=run_id,
        event_type="run.terminal",
        payload={"status": RunStatus.SUCCEEDED.value},
        manifest=succeeded,
    )
    return store.list_events(run_id)


def test_event_store_persists_hash_chain_manifest_budget_and_replay(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        events = populate_run(store, "run-a")

        assert [event.sequence for event in events] == list(range(6))
        assert store.load_manifest("run-a").status is RunStatus.SUCCEEDED
        used, reserved = store.load_budget_state("run-a")
        assert used.model_calls == 1
        assert reserved == BudgetUsage()
        assert store.events_jsonl("run-a").count("\n") == 6

    state = replay_events(events)
    assert state.status is RunStatus.SUCCEEDED
    assert state.budget_used.output_tokens == 8
    assert state.artifacts == {"patch": "b" * 64}
    assert state.evaluation_outcome == "passed"


def test_semantic_digest_ignores_run_identity_and_event_time(tmp_path: Path) -> None:
    first_database = tmp_path / "first.db"
    second_database = tmp_path / "second.db"
    with EventStore(first_database, clock=DeterministicClock(started_at=START)) as first:
        first_events = populate_run(first, "run-a")
    with EventStore(
        second_database,
        clock=DeterministicClock(started_at=START + timedelta(days=10)),
    ) as second:
        second_events = populate_run(second, "run-b")

    assert semantic_digest(first_events) == semantic_digest(second_events)


def test_event_store_detects_persisted_hash_tampering(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE events SET event_hash = ? WHERE run_id = ? AND sequence = 0",
            ("f" * 64, "run-a"),
        )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="hash mismatch"),
    ):
        store.list_events("run-a")
