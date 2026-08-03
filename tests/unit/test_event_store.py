import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

import guildmind.storage.events as event_store_module
from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EventRecord,
    RunManifest,
    RunStatus,
    canonical_json,
    canonical_sha256,
)
from guildmind.runtime import (
    DeterministicClock,
    ReplayIntegrityError,
    replay_events,
    semantic_digest,
)
from guildmind.storage import EventStore, StoreIntegrityError
from guildmind.storage.events import (
    _SCHEMA_INITIALIZATION_SQL,
    VerifiedRunRoot,
    verified_run_roots_sha256,
)

START = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)

_MALICIOUS_AFTER_INSERT_TRIGGER_SQL = f"""
CREATE TRIGGER corrupt_other_run_after_event
AFTER INSERT ON events
WHEN NEW.run_id = 'run-a'
BEGIN
    UPDATE runs
    SET manifest_sha256 = '{"0" * 64}'
    WHERE run_id = 'run-b';
END;
"""


def altered_schema(old: str | None, new: str = "", *, extra: str = "") -> str:
    schema = _SCHEMA_INITIALIZATION_SQL
    if old is not None:
        assert schema.count(old) >= 1
        schema = schema.replace(old, new, 1)
    return f"{schema}\n{extra}"


_ALTERED_EXISTING_SCHEMAS = (
    (
        "column-order",
        altered_schema(
            "status TEXT NOT NULL,\n                manifest_revision INTEGER NOT NULL,",
            "manifest_revision INTEGER NOT NULL,\n                status TEXT NOT NULL,",
        ),
    ),
    ("column-type", altered_schema("status TEXT NOT NULL", "status BLOB NOT NULL")),
    ("not-null", altered_schema("status TEXT NOT NULL", "status TEXT")),
    ("primary-key", altered_schema("PRIMARY KEY(run_id, sequence)", "PRIMARY KEY(sequence)")),
    (
        "foreign-key-target",
        altered_schema("REFERENCES runs(run_id)", "REFERENCES budget_state(run_id)"),
    ),
    (
        "foreign-key-action",
        altered_schema(
            "REFERENCES runs(run_id)",
            "REFERENCES runs(run_id) ON DELETE CASCADE",
        ),
    ),
    ("missing-unique", altered_schema("event_id TEXT NOT NULL UNIQUE", "event_id TEXT NOT NULL")),
    (
        "extra-table",
        altered_schema(None, extra="CREATE TABLE unexpected(value TEXT);"),
    ),
    (
        "extra-view",
        altered_schema(None, extra="CREATE VIEW unexpected AS SELECT run_id FROM runs;"),
    ),
    (
        "extra-index",
        altered_schema(None, extra="CREATE INDEX unexpected ON runs(status);"),
    ),
    (
        "malicious-trigger",
        altered_schema(None, extra=_MALICIOUS_AFTER_INSERT_TRIGGER_SQL),
    ),
    (
        "check-constraint",
        altered_schema(
            "status TEXT NOT NULL",
            "status TEXT NOT NULL CHECK(length(status) > 0)",
        ),
    ),
    (
        "conflict-policy",
        altered_schema(
            "event_id TEXT NOT NULL UNIQUE",
            "event_id TEXT NOT NULL UNIQUE ON CONFLICT REPLACE",
        ),
    ),
    (
        "deferred-foreign-key",
        altered_schema(
            "REFERENCES runs(run_id)",
            "REFERENCES runs(run_id) DEFERRABLE INITIALLY DEFERRED",
        ),
    ),
)


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


def directory_file_bytes(directory: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes()
        for entry in sorted(directory.iterdir())
        if entry.is_file() and not entry.is_symlink()
    }


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
    expected_snapshot_sha256 = canonical_sha256(
        {
            "roots": [
                {
                    "event_count": root.event_count,
                    "head_event_sha256": root.head_event_sha256,
                    "manifest_revision": root.manifest_revision,
                    "manifest_sha256": root.manifest_sha256,
                    "run_id": root.manifest.run_id,
                }
                for root in roots
            ],
            "schema_version": "guildmind.verified-run-root-snapshot/v1",
        }
    )
    assert verified_run_roots_sha256(roots) == expected_snapshot_sha256
    assert verified_run_roots_sha256(tuple(reversed(roots))) == expected_snapshot_sha256
    with pytest.raises(ValueError, match="duplicate verified run root: run-a"):
        verified_run_roots_sha256((roots[0], roots[0]))


def test_existing_read_only_store_verifies_without_filesystem_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs ?#%.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        event = writer.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    if wal.exists():
        assert wal.stat().st_size == 0
        wal.unlink()
    if shm.exists():
        shm.unlink()
    before = directory_file_bytes(tmp_path)

    with EventStore.open_existing_read_only(
        database,
        clock=DeterministicClock(started_at=START),
    ) as reader:
        assert reader._connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert reader._connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        roots = reader.verify_integrity()
        assert roots[0].manifest.run_id == "run-a"
        assert roots[0].head_event_sha256 == event.content_hash()
        with reader.verified_snapshot() as held_roots:
            assert held_roots == roots
            assert reader._connection.in_transaction
        assert not reader._connection.in_transaction
        with pytest.raises(StoreIntegrityError, match="read-only"):
            reader.create_run(pending_manifest("forbidden"))

    assert directory_file_bytes(tmp_path) == before
    assert not wal.exists()
    assert not shm.exists()


def test_existing_read_only_store_reads_committed_nonempty_wal_without_new_entries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    writer = EventStore(database, clock=DeterministicClock(started_at=START))
    try:
        event = writer.create_run(pending_manifest("run-in-wal"))
        wal = Path(f"{database}-wal")
        shm = Path(f"{database}-shm")
        assert wal.stat().st_size > 0
        assert shm.stat().st_size > 0
        before = directory_file_bytes(tmp_path)

        with EventStore.open_existing_read_only(database) as reader:
            assert reader._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            roots = reader.verify_integrity()

        assert roots[0].manifest.run_id == "run-in-wal"
        assert roots[0].head_event_sha256 == event.content_hash()
        after = directory_file_bytes(tmp_path)
        assert set(after) == set(before)
        assert after[database.name] == before[database.name]
        assert after[wal.name] == before[wal.name]
        # SQLite readers may update lock metadata in an existing SHM file.
        assert {name for name in before if before[name] != after[name]} <= {shm.name}
        assert after[shm.name]
    finally:
        writer.close()


def test_existing_read_only_store_fails_closed_for_wal_without_shm(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    writer = EventStore(source, clock=DeterministicClock(started_at=START))
    try:
        writer.create_run(pending_manifest("run-in-wal"))
        source_wal = Path(f"{source}-wal")
        target.write_bytes(source.read_bytes())
        target_wal = Path(f"{target}-wal")
        target_wal.write_bytes(source_wal.read_bytes())
        target_shm = Path(f"{target}-shm")

        with pytest.raises(StoreIntegrityError, match="WAL requires an existing usable SHM"):
            EventStore.open_existing_read_only(target)

        assert target_wal.read_bytes() == source_wal.read_bytes()
        assert not target_shm.exists()
    finally:
        writer.close()


def test_existing_read_only_store_fails_closed_for_shm_without_wal(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    if wal.exists():
        assert wal.stat().st_size == 0
        wal.unlink()
    shm.write_bytes(b"stale-shm")

    with pytest.raises(StoreIntegrityError, match="SHM sidecar has no matching WAL"):
        EventStore.open_existing_read_only(database)

    assert not wal.exists()
    assert shm.read_bytes() == b"stale-shm"


def test_existing_read_only_store_rejects_missing_leaf_without_creating_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"

    with pytest.raises(StoreIntegrityError, match="does not exist"):
        EventStore.open_existing_read_only(database)

    assert not database.exists()


def test_existing_read_only_store_rejects_missing_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "runs.db"

    with pytest.raises(StoreIntegrityError, match="trusted base is unavailable"):
        EventStore.open_existing_read_only(database)

    assert not database.exists()
    assert not database.parent.exists()


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("zero", "must not be empty"),
        ("directory", "regular file"),
        ("symlink", "must not be a symlink"),
    ],
)
def test_existing_read_only_store_rejects_unsafe_leaf_types(
    tmp_path: Path,
    kind: str,
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    target = tmp_path / "target.db"
    if kind == "zero":
        database.touch()
    elif kind == "directory":
        database.mkdir()
    else:
        with EventStore(target) as writer:
            writer.create_run(pending_manifest("target-run"))
        database.symlink_to(target)
    target_before = target.read_bytes() if target.exists() else None

    with pytest.raises(StoreIntegrityError, match=message):
        EventStore.open_existing_read_only(database)

    if kind == "zero":
        assert database.read_bytes() == b""
    elif kind == "directory":
        assert database.is_dir()
    else:
        assert database.is_symlink()
        assert target.read_bytes() == target_before


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_existing_read_only_store_rejects_fifo_before_connect(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    os.mkfifo(database)

    with pytest.raises(StoreIntegrityError, match="regular file"):
        EventStore.open_existing_read_only(database)


@pytest.mark.parametrize("kind", ["invalid-bytes", "wrong-schema"])
def test_existing_read_only_store_rejects_invalid_database(
    tmp_path: Path,
    kind: str,
) -> None:
    database = tmp_path / "runs.db"
    if kind == "invalid-bytes":
        database.write_bytes(b"not a sqlite database")
    else:
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = database.read_bytes()

    with pytest.raises(StoreIntegrityError):
        EventStore.open_existing_read_only(database)

    assert database.read_bytes() == before


def test_existing_read_only_store_accepts_trusted_parent_alias_but_not_controlled_one(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)
    database = alias_parent / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))

    with EventStore.open_existing_read_only(database) as reader:
        assert tuple(root.manifest.run_id for root in reader.verify_integrity()) == ("run-a",)
    with pytest.raises(StoreIntegrityError, match="real directories"):
        EventStore.open_existing_read_only(database, trusted_base=tmp_path)


def test_existing_read_only_store_detects_leaf_replacement_after_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    replacement = tmp_path / "replacement.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("original"))
    with EventStore(replacement) as writer:
        writer.create_run(pending_manifest("replacement"))
    configure = EventStore._configure_read_only_connection
    backup = tmp_path / "original.db"

    def configure_then_replace(reader: EventStore) -> None:
        configure(reader)
        database.rename(backup)
        replacement.rename(database)

    monkeypatch.setattr(EventStore, "_configure_read_only_connection", configure_then_replace)

    with pytest.raises(StoreIntegrityError, match="changed while it was being opened"):
        EventStore.open_existing_read_only(database)

    assert database.exists()
    assert backup.exists()


def test_existing_writable_store_performs_snapshot_guarded_recovery(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        running_run(writer, "run-a")
    with (
        EventStore.open_existing_read_only(database, trusted_base=tmp_path) as reader,
        reader.verified_snapshot() as roots,
    ):
        expected_snapshot_sha256 = verified_run_roots_sha256(roots)

    with EventStore.open_existing_writable(
        database,
        clock=DeterministicClock(started_at=START),
        trusted_base=tmp_path,
    ) as recovery_store:
        assert recovery_store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        recovered = recovery_store.recover_run(
            "run-a",
            finished_at=START + timedelta(seconds=2),
            expected_snapshot_sha256=expected_snapshot_sha256,
        )

    assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.terminal_reason == "interrupted_run_recovered"


def test_existing_writable_store_never_creates_missing_or_empty_database(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "runs.db"
    with pytest.raises(StoreIntegrityError, match="trusted base is unavailable"):
        EventStore.open_existing_writable(missing)
    assert not missing.parent.exists()

    empty = tmp_path / "empty.db"
    empty.touch()
    with pytest.raises(StoreIntegrityError, match="must not be empty"):
        EventStore.open_existing_writable(empty)
    assert empty.read_bytes() == b""


def test_create_or_open_constructor_rejects_existing_database_leaf_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.db"
    database = tmp_path / "runs.db"
    with EventStore(target) as writer:
        writer.create_run(pending_manifest("target-run"))
    target_before = target.read_bytes()
    database.symlink_to(target)

    with pytest.raises(StoreIntegrityError, match="must not be a symlink"):
        EventStore(database)

    assert database.is_symlink()
    assert target.read_bytes() == target_before


def test_create_or_open_constructor_syncs_exclusively_created_directory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    synced: list[Path] = []
    real_sync = event_store_module._fsync_directory

    def record_sync(path: Path) -> None:
        synced.append(path)
        real_sync(path)

    monkeypatch.setattr(event_store_module, "_fsync_directory", record_sync)

    with EventStore(database):
        pass

    assert synced == [tmp_path]


@pytest.mark.parametrize(
    ("sidecar_contents", "message"),
    [
        ({"-wal": b"existing-wal", "-shm": b"existing-shm"}, "pre-existing SQLite sidecars"),
        ({"-journal": b"existing-journal"}, "rollback-journal recovery"),
    ],
    ids=("matching-wal-shm", "rollback-journal"),
)
def test_create_or_open_constructor_does_not_create_main_database_over_sidecars(
    tmp_path: Path,
    sidecar_contents: dict[str, bytes],
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    sidecars = {
        Path(f"{database}{suffix}"): contents for suffix, contents in sidecar_contents.items()
    }
    for sidecar, contents in sidecars.items():
        sidecar.write_bytes(contents)

    with pytest.raises(StoreIntegrityError, match=message):
        EventStore(database)

    assert not database.exists()
    assert {sidecar: sidecar.read_bytes() for sidecar in sidecars} == sidecars


def test_create_or_open_constructor_rejects_preexisting_extra_trigger(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as attacker:
        attacker.execute(_MALICIOUS_AFTER_INSERT_TRIGGER_SQL)

    with pytest.raises(StoreIntegrityError, match="schema is invalid"):
        EventStore(database)

    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchone()[0]
            == 1
        )


def test_create_or_open_transaction_rejects_corrupt_unrelated_run_before_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        writer.create_run(pending_manifest("run-a"))
        writer.create_run(pending_manifest("run-b"))
    with sqlite3.connect(database) as attacker:
        attacker.execute(
            "UPDATE events SET event_hash = ? WHERE run_id = ? AND sequence = 0",
            ("f" * 64, "run-b"),
        )
    with sqlite3.connect(database) as connection:
        rows_before = tuple(
            connection.execute(
                "SELECT run_id, status, manifest_revision FROM runs ORDER BY run_id"
            ).fetchall()
        )

    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        changes_before = store._connection.total_changes
        with pytest.raises(StoreIntegrityError, match="event hash mismatch at sequence 0"):
            store.start_run(
                "run-a",
                started_at=START + timedelta(seconds=1),
                requested_model="fake-model-v1",
            )
        assert store._connection.total_changes == changes_before

    with sqlite3.connect(database) as connection:
        rows_after = tuple(
            connection.execute(
                "SELECT run_id, status, manifest_revision FROM runs ORDER BY run_id"
            ).fetchall()
        )
    assert rows_after == rows_before


def test_create_or_open_transaction_rolls_back_staged_cross_run_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        writer.create_run(pending_manifest("run-a"))
        writer.create_run(pending_manifest("run-b"))

    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        roots_before = store.verify_integrity()
        events_before = {run_id: store.list_events(run_id) for run_id in ("run-a", "run-b")}
        append_event = store._append_locked

        def append_then_corrupt_other_run(
            *,
            run_id: str,
            event_type: str,
            payload: dict[str, JsonValue],
            causal_parent_ids: Sequence[str] | None = None,
        ) -> EventRecord:
            event = append_event(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                causal_parent_ids=causal_parent_ids,
            )
            store._connection.execute(
                "UPDATE events SET event_hash = ? WHERE run_id = ? AND sequence = 0",
                ("f" * 64, "run-b"),
            )
            return event

        monkeypatch.setattr(store, "_append_locked", append_then_corrupt_other_run)

        with pytest.raises(StoreIntegrityError, match="event hash mismatch at sequence 0"):
            store.start_run(
                "run-a",
                started_at=START + timedelta(seconds=1),
                requested_model="fake-model-v1",
            )

        assert store.verify_integrity() == roots_before
        assert {run_id: store.list_events(run_id) for run_id in events_before} == events_before
        assert store.load_manifest("run-a").status is RunStatus.PENDING
        assert store.load_manifest("run-b").status is RunStatus.PENDING


def test_existing_writable_store_rejects_wrong_schema_without_initializing_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(StoreIntegrityError, match="schema is invalid"):
        EventStore.open_existing_writable(database)

    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"unrelated"}


@pytest.mark.parametrize("opening", ["read-only", "writable"])
def test_existing_store_rejects_non_wal_without_converting_it(
    tmp_path: Path,
    opening: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    before = database.read_bytes()

    with pytest.raises(StoreIntegrityError, match="journal_mode"):
        if opening == "read-only":
            EventStore.open_existing_read_only(database)
        else:
            EventStore.open_existing_writable(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert database.read_bytes() == before


@pytest.mark.parametrize("opening", ["read-only", "writable"])
@pytest.mark.parametrize(
    ("schema_name", "schema_sql"),
    _ALTERED_EXISTING_SCHEMAS,
    ids=tuple(name for name, _ in _ALTERED_EXISTING_SCHEMAS),
)
def test_existing_store_requires_exact_schema_without_mutating_it(
    tmp_path: Path,
    opening: str,
    schema_name: str,
    schema_sql: str,
) -> None:
    del schema_name
    database = tmp_path / "runs.db"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.executescript(schema_sql)
        before = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            ).fetchall()
        )

    with pytest.raises(StoreIntegrityError, match="schema is invalid"):
        if opening == "read-only":
            EventStore.open_existing_read_only(database)
        else:
            EventStore.open_existing_writable(database)

    with sqlite3.connect(database) as connection:
        after = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            ).fetchall()
        )
    assert after == before


@pytest.mark.parametrize("opening", ["read-only", "writable"])
def test_existing_store_rejects_hard_linked_main_database(
    tmp_path: Path,
    opening: str,
) -> None:
    database = tmp_path / "runs.db"
    alias = tmp_path / "database-alias.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    os.link(database, alias)

    with pytest.raises(StoreIntegrityError, match="hard linked"):
        if opening == "read-only":
            EventStore.open_existing_read_only(database)
        else:
            EventStore.open_existing_writable(database)

    assert database.stat().st_ino == alias.stat().st_ino
    assert database.stat().st_nlink == 2


@pytest.mark.parametrize("opening", ["read-only", "writable"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_existing_store_rejects_hard_linked_sidecars(
    tmp_path: Path,
    opening: str,
    suffix: str,
) -> None:
    database = tmp_path / "runs.db"
    writer = EventStore(database)
    try:
        writer.create_run(pending_manifest("run-a"))
        sidecar = Path(f"{database}{suffix}")
        alias = tmp_path / f"sidecar-alias{suffix}"
        assert sidecar.is_file()
        os.link(sidecar, alias)

        with pytest.raises(StoreIntegrityError, match="hard linked"):
            if opening == "read-only":
                EventStore.open_existing_read_only(database)
            else:
                EventStore.open_existing_writable(database)

        assert sidecar.stat().st_ino == alias.stat().st_ino
        assert sidecar.stat().st_nlink == 2
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("opening", "configure_name"),
    [
        ("read-only", "_configure_read_only_connection"),
        ("writable", "_configure_existing_writable_connection"),
    ],
)
def test_existing_store_detects_ancestor_replacement_even_when_leaf_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opening: str,
    configure_name: str,
) -> None:
    state = tmp_path / "state"
    displaced_state = tmp_path / "displaced-state"
    state.mkdir()
    database = state / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    database_inode = database.stat().st_ino
    configure = getattr(EventStore, configure_name)

    def configure_then_replace_parent(store: EventStore) -> None:
        configure(store)
        state.rename(displaced_state)
        state.mkdir()
        (displaced_state / database.name).rename(database)
        assert database.stat().st_ino == database_inode

    monkeypatch.setattr(EventStore, configure_name, configure_then_replace_parent)

    with pytest.raises(StoreIntegrityError, match="path changed while it was being opened"):
        if opening == "read-only":
            EventStore.open_existing_read_only(database, trusted_base=state)
        else:
            EventStore.open_existing_writable(database, trusted_base=state)

    assert database.stat().st_ino == database_inode
    assert displaced_state.is_dir()


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


def test_read_only_verify_integrity_rejects_query_only_drift(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database) as writer:
        writer.create_run(pending_manifest("run-a"))
    with EventStore.open_existing_read_only(database) as reader:
        reader._connection.execute("PRAGMA query_only = OFF")

        with pytest.raises(StoreIntegrityError, match="query_only"):
            reader.verify_integrity()


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

    with pytest.raises(StoreIntegrityError, match="foreign_key_check"):
        EventStore(database, clock=DeterministicClock(started_at=START))


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("sql-event-id", "event ID index mismatch"),
        ("sql-event-type", "event type index mismatch"),
        ("record-run-id", "event run ownership mismatch"),
        ("noncanonical-record", "non-canonical event record"),
    ],
)
def test_verify_integrity_rejects_false_safe_event_representations(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        event = store.create_run(pending_manifest("run-a"))

    with sqlite3.connect(database) as connection:
        if corruption == "sql-event-id":
            connection.execute(
                "UPDATE events SET event_id = ? WHERE run_id = ? AND sequence = 0",
                ("sql-only-event-id", "run-a"),
            )
        elif corruption == "sql-event-type":
            connection.execute(
                "UPDATE events SET event_type = ? WHERE run_id = ? AND sequence = 0",
                ("sql.only_type", "run-a"),
            )
        elif corruption == "record-run-id":
            rebound = event.model_copy(update={"run_id": "run-b"})
            connection.execute(
                """
                UPDATE events SET record_json = ?, event_hash = ?
                WHERE run_id = ? AND sequence = 0
                """,
                (canonical_json(rebound), rebound.content_hash(), "run-a"),
            )
        else:
            connection.execute(
                "UPDATE events SET record_json = record_json || ' ' WHERE run_id = ?",
                ("run-a",),
            )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match=message),
    ):
        store.verify_integrity()


@pytest.mark.parametrize(
    ("column", "message"),
    [
        ("limits_json", "budget limits index mismatch"),
        ("used_json", "non-canonical usage"),
        ("reserved_json", "non-canonical reservation"),
    ],
)
def test_verify_integrity_rejects_noncanonical_budget_json(
    tmp_path: Path,
    column: str,
    message: str,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        store.create_run(pending_manifest("run-a"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"UPDATE budget_state SET {column} = {column} || ' ' WHERE run_id = ?",
            ("run-a",),
        )
        connection.commit()

    with (
        EventStore(database, clock=DeterministicClock(started_at=START)) as store,
        pytest.raises(StoreIntegrityError, match=message),
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


def test_guarded_recovery_requires_current_verified_ledger_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        stale_snapshot_sha256 = verified_run_roots_sha256(store.verify_integrity())

        with pytest.raises(ValueError, match="expected_snapshot_sha256"):
            store.recover_run(
                "run-a",
                finished_at=START + timedelta(seconds=2),
                expected_snapshot_sha256="A" * 64,
            )

        store.create_run(pending_manifest("run-b"))
        events_before = store.list_events("run-a")
        manifest_before = store.load_manifest("run-a")
        budget_before = store.load_budget_state("run-a")
        changes_before = store._connection.total_changes

        with pytest.raises(
            StoreIntegrityError,
            match="snapshot changed before recovery",
        ):
            store.recover_run(
                "run-a",
                finished_at=START + timedelta(seconds=2),
                expected_snapshot_sha256=stale_snapshot_sha256,
            )

        assert store.list_events("run-a") == events_before
        assert store.load_manifest("run-a") == manifest_before
        assert store.load_budget_state("run-a") == budget_before
        assert store._connection.total_changes == changes_before

        current_snapshot_sha256 = verified_run_roots_sha256(store.verify_integrity())
        recovered = store.recover_run(
            "run-a",
            finished_at=START + timedelta(seconds=2),
            expected_snapshot_sha256=current_snapshot_sha256,
        )

    assert recovered.status is RunStatus.INFRASTRUCTURE_ERROR
    assert recovered.terminal_reason == "interrupted_run_recovered"


def test_guarded_recovery_rolls_back_trigger_induced_cross_run_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as store:
        running_run(store, "run-a")
        running_run(store, "run-b")
        expected_snapshot_sha256 = verified_run_roots_sha256(store.verify_integrity())
        events_before = {run_id: store.list_events(run_id) for run_id in ("run-a", "run-b")}
        manifests_before = {run_id: store.load_manifest(run_id) for run_id in ("run-a", "run-b")}
        budgets_before = {run_id: store.load_budget_state(run_id) for run_id in ("run-a", "run-b")}
        validate_schema = store._validate_schema_locked
        validation_count = 0

        def validate_then_inject_trigger() -> None:
            nonlocal validation_count
            validation_count += 1
            validate_schema()
            if validation_count == 2:
                store._connection.execute(_MALICIOUS_AFTER_INSERT_TRIGGER_SQL)

        monkeypatch.setattr(store, "_validate_schema_locked", validate_then_inject_trigger)

        with pytest.raises(StoreIntegrityError, match="manifest hash mismatch for run run-b"):
            store.recover_run(
                "run-a",
                finished_at=START + timedelta(seconds=2),
                expected_snapshot_sha256=expected_snapshot_sha256,
            )

        assert {run_id: store.list_events(run_id) for run_id in events_before} == events_before
        assert {
            run_id: store.load_manifest(run_id) for run_id in manifests_before
        } == manifests_before
        assert {
            run_id: store.load_budget_state(run_id) for run_id in budgets_before
        } == budgets_before
        assert store.verify_integrity()
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchone()[0]
            == 0
        )


def test_guarded_recovery_revalidates_schema_after_existing_store_open(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        running_run(writer, "run-a")
        running_run(writer, "run-b")

    with EventStore.open_existing_writable(
        database,
        clock=DeterministicClock(started_at=START),
    ) as store:
        expected_snapshot_sha256 = verified_run_roots_sha256(store.verify_integrity())
        events_before = {run_id: store.list_events(run_id) for run_id in ("run-a", "run-b")}
        with sqlite3.connect(database) as attacker:
            attacker.execute(_MALICIOUS_AFTER_INSERT_TRIGGER_SQL)

        with pytest.raises(StoreIntegrityError, match="schema is invalid"):
            store.recover_run(
                "run-a",
                finished_at=START + timedelta(seconds=2),
                expected_snapshot_sha256=expected_snapshot_sha256,
            )

        assert {run_id: store.list_events(run_id) for run_id in events_before} == events_before
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type = 'trigger'"
            ).fetchone()[0]
            == 1
        )


def test_every_existing_writable_transaction_revalidates_schema_before_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        writer.create_run(pending_manifest("run-a"))
        writer.create_run(pending_manifest("run-b"))

    with EventStore.open_existing_writable(
        database,
        clock=DeterministicClock(started_at=START),
    ) as store:
        roots_before = store.verify_integrity()
        events_before = {run_id: store.list_events(run_id) for run_id in ("run-a", "run-b")}
        with sqlite3.connect(database) as attacker:
            attacker.execute(_MALICIOUS_AFTER_INSERT_TRIGGER_SQL)

        with pytest.raises(StoreIntegrityError, match="schema is invalid"):
            store.start_run(
                "run-a",
                started_at=START + timedelta(seconds=1),
                requested_model="fake-model-v1",
            )

        assert {run_id: store.list_events(run_id) for run_id in events_before} == events_before
        assert store.load_manifest("run-a").status is RunStatus.PENDING
        assert store.load_manifest("run-b").status is RunStatus.PENDING
        with pytest.raises(StoreIntegrityError, match="schema is invalid"):
            store.verify_integrity()
        with sqlite3.connect(database) as attacker:
            attacker.execute("DROP TRIGGER corrupt_other_run_after_event")
        assert store.verify_integrity() == roots_before


def test_existing_writable_revalidates_ancestor_identity_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    displaced_state = tmp_path / "displaced-state"
    state.mkdir()
    database = state / "runs.db"
    with EventStore(database, clock=DeterministicClock(started_at=START)) as writer:
        running_run(writer, "run-a")
    database_inode = database.stat().st_ino

    with EventStore.open_existing_writable(
        database,
        clock=DeterministicClock(started_at=START),
        trusted_base=state,
    ) as store:
        verify_roots = store._verified_roots_locked

        def verify_then_replace_parent() -> tuple[VerifiedRunRoot, ...]:
            roots = verify_roots()
            state.rename(displaced_state)
            state.mkdir()
            (displaced_state / database.name).rename(database)
            assert database.stat().st_ino == database_inode
            return roots

        monkeypatch.setattr(store, "_verified_roots_locked", verify_then_replace_parent)

        with pytest.raises(StoreIntegrityError, match="path changed while it was being opened"):
            store.recover_run(
                "run-a",
                finished_at=START + timedelta(seconds=2),
            )

    assert database.stat().st_ino == database_inode
    with EventStore.open_existing_read_only(database, trusted_base=state) as reader:
        assert reader.load_manifest("run-a").status is RunStatus.RUNNING


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
