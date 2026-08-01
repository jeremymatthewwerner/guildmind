"""Single-writer SQLite event ledger and run-state index."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import JsonValue

from guildmind.domain import (
    BudgetUsage,
    EventRecord,
    RunManifest,
    RunStatus,
    canonical_json,
    canonical_sha256,
)
from guildmind.runtime.clock import Clock, SystemClock


class StoreIntegrityError(RuntimeError):
    """Raised when persisted state violates its recorded identity or ordering."""


class EventStore:
    """Own all local control-plane writes through one SQLite connection."""

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or SystemClock()
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_run(self, manifest: RunManifest) -> EventRecord:
        if manifest.status is not RunStatus.PENDING:
            raise ValueError("new run manifests must be pending")
        manifest_json = canonical_json(manifest)
        manifest_hash = canonical_sha256(manifest)
        empty_usage = BudgetUsage()
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO runs(
                    run_id, status, manifest_revision, manifest_json, manifest_sha256
                ) VALUES (?, ?, 0, ?, ?)
                """,
                (manifest.run_id, manifest.status.value, manifest_json, manifest_hash),
            )
            self._connection.execute(
                """
                INSERT INTO run_manifests(run_id, revision, manifest_json, manifest_sha256)
                VALUES (?, 0, ?, ?)
                """,
                (manifest.run_id, manifest_json, manifest_hash),
            )
            self._connection.execute(
                """
                INSERT INTO budget_state(run_id, limits_json, used_json, reserved_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    manifest.run_id,
                    canonical_json(manifest.budget_limits),
                    canonical_json(empty_usage),
                    canonical_json(empty_usage),
                ),
            )
            return self._append_locked(
                run_id=manifest.run_id,
                event_type="run.created",
                payload={
                    "candidate_id": manifest.candidate_id,
                    "experiment_id": manifest.experiment_id,
                    "status": manifest.status.value,
                    "task_id": manifest.task_id,
                },
            )

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, JsonValue] | None = None,
        causal_parent_ids: Sequence[str] | None = None,
        manifest: RunManifest | None = None,
        budget_used: BudgetUsage | None = None,
        budget_reserved: BudgetUsage | None = None,
    ) -> EventRecord:
        if (budget_used is None) is not (budget_reserved is None):
            raise ValueError("budget_used and budget_reserved must be supplied together")
        with self._transaction():
            self._require_run(run_id)
            event = self._append_locked(
                run_id=run_id,
                event_type=event_type,
                payload=payload or {},
                causal_parent_ids=causal_parent_ids,
            )
            if manifest is not None:
                self._save_manifest_locked(run_id, manifest)
            if budget_used is not None and budget_reserved is not None:
                self._connection.execute(
                    """
                    UPDATE budget_state SET used_json = ?, reserved_json = ? WHERE run_id = ?
                    """,
                    (canonical_json(budget_used), canonical_json(budget_reserved), run_id),
                )
            return event

    def load_manifest(self, run_id: str) -> RunManifest:
        row = self._connection.execute(
            "SELECT manifest_json, manifest_sha256 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        manifest = RunManifest.model_validate(json.loads(row["manifest_json"]))
        if canonical_sha256(manifest) != row["manifest_sha256"]:
            raise StoreIntegrityError(f"manifest hash mismatch for run {run_id}")
        return manifest

    def load_budget_state(self, run_id: str) -> tuple[BudgetUsage, BudgetUsage]:
        row = self._connection.execute(
            "SELECT used_json, reserved_json FROM budget_state WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return (
            BudgetUsage.model_validate(json.loads(row["used_json"])),
            BudgetUsage.model_validate(json.loads(row["reserved_json"])),
        )

    def list_events(self, run_id: str) -> list[EventRecord]:
        rows = self._connection.execute(
            """
            SELECT sequence, record_json, event_hash
            FROM events WHERE run_id = ? ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            self._require_run(run_id)
            return []
        events: list[EventRecord] = []
        previous_hash: str | None = None
        for expected_sequence, row in enumerate(rows):
            event = EventRecord.model_validate(json.loads(row["record_json"]))
            event_hash = event.content_hash()
            if row["sequence"] != expected_sequence or event.sequence != expected_sequence:
                raise StoreIntegrityError(f"non-contiguous event sequence for run {run_id}")
            if event.previous_event_hash != previous_hash:
                raise StoreIntegrityError(f"broken event predecessor for run {run_id}")
            if event_hash != row["event_hash"]:
                raise StoreIntegrityError(f"event hash mismatch at sequence {expected_sequence}")
            events.append(event)
            previous_hash = event_hash
        return events

    def list_run_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute("SELECT run_id FROM runs ORDER BY run_id ASC").fetchall()
        return tuple(row["run_id"] for row in rows)

    def events_jsonl(self, run_id: str) -> str:
        events = self.list_events(run_id)
        return "".join(f"{canonical_json(event)}\n" for event in events)

    def _append_locked(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, JsonValue],
        causal_parent_ids: Sequence[str] | None = None,
    ) -> EventRecord:
        last = self._connection.execute(
            """
            SELECT sequence, event_id, event_hash FROM events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if last is None:
            sequence = 0
            previous_hash = None
            default_parents: tuple[str, ...] = ()
        else:
            sequence = int(last["sequence"]) + 1
            previous_hash = str(last["event_hash"])
            default_parents = (str(last["event_id"]),)
        parents = tuple(causal_parent_ids) if causal_parent_ids is not None else default_parents
        stamp = self._clock.stamp()
        event = EventRecord(
            event_id=f"{run_id}:event:{sequence:08d}",
            run_id=run_id,
            sequence=sequence,
            causal_parent_ids=parents,
            event_type=event_type,
            monotonic_ns=stamp.monotonic_ns,
            occurred_at=stamp.occurred_at,
            payload=payload,
            payload_sha256=canonical_sha256(payload),
            previous_event_hash=previous_hash,
        )
        event_hash = event.content_hash()
        self._connection.execute(
            """
            INSERT INTO events(
                run_id, sequence, event_id, event_type, record_json, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event.sequence,
                event.event_id,
                event.event_type,
                canonical_json(event),
                event_hash,
            ),
        )
        return event

    def _save_manifest_locked(self, run_id: str, manifest: RunManifest) -> None:
        if manifest.run_id != run_id:
            raise ValueError("manifest run ID does not match event run ID")
        row = self._connection.execute(
            "SELECT manifest_revision FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        revision = int(row["manifest_revision"]) + 1
        manifest_json = canonical_json(manifest)
        manifest_hash = canonical_sha256(manifest)
        self._connection.execute(
            """
            INSERT INTO run_manifests(run_id, revision, manifest_json, manifest_sha256)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, revision, manifest_json, manifest_hash),
        )
        self._connection.execute(
            """
            UPDATE runs
            SET status = ?, manifest_revision = ?, manifest_json = ?, manifest_sha256 = ?
            WHERE run_id = ?
            """,
            (manifest.status.value, revision, manifest_json, manifest_hash, run_id),
        )

    def _require_run(self, run_id: str) -> None:
        row = self._connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                manifest_revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS run_manifests (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                PRIMARY KEY(run_id, revision)
            );

            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                record_json TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence)
            );

            CREATE TABLE IF NOT EXISTS budget_state (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                limits_json TEXT NOT NULL,
                used_json TEXT NOT NULL,
                reserved_json TEXT NOT NULL
            );
            """
        )
        self._connection.commit()
