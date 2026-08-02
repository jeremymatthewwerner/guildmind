import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EventRecord,
    RunManifest,
    RunStatus,
    canonical_sha256,
)
from guildmind.runtime import (
    DeterministicClock,
    ReplayIntegrityError,
    replay_events,
    semantic_digest,
)
from guildmind.storage import EventStore, StoreIntegrityError
from guildmind.storage.events import VerifiedRunRoot

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


def artifact(digest: str) -> ArtifactRef:
    return ArtifactRef(
        media_type="text/plain",
        size_bytes=1,
        sha256=digest,
        storage_ref=f"sha256/{digest[:2]}/{digest}",
    )


def running_run(store: EventStore, run_id: str) -> RunManifest:
    pending = pending_manifest(run_id)
    store.create_run(pending)
    return store.start_run(
        run_id,
        started_at=START + timedelta(seconds=1),
        requested_model=pending.requested_model,
    )


def populate_run(
    store: EventStore,
    run_id: str,
    *,
    evaluator_transcripts: bool = False,
) -> list[EventRecord]:
    running_run(store, run_id)
    store.record_artifact(run_id, "task_spec", artifact("a" * 64))
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    store.start_model_request(
        run_id=run_id,
        request_id="model-request-0001",
        maximum=maximum,
        budget_used=BudgetUsage(),
        budget_reserved=maximum,
    )
    used = BudgetUsage(output_tokens=8, model_calls=1)
    store.complete_model_response(
        run_id=run_id,
        request_id="model-request-0001",
        returned_model="fake-model-v1",
        actual_usage=used,
        patch=artifact("b" * 64),
        budget_used=used,
        budget_reserved=BudgetUsage(),
    )
    evaluation_artifacts = {
        "evaluation_stdout": artifact("c" * 64),
        "evaluation_stderr": artifact("d" * 64),
        "evaluation": artifact("e" * 64),
    }
    if evaluator_transcripts:
        evaluation_artifacts["evaluation_candidate_stdout"] = artifact("0" * 64)
        evaluation_artifacts["evaluation_scorer_stdout"] = artifact("1" * 64)
    store.complete_evaluation(
        run_id=run_id,
        artifacts=evaluation_artifacts,
        evaluation_payload={"outcome": "passed", "result_sha256": "f" * 64},
        status=RunStatus.SUCCEEDED,
        finished_at=START + timedelta(seconds=2),
        terminal_reason=None,
        budget_used=used,
        budget_reserved=BudgetUsage(),
    )
    return store.list_events(run_id)


def chained_event(
    events: list[EventRecord],
    event_type: str,
    payload: dict[str, JsonValue],
) -> EventRecord:
    previous = events[-1]
    sequence = previous.sequence + 1
    return EventRecord(
        event_id=f"{previous.run_id}:forged:{sequence:08d}",
        run_id=previous.run_id,
        sequence=sequence,
        causal_parent_ids=(previous.event_id,),
        event_type=event_type,
        monotonic_ns=previous.monotonic_ns + 1,
        occurred_at=previous.occurred_at + timedelta(microseconds=1),
        payload=payload,
        payload_sha256=canonical_sha256(payload),
        previous_event_hash=previous.content_hash(),
    )


def test_event_store_persists_hash_chain_manifest_budget_and_replay(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        events = populate_run(store, "run-a")

        assert [event.sequence for event in events] == list(range(14))
        assert store.load_manifest("run-a").status is RunStatus.SUCCEEDED
        used, reserved = store.load_budget_state("run-a")
        assert used.model_calls == 1
        assert reserved == BudgetUsage()
        assert store.events_jsonl("run-a").count("\n") == 14

    state = replay_events(events)
    assert state.status is RunStatus.SUCCEEDED
    assert state.budget_used.output_tokens == 8
    assert state.artifacts["patch"] == "b" * 64
    assert set(state.artifacts) == {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
        "task_spec",
    }
    assert state.evaluation_outcome == "passed"


def test_event_store_verifies_connection_settings_and_returns_stable_roots(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        run_b_events = populate_run(store, "run-b")
        run_a_event = store.create_run(pending_manifest("run-a"))

        assert store._connection.isolation_level is None
        assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000

        statements: list[str] = []
        store._connection.set_trace_callback(statements.append)
        changes_before = store._connection.total_changes
        roots = store.verify_integrity()
        store._connection.set_trace_callback(None)

        assert roots == store.verify_integrity()
        assert store._connection.total_changes == changes_before

    assert tuple(root.manifest.run_id for root in roots) == ("run-a", "run-b")
    assert all(isinstance(root, VerifiedRunRoot) for root in roots)
    assert roots[0].manifest_revision == 0
    assert roots[0].manifest_sha256 == canonical_sha256(roots[0].manifest)
    assert roots[0].event_count == 1
    assert roots[0].head_event_sha256 == run_a_event.content_hash()
    assert roots[1].manifest_revision == 4
    assert roots[1].manifest_sha256 == canonical_sha256(roots[1].manifest)
    assert roots[1].event_count == len(run_b_events)
    assert roots[1].head_event_sha256 == run_b_events[-1].content_hash()
    assert "PRAGMA quick_check" in statements
    assert "PRAGMA foreign_key_check" in statements


def test_explicit_transaction_policy_commits_and_rolls_back_lifecycle_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        append_event = store._append_locked

        def fail_creation_event(**_: object) -> EventRecord:
            raise RuntimeError("simulated lifecycle failure")

        monkeypatch.setattr(store, "_append_locked", fail_creation_event)
        with pytest.raises(RuntimeError, match="simulated lifecycle failure"):
            store.create_run(pending_manifest("run-rolled-back"))
        assert store.list_run_ids() == ()

        monkeypatch.setattr(store, "_append_locked", append_event)
        committed_event = store.create_run(pending_manifest("run-committed"))
        assert committed_event.event_type == "run.created"

    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        assert store.list_run_ids() == ("run-committed",)
        assert store.load_manifest("run-committed") == pending_manifest("run-committed")
        assert store.list_events("run-committed") == [committed_event]


def test_verify_integrity_returns_an_empty_snapshot_for_an_empty_database(
    tmp_path: Path,
) -> None:
    with EventStore(tmp_path / "runs.db") as store:
        assert store.verify_integrity() == ()


def test_verify_integrity_is_read_only_for_outstanding_external_work(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )
        events_before = store.list_events("run-a")
        manifest_before = store.load_manifest("run-a")
        budget_before = store.load_budget_state("run-a")
        changes_before = store._connection.total_changes

        roots = store.verify_integrity()

        assert roots[0].manifest == manifest_before
        assert roots[0].manifest.status is RunStatus.RUNNING
        assert store.list_events("run-a") == events_before
        assert store.load_manifest("run-a") == manifest_before
        assert store.load_budget_state("run-a") == budget_before
        assert store._connection.total_changes == changes_before
        assert "model.request_ambiguous" not in {
            event.event_type for event in store.list_events("run-a")
        }
        assert "run.terminal" not in {event.event_type for event in store.list_events("run-a")}


@pytest.mark.parametrize(
    ("pragma", "value"),
    [
        ("foreign_keys", "OFF"),
        ("journal_mode", "DELETE"),
        ("synchronous", "NORMAL"),
        ("busy_timeout", "1"),
    ],
)
def test_verify_integrity_rejects_connection_setting_drift(
    tmp_path: Path,
    pragma: str,
    value: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store._connection.execute(f"PRAGMA {pragma} = {value}")

        with pytest.raises(StoreIntegrityError, match=pragma):
            store.verify_integrity()


def test_event_store_orders_and_replays_optional_evaluator_transcripts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        events = populate_run(store, "run-a", evaluator_transcripts=True)
        manifest = store.load_manifest("run-a")

    recorded = [event for event in events if event.event_type == "artifact.recorded"]
    assert [event.payload["name"] for event in recorded] == [
        "task_spec",
        "patch",
        "evaluation_stdout",
        "evaluation_stderr",
        "evaluation_candidate_stdout",
        "evaluation_scorer_stdout",
        "evaluation",
    ]
    replay = replay_events(events, require_terminal=True)
    assert replay.artifacts["evaluation_candidate_stdout"] == "0" * 64
    assert replay.artifacts["evaluation_scorer_stdout"] == "1" * 64
    assert set(manifest.artifacts) == set(replay.artifacts)

    stderr_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "artifact.recorded"
        and event.payload.get("name") == "evaluation_stderr"
    )
    through_stderr = events[: stderr_index + 1]
    scorer_event = next(
        event for event in recorded if event.payload.get("name") == "evaluation_scorer_stdout"
    )
    scorer_first = chained_event(
        through_stderr,
        "artifact.recorded",
        dict(scorer_event.payload),
    )
    with pytest.raises(ReplayIntegrityError, match="out of phase"):
        replay_events([*through_stderr, scorer_first])


def test_event_store_rejects_uncoordinated_low_level_phase_events(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))

        with pytest.raises(ValueError, match="append is disabled"):
            store.append_event(
                run_id="run-a",
                event_type="artifact.recorded",
                payload={"name": "patch", "sha256": "f" * 64},
            )
        with pytest.raises(ValueError, match="append is disabled"):
            store.append_event(
                run_id="run-a",
                event_type="run.started",
                payload={"requested_model": "different-model"},
            )

        assert len(store.list_events("run-a")) == 1

        store.start_run(
            "run-a",
            started_at=START + timedelta(seconds=1),
            requested_model="fake-model-v1",
        )
        with pytest.raises(ValueError, match="initial task_spec"):
            store.record_artifact("run-a", "patch", artifact("b" * 64))
        with pytest.raises(StoreIntegrityError, match="task_spec artifact"):
            store.start_model_request(
                run_id="run-a",
                request_id="model-request-0001",
                maximum=BudgetUsage(model_calls=1),
                budget_used=BudgetUsage(),
                budget_reserved=BudgetUsage(model_calls=1),
            )
        assert len(store.list_events("run-a")) == 2
        assert store.load_budget_state("run-a") == (BudgetUsage(), BudgetUsage())


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


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": 1},
        {"code_revision": "different-revision"},
        {"environment_digest": f"sha256:{'b' * 64}"},
        {"model_parameters": {"temperature": 0.25}},
        {"genome_hash": "c" * 64},
        {"budget_limits": BudgetLimits(max_model_calls=1, max_total_tokens=99)},
    ],
)
def test_semantic_digest_changes_with_immutable_treatment_identity(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    original = pending_manifest("run-a")
    changed = RunManifest.model_validate({**pending_manifest("run-b").model_dump(), **changes})
    with EventStore(
        tmp_path / "first.db",
        clock=DeterministicClock(started_at=START),
    ) as first:
        first_event = first.create_run(original)
    with EventStore(
        tmp_path / "second.db",
        clock=DeterministicClock(started_at=START + timedelta(days=1)),
    ) as second:
        second_event = second.create_run(changed)

    assert semantic_digest([first_event]) != semantic_digest([second_event])


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


def test_event_store_detects_duplicate_index_tampering(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (RunStatus.RUNNING.value, "run-a"),
        )
        connection.commit()
    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="status index mismatch"),
    ):
        store.load_manifest("run-a")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (RunStatus.PENDING.value, "run-a"),
        )
        connection.execute(
            "UPDATE run_manifests SET manifest_sha256 = ? WHERE run_id = ? AND revision = 0",
            ("f" * 64, "run-a"),
        )
        connection.commit()
    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="revision mismatch"),
    ):
        store.load_manifest("run-a")


def test_event_store_detects_budget_limit_index_tampering(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE budget_state SET limits_json = ? WHERE run_id = ?",
            (BudgetLimits(max_model_calls=2, max_total_tokens=100).model_dump_json(), "run-a"),
        )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="budget limits index mismatch"),
    ):
        store.load_budget_state("run-a")


def test_event_store_detects_budget_usage_index_tampering(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        populate_run(store, "run-a")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE budget_state SET used_json = ? WHERE run_id = ?",
            (BudgetUsage(output_tokens=9, model_calls=1).model_dump_json(), "run-a"),
        )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="budget index disagrees"),
    ):
        store.list_events("run-a")


@pytest.mark.parametrize(
    ("statement", "parameters", "message"),
    [
        (
            "UPDATE runs SET status = ? WHERE run_id = ?",
            (RunStatus.RUNNING.value, "run-a"),
            "status index mismatch",
        ),
        (
            "UPDATE events SET event_hash = ? WHERE run_id = ? AND sequence = 0",
            ("0" * 64, "run-a"),
            "event hash mismatch",
        ),
        (
            "UPDATE budget_state SET used_json = ? WHERE run_id = ?",
            (BudgetUsage(output_tokens=9, model_calls=1).model_dump_json(), "run-a"),
            "budget index disagrees",
        ),
    ],
    ids=("run-index", "event-chain", "budget-index"),
)
def test_verify_integrity_rejects_corrupt_current_run_state(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        populate_run(store, "run-a")
    with sqlite3.connect(database) as connection:
        connection.execute(statement, parameters)
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match=message),
    ):
        store.verify_integrity()


def test_verify_integrity_rejects_foreign_key_violations(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO budget_state(run_id, limits_json, used_json, reserved_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                "orphan-run",
                BudgetLimits(max_model_calls=1).model_dump_json(),
                BudgetUsage().model_dump_json(),
                BudgetUsage().model_dump_json(),
            ),
        )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match="foreign_key_check"),
    ):
        store.verify_integrity()


@pytest.mark.parametrize(
    ("statement", "parameters", "message"),
    [
        (
            "DELETE FROM run_manifests WHERE run_id = ? AND revision = 1",
            ("run-a",),
            "non-contiguous manifest history",
        ),
        (
            """
            UPDATE run_manifests SET manifest_sha256 = ?
            WHERE run_id = ? AND revision = 0
            """,
            ("0" * 64, "run-a"),
            "manifest history hash mismatch",
        ),
    ],
    ids=("missing-revision", "historical-hash"),
)
def test_verify_integrity_validates_the_complete_manifest_history(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        populate_run(store, "run-a")
    with sqlite3.connect(database) as connection:
        connection.execute(statement, parameters)
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match=message),
    ):
        store.verify_integrity()


def test_explicit_recovery_charges_ambiguous_request_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )

    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        recovered = store.recover_run(
            "run-a",
            finished_at=START + timedelta(seconds=2),
        )
        events_after_first_recovery = store.list_events("run-a")
        recovered_again = store.recover_run(
            "run-a",
            finished_at=START + timedelta(seconds=3),
        )
        used, reserved = store.load_budget_state("run-a")

        assert recovered_again == recovered
        assert store.list_events("run-a") == events_after_first_recovery

    state = replay_events(events_after_first_recovery, require_terminal=True)
    assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.terminal_reason == "ambiguous_model_request"
    assert state.model_request_state == "ambiguous"
    assert used == maximum
    assert reserved == BudgetUsage()
    assert set(state.absent_artifacts) == {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
    }


def test_model_lifecycle_requires_one_ledgered_call(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        events_before = store.list_events("run-a")

        with pytest.raises(StoreIntegrityError, match="exactly one model call"):
            store.start_model_request(
                run_id="run-a",
                request_id="model-request-0001",
                maximum=BudgetUsage(output_tokens=20),
                budget_used=BudgetUsage(),
                budget_reserved=BudgetUsage(output_tokens=20),
            )
        assert store.list_events("run-a") == events_before
        assert store.load_budget_state("run-a") == (BudgetUsage(), BudgetUsage())

        maximum = BudgetUsage(output_tokens=20, model_calls=1)
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )
        response_events_before = store.list_events("run-a")
        with pytest.raises(StoreIntegrityError, match="exactly one model call"):
            store.complete_model_response(
                run_id="run-a",
                request_id="model-request-0001",
                returned_model="fake-model-v1",
                actual_usage=BudgetUsage(output_tokens=8),
                patch=artifact("b" * 64),
                budget_used=BudgetUsage(output_tokens=8),
                budget_reserved=BudgetUsage(),
            )
        assert store.list_events("run-a") == response_events_before
        assert store.load_budget_state("run-a") == (BudgetUsage(), maximum)


def test_recovery_preserves_completed_response_usage_and_patch(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    actual = BudgetUsage(output_tokens=8, model_calls=1)
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )
        store.complete_model_response(
            run_id="run-a",
            request_id="model-request-0001",
            returned_model="fake-model-v1",
            actual_usage=actual,
            patch=artifact("b" * 64),
            budget_used=actual,
            budget_reserved=BudgetUsage(),
        )

    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        recovered = store.recover_run(
            "run-a",
            finished_at=START + timedelta(seconds=2),
        )
        events = store.list_events("run-a")
        used, reserved = store.load_budget_state("run-a")

    state = replay_events(events, require_terminal=True)
    assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.terminal_reason == "interrupted_run_recovered"
    assert recovered.returned_model == "fake-model-v1"
    assert set(recovered.artifacts) == {"patch", "task_spec"}
    assert state.model_request_state == "completed"
    assert set(state.absent_artifacts) == {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
    }
    assert used == actual
    assert reserved == BudgetUsage()


def test_replay_rejects_events_after_terminal_rebinding_decreasing_budget_and_second_evaluation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        events = populate_run(store, "run-a")

    after_terminal = chained_event(
        events,
        "artifact.not_produced",
        {"name": "late", "reason": "late"},
    )
    with pytest.raises(ReplayIntegrityError, match="after the terminal"):
        replay_events([*events, after_terminal])

    patch_index = next(
        index
        for index, event in enumerate(events)
        if event.event_type == "artifact.recorded" and event.payload.get("name") == "patch"
    )
    through_patch = events[: patch_index + 1]
    rebound = chained_event(
        through_patch,
        "artifact.recorded",
        {"name": "patch", "sha256": "f" * 64},
    )
    with pytest.raises(ReplayIntegrityError, match="already bound"):
        replay_events([*through_patch, rebound])

    decreased = chained_event(
        through_patch,
        "budget.snapshot",
        {
            "used": BudgetUsage(output_tokens=7, model_calls=1).model_dump(mode="json"),
            "reserved": BudgetUsage().model_dump(mode="json"),
        },
    )
    with pytest.raises(ReplayIntegrityError, match="usage decreased"):
        replay_events([*through_patch, decreased])

    increased = chained_event(
        through_patch,
        "budget.snapshot",
        {
            "used": BudgetUsage(output_tokens=9, model_calls=1).model_dump(mode="json"),
            "reserved": BudgetUsage().model_dump(mode="json"),
        },
    )
    with pytest.raises(ReplayIntegrityError, match="changed without a lifecycle event"):
        replay_events([*through_patch, increased])

    evaluation_index = next(
        index for index, event in enumerate(events) if event.event_type == "evaluation.completed"
    )
    through_evaluation = events[: evaluation_index + 1]
    duplicate_evaluation = chained_event(
        through_evaluation,
        "evaluation.completed",
        {"outcome": "passed"},
    )
    with pytest.raises(ReplayIntegrityError, match="already been recorded"):
        replay_events([*through_evaluation, duplicate_evaluation])

    mismatched_terminal = chained_event(
        events[:-1],
        "run.terminal",
        {
            "evaluation_outcome": "passed",
            "status": RunStatus.FAILED.value,
            "terminal_reason": "tests_failed",
        },
    )
    with pytest.raises(ReplayIntegrityError, match="status disagrees"):
        replay_events([*events[:-1], mismatched_terminal])


def test_model_response_phase_rolls_back_every_binding_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    actual = BudgetUsage(output_tokens=8, model_calls=1)
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )
        events_before = store.list_events("run-a")
        save_manifest = store._save_manifest_locked

        def fail_save_manifest(run_id: str, manifest: RunManifest) -> None:
            raise RuntimeError("simulated commit-phase failure")

        monkeypatch.setattr(store, "_save_manifest_locked", fail_save_manifest)
        with pytest.raises(RuntimeError, match="simulated commit-phase failure"):
            store.complete_model_response(
                run_id="run-a",
                request_id="model-request-0001",
                returned_model="fake-model-v1",
                actual_usage=actual,
                patch=artifact("b" * 64),
                budget_used=actual,
                budget_reserved=BudgetUsage(),
            )
        monkeypatch.setattr(store, "_save_manifest_locked", save_manifest)

        assert store.list_events("run-a") == events_before
        assert store.load_manifest("run-a").returned_model is None
        assert set(store.load_manifest("run-a").artifacts) == {"task_spec"}
        assert store.load_budget_state("run-a") == (BudgetUsage(), maximum)


def test_terminal_evaluation_phase_rolls_back_every_binding_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    maximum = BudgetUsage(output_tokens=20, model_calls=1)
    actual = BudgetUsage(output_tokens=8, model_calls=1)
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        store.record_artifact("run-a", "task_spec", artifact("a" * 64))
        store.start_model_request(
            run_id="run-a",
            request_id="model-request-0001",
            maximum=maximum,
            budget_used=BudgetUsage(),
            budget_reserved=maximum,
        )
        store.complete_model_response(
            run_id="run-a",
            request_id="model-request-0001",
            returned_model="fake-model-v1",
            actual_usage=actual,
            patch=artifact("b" * 64),
            budget_used=actual,
            budget_reserved=BudgetUsage(),
        )
        events_before = store.list_events("run-a")
        save_manifest = store._save_manifest_locked

        def fail_save_manifest(run_id: str, manifest: RunManifest) -> None:
            raise RuntimeError("simulated terminal commit failure")

        monkeypatch.setattr(store, "_save_manifest_locked", fail_save_manifest)
        with pytest.raises(RuntimeError, match="simulated terminal commit failure"):
            store.complete_evaluation(
                run_id="run-a",
                artifacts={
                    "evaluation_stdout": artifact("c" * 64),
                    "evaluation_stderr": artifact("d" * 64),
                    "evaluation": artifact("e" * 64),
                },
                evaluation_payload={"outcome": "passed", "result_sha256": "f" * 64},
                status=RunStatus.SUCCEEDED,
                finished_at=START + timedelta(seconds=2),
                terminal_reason=None,
                budget_used=actual,
                budget_reserved=BudgetUsage(),
            )
        monkeypatch.setattr(store, "_save_manifest_locked", save_manifest)

        manifest = store.load_manifest("run-a")
        assert store.list_events("run-a") == events_before
        assert manifest.status is RunStatus.RUNNING
        assert set(manifest.artifacts) == {"patch", "task_spec"}
        assert store.load_budget_state("run-a") == (actual, BudgetUsage())
