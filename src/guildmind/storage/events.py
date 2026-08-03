"""Single-writer SQLite event ledger and run-state index."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self

from pydantic import JsonValue

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
from guildmind.runtime.clock import Clock, SystemClock
from guildmind.runtime.replay import (
    EXPECTED_ARTIFACT_NAMES,
    OPTIONAL_EVALUATION_ARTIFACT_NAMES,
    ReplayIntegrityError,
    ReplayState,
    replay_events,
)

_USAGE_FIELDS = (
    "uncached_input_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "model_calls",
    "model_retries",
    "tool_calls",
    "tool_cpu_seconds",
    "container_wall_seconds",
    "elapsed_seconds",
    "estimated_cost_usd",
)
_MANIFEST_MUTABLE_FIELDS = {
    "artifacts",
    "finished_at",
    "returned_model",
    "started_at",
    "status",
    "terminal_reason",
}
_CONNECTION_TIMEOUT_SECONDS = 5.0
_BUSY_TIMEOUT_MILLISECONDS = 5_000
_VERIFIED_RUN_ROOT_SNAPSHOT_SCHEMA_VERSION = "guildmind.verified-run-root-snapshot/v1"
_EXPECTED_CONNECTION_SETTINGS: tuple[tuple[str, str | int], ...] = (
    ("foreign_keys", 1),
    ("journal_mode", "wal"),
    ("synchronous", 2),
    ("busy_timeout", _BUSY_TIMEOUT_MILLISECONDS),
)
_EXPECTED_READ_ONLY_CONNECTION_SETTINGS: tuple[tuple[str, str | int], ...] = (
    ("foreign_keys", 1),
    ("synchronous", 2),
    ("busy_timeout", _BUSY_TIMEOUT_MILLISECONDS),
    ("query_only", 1),
)
_EXPECTED_TABLE_SQL: tuple[tuple[str, str], ...] = (
    (
        "runs",
        """CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                manifest_revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL
            )""",
    ),
    (
        "run_manifests",
        """CREATE TABLE run_manifests (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                revision INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_sha256 TEXT NOT NULL,
                PRIMARY KEY(run_id, revision)
            )""",
    ),
    (
        "events",
        """CREATE TABLE events (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                record_json TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                PRIMARY KEY(run_id, sequence)
            )""",
    ),
    (
        "budget_state",
        """CREATE TABLE budget_state (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                limits_json TEXT NOT NULL,
                used_json TEXT NOT NULL,
                reserved_json TEXT NOT NULL
            )""",
    ),
)
_SCHEMA_INITIALIZATION_SQL = (
    ";\n\n".join(
        sql.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)
        for _, sql in _EXPECTED_TABLE_SQL
    )
    + ";"
)
_EXPECTED_TABLE_COLUMNS: Mapping[
    str,
    tuple[tuple[int, str, str, int, str | None, int, int], ...],
] = {
    "runs": (
        (0, "run_id", "TEXT", 0, None, 1, 0),
        (1, "status", "TEXT", 1, None, 0, 0),
        (2, "manifest_revision", "INTEGER", 1, None, 0, 0),
        (3, "manifest_json", "TEXT", 1, None, 0, 0),
        (4, "manifest_sha256", "TEXT", 1, None, 0, 0),
    ),
    "run_manifests": (
        (0, "run_id", "TEXT", 1, None, 1, 0),
        (1, "revision", "INTEGER", 1, None, 2, 0),
        (2, "manifest_json", "TEXT", 1, None, 0, 0),
        (3, "manifest_sha256", "TEXT", 1, None, 0, 0),
    ),
    "events": (
        (0, "run_id", "TEXT", 1, None, 1, 0),
        (1, "sequence", "INTEGER", 1, None, 2, 0),
        (2, "event_id", "TEXT", 1, None, 0, 0),
        (3, "event_type", "TEXT", 1, None, 0, 0),
        (4, "record_json", "TEXT", 1, None, 0, 0),
        (5, "event_hash", "TEXT", 1, None, 0, 0),
    ),
    "budget_state": (
        (0, "run_id", "TEXT", 0, None, 1, 0),
        (1, "limits_json", "TEXT", 1, None, 0, 0),
        (2, "used_json", "TEXT", 1, None, 0, 0),
        (3, "reserved_json", "TEXT", 1, None, 0, 0),
    ),
}
_EXPECTED_FOREIGN_KEYS: Mapping[
    str,
    tuple[tuple[int, int, str, str, str, str, str, str], ...],
] = {
    "runs": (),
    "run_manifests": ((0, 0, "runs", "run_id", "run_id", "NO ACTION", "NO ACTION", "NONE"),),
    "events": ((0, 0, "runs", "run_id", "run_id", "NO ACTION", "NO ACTION", "NONE"),),
    "budget_state": ((0, 0, "runs", "run_id", "run_id", "NO ACTION", "NO ACTION", "NONE"),),
}
_EXPECTED_TABLE_INDEXES: Mapping[
    str,
    tuple[tuple[str, int, str, int], ...],
] = {
    "runs": (("sqlite_autoindex_runs_1", 1, "pk", 0),),
    "run_manifests": (("sqlite_autoindex_run_manifests_1", 1, "pk", 0),),
    "events": (
        ("sqlite_autoindex_events_1", 1, "u", 0),
        ("sqlite_autoindex_events_2", 1, "pk", 0),
    ),
    "budget_state": (("sqlite_autoindex_budget_state_1", 1, "pk", 0),),
}
_EXPECTED_INDEX_COLUMNS: Mapping[
    str,
    tuple[tuple[int, int, str | None, int, str, int], ...],
] = {
    "sqlite_autoindex_runs_1": (
        (0, 0, "run_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_run_manifests_1": (
        (0, 0, "run_id", 0, "BINARY", 1),
        (1, 1, "revision", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_events_1": (
        (0, 2, "event_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_events_2": (
        (0, 0, "run_id", 0, "BINARY", 1),
        (1, 1, "sequence", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_budget_state_1": (
        (0, 0, "run_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
}


class StoreIntegrityError(RuntimeError):
    """Raised when persisted state violates its recorded identity or ordering."""


@dataclass(frozen=True, slots=True)
class VerifiedRunRoot:
    """Validated manifest and ledger-head identity for one run."""

    manifest: RunManifest
    manifest_revision: int
    manifest_sha256: str
    event_count: int
    head_event_sha256: str


@dataclass(frozen=True, slots=True)
class EventStoreTerminalResult:
    """A terminal manifest and event stream captured inside its write transaction."""

    manifest: RunManifest
    events: tuple[EventRecord, ...]


def verified_run_roots_sha256(roots: Sequence[VerifiedRunRoot]) -> str:
    """Commit deterministically to an ordered set of verified ledger roots."""

    ordered = tuple(sorted(roots, key=lambda root: root.manifest.run_id))
    identities: list[dict[str, str | int]] = []
    previous_run_id: str | None = None
    for root in ordered:
        run_id = root.manifest.run_id
        if run_id == previous_run_id:
            raise ValueError(f"duplicate verified run root: {run_id}")
        previous_run_id = run_id
        identities.append(
            {
                "event_count": root.event_count,
                "head_event_sha256": root.head_event_sha256,
                "manifest_revision": root.manifest_revision,
                "manifest_sha256": root.manifest_sha256,
                "run_id": run_id,
            }
        )
    return canonical_sha256(
        {
            "roots": identities,
            "schema_version": _VERIFIED_RUN_ROOT_SNAPSHOT_SCHEMA_VERSION,
        }
    )


@dataclass(frozen=True, slots=True)
class _DatabaseFileIdentity:
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _DatabasePathIdentity:
    directory_identities: tuple[_DatabaseFileIdentity, ...]
    leaf_identity: _DatabaseFileIdentity


@dataclass(frozen=True, slots=True)
class _SidecarState:
    wal_identity: _DatabaseFileIdentity | None
    wal_size: int | None
    shm_identity: _DatabaseFileIdentity | None
    shm_size: int | None


class EventStore:
    """Own all local control-plane writes through one SQLite connection."""

    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        configured_path = Path(os.path.abspath(path))
        configured_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path, resolved_base = _canonicalize_existing_database_path(
            configured_path,
            trusted_base=configured_path.parent,
        )
        _preflight_missing_database_sidecars(absolute_path)
        created_identity = _create_database_exclusively(absolute_path)
        if created_identity is None:
            self._open_existing_writable_in_place(
                absolute_path,
                clock=clock,
                trusted_base=resolved_base,
            )
            return

        expected_path_identity = _validate_existing_database_path(
            absolute_path,
            trusted_base=resolved_base,
            allow_empty=True,
        )
        if expected_path_identity.leaf_identity != created_identity:
            raise StoreIntegrityError("new event store changed before it could be opened")
        sidecars = _inspect_existing_sidecars(absolute_path)
        if sidecars.wal_identity is not None or sidecars.shm_identity is not None:
            raise StoreIntegrityError("new event store path has pre-existing SQLite sidecars")

        self.path = absolute_path
        self._clock = clock or SystemClock()
        self._read_only = False
        self._read_only_journal_mode = "wal"
        self._trusted_base = resolved_base
        self._existing_path_identity = expected_path_identity
        uri = f"{absolute_path.as_uri()}?mode=rw&cache=private"
        try:
            self._connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=_CONNECTION_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError("new event store could not be opened writable") from error
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self._initialize_schema()
            self._validate_existing_database()
            _validate_existing_database_path(
                absolute_path,
                trusted_base=resolved_base,
                expected_identity=expected_path_identity,
            )
            _validate_persisted_wal_header(absolute_path, expected_path_identity)
            _inspect_existing_sidecars(absolute_path)
        except BaseException:
            self._connection.close()
            raise

    @classmethod
    def open_existing_read_only(
        cls,
        path: Path,
        *,
        clock: Clock | None = None,
        trusted_base: Path | None = None,
    ) -> Self:
        """Open an existing database without creating its main DB or schema.

        ``trusted_base`` is resolved once so trusted operating-system path aliases are
        accepted; descendants through the database leaf may not be links. The database
        must remain quiescent for this handle. A present WAL requires its existing SHM;
        committed WAL state is included, and SQLite may update coordination metadata in
        that existing SHM. When both sidecars are absent, immutable mode prevents their
        creation and their absence is checked again after database validation.
        """

        absolute_path, resolved_base = _canonicalize_existing_database_path(
            path,
            trusted_base=trusted_base,
        )
        expected_path_identity = _validate_existing_database_path(
            absolute_path,
            trusted_base=resolved_base,
        )
        _validate_persisted_wal_header(absolute_path, expected_path_identity)
        expected_sidecars = _inspect_existing_sidecars(absolute_path)
        immutable = expected_sidecars.wal_identity is None
        instance = cls.__new__(cls)
        instance.path = absolute_path
        instance._clock = clock or SystemClock()
        instance._read_only = True
        instance._read_only_journal_mode = "delete" if immutable else "wal"
        instance._trusted_base = resolved_base
        instance._existing_path_identity = expected_path_identity
        immutable_parameter = "&immutable=1" if immutable else ""
        uri = f"{absolute_path.as_uri()}?mode=ro{immutable_parameter}&cache=private"
        try:
            instance._connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=_CONNECTION_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError(
                "existing event store could not be opened read-only"
            ) from error
        instance._connection.row_factory = sqlite3.Row
        try:
            instance._configure_read_only_connection()
            instance._validate_existing_database()
            _validate_existing_database_path(
                absolute_path,
                trusted_base=resolved_base,
                expected_identity=expected_path_identity,
            )
            _validate_persisted_wal_header(absolute_path, expected_path_identity)
            if _inspect_existing_sidecars(absolute_path) != expected_sidecars:
                raise StoreIntegrityError("event store sidecars changed while opening")
        except BaseException:
            instance._connection.close()
            raise
        return instance

    @classmethod
    def open_existing_writable(
        cls,
        path: Path,
        *,
        clock: Clock | None = None,
        trusted_base: Path | None = None,
    ) -> Self:
        """Open an existing WAL database for writes without creating or migrating it."""

        instance = cls.__new__(cls)
        instance._open_existing_writable_in_place(
            path,
            clock=clock,
            trusted_base=trusted_base,
        )
        return instance

    def _open_existing_writable_in_place(
        self,
        path: Path,
        *,
        clock: Clock | None,
        trusted_base: Path | None,
    ) -> None:
        absolute_path, resolved_base = _canonicalize_existing_database_path(
            path,
            trusted_base=trusted_base,
        )
        expected_path_identity = _validate_existing_database_path(
            absolute_path,
            trusted_base=resolved_base,
        )
        _validate_persisted_wal_header(absolute_path, expected_path_identity)
        _inspect_existing_sidecars(absolute_path)
        self.path = absolute_path
        self._clock = clock or SystemClock()
        self._read_only = False
        self._read_only_journal_mode = "wal"
        self._trusted_base = resolved_base
        self._existing_path_identity = expected_path_identity
        uri = f"{absolute_path.as_uri()}?mode=rw&cache=private"
        try:
            self._connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=_CONNECTION_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError(
                "existing event store could not be opened writable"
            ) from error
        self._connection.row_factory = sqlite3.Row
        try:
            self._configure_existing_writable_connection()
            self._validate_existing_database()
            _validate_existing_database_path(
                absolute_path,
                trusted_base=resolved_base,
                expected_identity=expected_path_identity,
            )
            _validate_persisted_wal_header(absolute_path, expected_path_identity)
            _inspect_existing_sidecars(absolute_path)
        except BaseException:
            self._connection.close()
            raise

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
                payload=_manifest_creation_payload(manifest),
            )

    def append_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> None:
        """Reject direct phase writes; callers must use an atomic lifecycle method."""

        del run_id, payload
        raise ValueError(f"direct {event_type} append is disabled; use a lifecycle method")

    def start_run(
        self,
        run_id: str,
        *,
        started_at: datetime,
        requested_model: str,
    ) -> RunManifest:
        """Atomically append the start event and advance the manifest to running."""

        with self._transaction():
            current = self._load_manifest_locked(run_id)
            if current.status is not RunStatus.PENDING:
                raise StoreIntegrityError("run start requires a pending manifest")
            if requested_model != current.requested_model:
                raise StoreIntegrityError("run start model disagrees with the manifest")
            running = _transition_manifest(
                current,
                status=RunStatus.RUNNING,
                started_at=started_at,
            )
            self._append_locked(
                run_id=run_id,
                event_type="run.started",
                payload={"requested_model": requested_model},
            )
            self._save_manifest_locked(run_id, running)
            return running

    def record_artifact(self, run_id: str, name: str, artifact: ArtifactRef) -> EventRecord:
        """Bind one durable CAS reference to a running manifest and event atomically."""

        if name != "task_spec":
            raise ValueError("record_artifact may only bind the initial task_spec")
        with self._transaction():
            current = self._load_manifest_locked(run_id)
            state = self._validated_state_locked(run_id, current)
            if (
                current.status is not RunStatus.RUNNING
                or state.model_request_state != "not_started"
            ):
                raise StoreIntegrityError("task_spec must be bound before model work starts")
            artifacts = dict(current.artifacts)
            if name in artifacts:
                raise StoreIntegrityError(f"artifact name is already bound: {name}")
            artifacts[name] = artifact
            updated = _transition_manifest(current, artifacts=artifacts)
            event = self._append_locked(
                run_id=run_id,
                event_type="artifact.recorded",
                payload=_artifact_payload(name, artifact),
            )
            self._save_manifest_locked(run_id, updated)
            return event

    def start_model_request(
        self,
        *,
        run_id: str,
        request_id: str,
        maximum: BudgetUsage,
        budget_used: BudgetUsage,
        budget_reserved: BudgetUsage,
    ) -> tuple[EventRecord, EventRecord]:
        """Durably reserve conservative usage before any model dispatch."""

        if not request_id:
            raise ValueError("request_id cannot be empty")
        if maximum.model_calls != 1:
            raise StoreIntegrityError("one model request must reserve exactly one model call")
        with self._transaction():
            manifest = self._load_manifest_locked(run_id)
            if manifest.status is not RunStatus.RUNNING:
                raise StoreIntegrityError("model requests require a running manifest")
            used_before, reserved_before = self._load_budget_locked(run_id)
            if budget_used != used_before:
                raise StoreIntegrityError("request reservation changed used budget")
            if budget_reserved != _add_usage(reserved_before, maximum):
                raise StoreIntegrityError("request reservation does not match its maximum")
            projected = _add_usage(budget_used, budget_reserved)
            exceeded = projected.exceeded_limits(manifest.budget_limits)
            if exceeded:
                raise StoreIntegrityError(
                    f"request reservation exceeds budget: {', '.join(exceeded)}"
                )
            request_event = self._append_locked(
                run_id=run_id,
                event_type="model.request_started",
                payload={
                    "maximum": maximum.model_dump(mode="json"),
                    "request_id": request_id,
                },
            )
            budget_event = self._append_budget_snapshot_locked(
                run_id,
                budget_used,
                budget_reserved,
            )
            self._update_budget_locked(run_id, budget_used, budget_reserved)
            return request_event, budget_event

    def complete_model_response(
        self,
        *,
        run_id: str,
        request_id: str,
        returned_model: str,
        actual_usage: BudgetUsage,
        patch: ArtifactRef,
        budget_used: BudgetUsage,
        budget_reserved: BudgetUsage,
    ) -> RunManifest:
        """Commit response identity, actual usage, and patch binding as one phase."""

        with self._transaction():
            current = self._load_manifest_locked(run_id)
            state = self._validated_state_locked(run_id, current)
            if state.model_request_state != "started" or state.model_request_id != request_id:
                raise StoreIntegrityError("model response has no matching outstanding request")
            maximum = state.model_request_maximum
            if maximum is None or not _usage_at_most(actual_usage, maximum):
                raise StoreIntegrityError("actual model usage exceeds its reservation")
            if actual_usage.model_calls != 1:
                raise StoreIntegrityError("one model response must report exactly one model call")
            used_before, reserved_before = self._load_budget_locked(run_id)
            expected_used = _add_usage(used_before, actual_usage)
            expected_reserved = _subtract_usage(reserved_before, maximum)
            if budget_used != expected_used or budget_reserved != expected_reserved:
                raise StoreIntegrityError("model response budget reconciliation is inconsistent")
            artifacts = dict(current.artifacts)
            if "patch" in artifacts:
                raise StoreIntegrityError("artifact name is already bound: patch")
            artifacts["patch"] = patch
            updated = _transition_manifest(
                current,
                returned_model=returned_model,
                artifacts=artifacts,
            )

            self._append_locked(
                run_id=run_id,
                event_type="model.response_completed",
                payload={
                    "actual": actual_usage.model_dump(mode="json"),
                    "request_id": request_id,
                    "returned_model": returned_model,
                },
            )
            self._append_budget_snapshot_locked(run_id, budget_used, budget_reserved)
            self._append_locked(
                run_id=run_id,
                event_type="artifact.recorded",
                payload=_artifact_payload("patch", patch),
            )
            self._save_manifest_locked(run_id, updated)
            self._update_budget_locked(run_id, budget_used, budget_reserved)
            return updated

    def complete_evaluation(
        self,
        *,
        run_id: str,
        artifacts: Mapping[str, ArtifactRef],
        evaluation_payload: dict[str, JsonValue],
        status: RunStatus,
        finished_at: datetime,
        terminal_reason: str | None,
        budget_used: BudgetUsage,
        budget_reserved: BudgetUsage,
    ) -> RunManifest:
        """Compatibility wrapper returning only the terminal manifest."""

        return self.complete_evaluation_with_events(
            run_id=run_id,
            artifacts=artifacts,
            evaluation_payload=evaluation_payload,
            status=status,
            finished_at=finished_at,
            terminal_reason=terminal_reason,
            budget_used=budget_used,
            budget_reserved=budget_reserved,
        ).manifest

    def complete_evaluation_with_events(
        self,
        *,
        run_id: str,
        artifacts: Mapping[str, ArtifactRef],
        evaluation_payload: dict[str, JsonValue],
        status: RunStatus,
        finished_at: datetime,
        terminal_reason: str | None,
        budget_used: BudgetUsage,
        budget_reserved: BudgetUsage,
    ) -> EventStoreTerminalResult:
        """Atomically bind evaluation evidence and capture its terminal event stream."""

        if not status.is_terminal:
            raise ValueError("evaluation completion requires a terminal status")
        required = {"evaluation", "evaluation_stderr", "evaluation_stdout"}
        if not required.issubset(artifacts):
            missing = required - set(artifacts)
            raise ValueError(f"evaluation artifacts are missing: {', '.join(sorted(missing))}")
        allowed = required | OPTIONAL_EVALUATION_ARTIFACT_NAMES
        if unexpected := set(artifacts) - allowed:
            raise ValueError(
                f"evaluation artifacts have unknown roles: {', '.join(sorted(unexpected))}"
            )
        if (
            "evaluation_scorer_stdout" in artifacts
            and "evaluation_candidate_stdout" not in artifacts
        ):
            raise ValueError("a scorer transcript requires a candidate transcript")
        with self._transaction():
            current = self._load_manifest_locked(run_id)
            state = self._validated_state_locked(run_id, current)
            if current.status is not RunStatus.RUNNING:
                raise StoreIntegrityError("evaluation completion requires a running manifest")
            if state.model_request_state != "completed":
                raise StoreIntegrityError("evaluation completion requires a model response")
            outcome = evaluation_payload.get("outcome")
            if not isinstance(outcome, str) or not _terminal_status_matches_outcome(
                status,
                outcome,
            ):
                raise StoreIntegrityError("evaluation outcome disagrees with terminal status")
            self._validate_budget_update_locked(run_id, budget_used, budget_reserved)
            if budget_reserved != BudgetUsage():
                raise StoreIntegrityError("terminal runs cannot retain reservations")

            combined_artifacts = dict(current.artifacts)
            for name in artifacts:
                if name in combined_artifacts:
                    raise StoreIntegrityError(f"artifact name is already bound: {name}")
            combined_artifacts.update(artifacts)
            final_manifest = _transition_manifest(
                current,
                status=status,
                finished_at=finished_at,
                terminal_reason=terminal_reason,
                artifacts=combined_artifacts,
            )

            preferred_order = (
                "evaluation_stdout",
                "evaluation_stderr",
                "evaluation_candidate_stdout",
                "evaluation_scorer_stdout",
                "evaluation",
            )
            ordered_names = [name for name in preferred_order if name in artifacts]
            ordered_names.extend(sorted(set(artifacts) - set(ordered_names)))
            for name in ordered_names:
                self._append_locked(
                    run_id=run_id,
                    event_type="artifact.recorded",
                    payload=_artifact_payload(name, artifacts[name]),
                )
            self._append_locked(
                run_id=run_id,
                event_type="evaluation.completed",
                payload=evaluation_payload,
            )
            self._append_budget_snapshot_locked(run_id, budget_used, budget_reserved)
            self._append_locked(
                run_id=run_id,
                event_type="run.terminal",
                payload={
                    "evaluation_outcome": evaluation_payload.get("outcome"),
                    "status": status.value,
                    "terminal_reason": terminal_reason,
                },
            )
            self._save_manifest_locked(run_id, final_manifest)
            self._update_budget_locked(run_id, budget_used, budget_reserved)
            self._validate_terminal_state_locked(run_id, final_manifest)
            return EventStoreTerminalResult(
                manifest=final_manifest,
                events=tuple(self._list_events_locked(run_id)),
            )

    def recover_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        terminal_reason: str = "interrupted_run_recovered",
        expected_snapshot_sha256: str | None = None,
        integrity_guard: Callable[[tuple[VerifiedRunRoot, ...]], None] | None = None,
    ) -> RunManifest:
        """Compatibility wrapper returning only the recovered manifest."""

        return self.recover_run_with_events(
            run_id,
            finished_at=finished_at,
            terminal_reason=terminal_reason,
            expected_snapshot_sha256=expected_snapshot_sha256,
            integrity_guard=integrity_guard,
        ).manifest

    def recover_run_with_events(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        terminal_reason: str = "interrupted_run_recovered",
        expected_snapshot_sha256: str | None = None,
        integrity_guard: Callable[[tuple[VerifiedRunRoot, ...]], None] | None = None,
    ) -> EventStoreTerminalResult:
        """Terminalize an interrupted run without redispatching external work.

        A started request with no recorded response is classified as ambiguous and its
        full outstanding reservation is charged. Calling this method again on the
        resulting terminal run is a read-only no-op. When an expected snapshot is
        supplied, the complete ledger commitment must still match under the recovery
        write transaction before any lifecycle state is changed. ``integrity_guard``
        receives those exact writer-locked roots and must return successfully before
        recovery may stage a lifecycle mutation. It is invoked again against the
        post-mutation roots at the transaction's final pre-commit boundary.
        """

        if not terminal_reason.strip():
            raise ValueError("terminal_reason cannot be empty")
        if expected_snapshot_sha256 is not None and not _is_lower_sha256(expected_snapshot_sha256):
            raise ValueError("expected_snapshot_sha256 must be a lowercase SHA-256")
        if integrity_guard is not None and expected_snapshot_sha256 is None:
            raise ValueError("integrity_guard requires expected_snapshot_sha256")
        with self._transaction(precommit_integrity_guard=integrity_guard):
            self._validate_schema_locked()
            if expected_snapshot_sha256 is not None or integrity_guard is not None:
                locked_roots = self._verified_roots_locked()
                observed_snapshot_sha256 = verified_run_roots_sha256(locked_roots)
                if observed_snapshot_sha256 != expected_snapshot_sha256:
                    raise StoreIntegrityError("verified run root snapshot changed before recovery")
                if integrity_guard is not None:
                    integrity_guard(locked_roots)
            current = self._load_manifest_locked(run_id)
            state = self._validated_state_locked(run_id, current)
            if current.status.is_terminal:
                self._validate_terminal_state_locked(run_id, current)
                return EventStoreTerminalResult(
                    manifest=current,
                    events=tuple(self._list_events_locked(run_id)),
                )

            absence_reason = "interrupted"
            for name in sorted(EXPECTED_ARTIFACT_NAMES - set(state.artifacts)):
                if name in state.absent_artifacts:
                    continue
                self._append_locked(
                    run_id=run_id,
                    event_type="artifact.not_produced",
                    payload={"name": name, "reason": absence_reason},
                )

            used, reserved = self._load_budget_locked(run_id)
            reason = terminal_reason
            if state.model_request_state == "started":
                if state.model_request_id is None:
                    raise StoreIntegrityError("outstanding model request has no ID")
                self._append_locked(
                    run_id=run_id,
                    event_type="model.request_ambiguous",
                    payload={"request_id": state.model_request_id},
                )
                used = _add_usage(used, reserved)
                reserved = BudgetUsage()
                reason = "ambiguous_model_request"
            elif reserved != BudgetUsage():
                raise StoreIntegrityError("run has a reservation without outstanding work")

            self._append_budget_snapshot_locked(run_id, used, reserved)
            safe_finished_at = max(
                finished_at,
                current.started_at or current.created_at,
            )
            final_manifest = _transition_manifest(
                current,
                status=RunStatus.INFRASTRUCTURE_ERROR,
                finished_at=safe_finished_at,
                terminal_reason=reason,
            )
            self._append_locked(
                run_id=run_id,
                event_type="run.terminal",
                payload={
                    "recovered": True,
                    "status": RunStatus.INFRASTRUCTURE_ERROR.value,
                    "terminal_reason": reason,
                },
            )
            self._save_manifest_locked(run_id, final_manifest)
            self._update_budget_locked(run_id, used, reserved)
            self._validate_terminal_state_locked(run_id, final_manifest)
            try:
                self._verified_roots_locked()
            except StoreIntegrityError:
                raise
            except (IndexError, KeyError, ValueError, sqlite3.DatabaseError) as error:
                raise StoreIntegrityError(
                    "database integrity validation failed after recovery"
                ) from error
            return EventStoreTerminalResult(
                manifest=final_manifest,
                events=tuple(self._list_events_locked(run_id)),
            )

    def complete_budget_exhaustion(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        terminal_reason: str = "model_reservation_refused",
    ) -> RunManifest:
        """Compatibility wrapper returning only the budget-exhausted manifest."""

        return self.complete_budget_exhaustion_with_events(
            run_id,
            finished_at=finished_at,
            terminal_reason=terminal_reason,
        ).manifest

    def complete_budget_exhaustion_with_events(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        terminal_reason: str = "model_reservation_refused",
        expected_snapshot_sha256: str | None = None,
        integrity_guard: Callable[[tuple[VerifiedRunRoot, ...]], None] | None = None,
    ) -> EventStoreTerminalResult:
        """Terminalize a pre-dispatch refusal and capture its terminal event stream."""

        if not terminal_reason.strip():
            raise ValueError("terminal_reason cannot be empty")
        if expected_snapshot_sha256 is not None and not _is_lower_sha256(expected_snapshot_sha256):
            raise ValueError("expected_snapshot_sha256 must be a lowercase SHA-256")
        if integrity_guard is not None and expected_snapshot_sha256 is None:
            raise ValueError("integrity_guard requires expected_snapshot_sha256")
        with self._transaction(precommit_integrity_guard=integrity_guard):
            self._validate_schema_locked()
            if expected_snapshot_sha256 is not None or integrity_guard is not None:
                locked_roots = self._verified_roots_locked()
                observed_snapshot_sha256 = verified_run_roots_sha256(locked_roots)
                if observed_snapshot_sha256 != expected_snapshot_sha256:
                    raise StoreIntegrityError(
                        "verified run root snapshot changed before budget terminalization"
                    )
                if integrity_guard is not None:
                    integrity_guard(locked_roots)
            current = self._load_manifest_locked(run_id)
            state = self._validated_state_locked(run_id, current)
            if current.status.is_terminal:
                self._validate_terminal_state_locked(run_id, current)
                return EventStoreTerminalResult(
                    manifest=current,
                    events=tuple(self._list_events_locked(run_id)),
                )
            if state.model_request_state != "not_started":
                raise StoreIntegrityError("budget refusal terminalization must precede dispatch")
            used, reserved = self._load_budget_locked(run_id)
            if reserved != BudgetUsage():
                raise StoreIntegrityError("budget refusal cannot retain a reservation")

            for name in sorted(EXPECTED_ARTIFACT_NAMES - set(state.artifacts)):
                if name in state.absent_artifacts:
                    continue
                self._append_locked(
                    run_id=run_id,
                    event_type="artifact.not_produced",
                    payload={"name": name, "reason": "budget_exhausted"},
                )
            self._append_budget_snapshot_locked(run_id, used, reserved)
            safe_finished_at = max(
                finished_at,
                current.started_at or current.created_at,
            )
            final_manifest = _transition_manifest(
                current,
                status=RunStatus.BUDGET_EXHAUSTED,
                finished_at=safe_finished_at,
                terminal_reason=terminal_reason,
            )
            self._append_locked(
                run_id=run_id,
                event_type="run.terminal",
                payload={
                    "status": RunStatus.BUDGET_EXHAUSTED.value,
                    "terminal_reason": terminal_reason,
                },
            )
            self._save_manifest_locked(run_id, final_manifest)
            self._update_budget_locked(run_id, used, reserved)
            self._validate_terminal_state_locked(run_id, final_manifest)
            return EventStoreTerminalResult(
                manifest=final_manifest,
                events=tuple(self._list_events_locked(run_id)),
            )

    def load_manifest(self, run_id: str) -> RunManifest:
        manifest = self._load_manifest_locked(run_id)
        self._validated_state_locked(run_id, manifest)
        if manifest.status.is_terminal:
            self._validate_terminal_state_locked(run_id, manifest)
        return manifest

    def load_budget_state(self, run_id: str) -> tuple[BudgetUsage, BudgetUsage]:
        manifest = self._load_manifest_locked(run_id)
        state = self._validated_state_locked(run_id, manifest)
        return state.budget_used, state.budget_reserved

    def list_events(self, run_id: str) -> list[EventRecord]:
        events = self._list_events_locked(run_id)
        manifest = self._load_manifest_locked(run_id)
        try:
            replay_events(events, require_terminal=manifest.status.is_terminal)
        except ReplayIntegrityError as error:
            raise StoreIntegrityError(f"invalid event stream for run {run_id}: {error}") from error
        self._validated_state_locked(run_id, manifest)
        return events

    def list_run_ids(self) -> tuple[str, ...]:
        rows = self._connection.execute("SELECT run_id FROM runs ORDER BY run_id ASC").fetchall()
        return tuple(row["run_id"] for row in rows)

    def events_jsonl(self, run_id: str) -> str:
        events = self.list_events(run_id)
        return "".join(f"{canonical_json(event)}\n" for event in events)

    def verify_integrity(self) -> tuple[VerifiedRunRoot, ...]:
        """Validate one consistent read-only snapshot of the complete ledger.

        The returned roots are ordered by run ID. Each root binds the validated
        current manifest (and therefore its artifact references) to the exact
        manifest revision and hash-chained event head observed in this snapshot.
        """

        with self.verified_snapshot() as roots:
            return roots

    @contextmanager
    def verified_snapshot(self) -> Iterator[tuple[VerifiedRunRoot, ...]]:
        """Yield validated roots while retaining their consistent read transaction."""

        self._verify_connection_settings()
        if self._connection.in_transaction:
            raise StoreIntegrityError("integrity verification requires an idle connection")
        self._validate_tracked_path()
        try:
            self._connection.execute("BEGIN")
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError(
                "integrity verification could not begin a snapshot"
            ) from error
        try:
            try:
                self._validate_tracked_path()
                if self._existing_path_identity is not None:
                    self._validate_schema_locked()
                roots = self._verified_roots_locked()
            except StoreIntegrityError:
                raise
            except (IndexError, KeyError, ValueError, sqlite3.DatabaseError) as error:
                raise StoreIntegrityError("database integrity validation failed") from error
            yield roots
            self._validate_tracked_path()
        finally:
            if self._connection.in_transaction:
                self._connection.rollback()

    def _verified_roots_locked(self) -> tuple[VerifiedRunRoot, ...]:
        """Validate roots inside the caller's already-active consistent transaction."""

        if not self._connection.in_transaction:
            raise StoreIntegrityError("verified roots require an active transaction")
        self._verify_sqlite_integrity_locked()
        rows = self._connection.execute(
            "SELECT run_id, manifest_revision FROM runs ORDER BY run_id ASC"
        ).fetchall()
        roots: list[VerifiedRunRoot] = []
        for row in rows:
            run_id = str(row["run_id"])
            manifest_revision = int(row["manifest_revision"])
            manifest = self._load_manifest_locked(run_id)
            self._validate_manifest_history_locked(
                run_id,
                current=manifest,
                current_revision=manifest_revision,
            )
            state = self._validated_state_locked(run_id, manifest)
            events = self._list_events_locked(run_id)
            if manifest.status.is_terminal:
                try:
                    replay_events(events, require_terminal=True)
                except ReplayIntegrityError as error:
                    raise StoreIntegrityError(
                        f"incomplete terminal run {run_id}: {error}"
                    ) from error
                if state.terminal_reason != manifest.terminal_reason:
                    raise StoreIntegrityError(
                        f"manifest terminal reason disagrees with replay for run {run_id}"
                    )
            roots.append(
                VerifiedRunRoot(
                    manifest=manifest,
                    manifest_revision=manifest_revision,
                    manifest_sha256=canonical_sha256(manifest),
                    event_count=len(events),
                    head_event_sha256=events[-1].content_hash(),
                )
            )
        return tuple(roots)

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
        try:
            replay_events([*self._list_events_locked(run_id), event])
        except ReplayIntegrityError as error:
            raise StoreIntegrityError(f"illegal {event_type} event: {error}") from error
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

    def _append_budget_snapshot_locked(
        self,
        run_id: str,
        used: BudgetUsage,
        reserved: BudgetUsage,
    ) -> EventRecord:
        return self._append_locked(
            run_id=run_id,
            event_type="budget.snapshot",
            payload={
                "used": used.model_dump(mode="json"),
                "reserved": reserved.model_dump(mode="json"),
            },
        )

    def _save_manifest_locked(self, run_id: str, manifest: RunManifest) -> None:
        if manifest.run_id != run_id:
            raise ValueError("manifest run ID does not match event run ID")
        current = self._load_manifest_locked(run_id)
        _validate_manifest_transition(current, manifest)
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

    def _load_manifest_locked(self, run_id: str) -> RunManifest:
        row = self._connection.execute(
            """
            SELECT status, manifest_revision, manifest_json, manifest_sha256
            FROM runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        manifest = RunManifest.model_validate(json.loads(row["manifest_json"]))
        if canonical_sha256(manifest) != row["manifest_sha256"]:
            raise StoreIntegrityError(f"manifest hash mismatch for run {run_id}")
        if row["status"] != manifest.status.value:
            raise StoreIntegrityError(f"run status index mismatch for run {run_id}")
        history = self._connection.execute(
            """
            SELECT manifest_json, manifest_sha256
            FROM run_manifests WHERE run_id = ? AND revision = ?
            """,
            (run_id, row["manifest_revision"]),
        ).fetchone()
        if history is None:
            raise StoreIntegrityError(f"manifest revision is missing for run {run_id}")
        if (
            history["manifest_json"] != row["manifest_json"]
            or history["manifest_sha256"] != row["manifest_sha256"]
        ):
            raise StoreIntegrityError(f"manifest revision mismatch for run {run_id}")
        return manifest

    def _validate_manifest_history_locked(
        self,
        run_id: str,
        *,
        current: RunManifest,
        current_revision: int,
    ) -> None:
        rows = self._connection.execute(
            """
            SELECT revision, manifest_json, manifest_sha256
            FROM run_manifests WHERE run_id = ? ORDER BY revision ASC
            """,
            (run_id,),
        ).fetchall()
        revisions = tuple(int(row["revision"]) for row in rows)
        history_is_contiguous = (
            current_revision >= 0
            and len(revisions) == current_revision + 1
            and all(revision == expected for expected, revision in enumerate(revisions))
        )
        if not history_is_contiguous:
            raise StoreIntegrityError(f"non-contiguous manifest history for run {run_id}")

        previous: RunManifest | None = None
        for row in rows:
            revision = int(row["revision"])
            raw_manifest = str(row["manifest_json"])
            manifest = RunManifest.model_validate(json.loads(raw_manifest))
            if manifest.run_id != run_id:
                raise StoreIntegrityError(
                    f"manifest history run ID mismatch for run {run_id} at revision {revision}"
                )
            if raw_manifest != canonical_json(manifest):
                raise StoreIntegrityError(
                    f"non-canonical manifest history for run {run_id} at revision {revision}"
                )
            if row["manifest_sha256"] != canonical_sha256(manifest):
                raise StoreIntegrityError(
                    f"manifest history hash mismatch for run {run_id} at revision {revision}"
                )
            if previous is not None:
                try:
                    _validate_manifest_transition(previous, manifest)
                except (StoreIntegrityError, ValueError) as error:
                    raise StoreIntegrityError(
                        f"invalid manifest history transition for run {run_id} "
                        f"at revision {revision}"
                    ) from error
            previous = manifest

        if previous != current:
            raise StoreIntegrityError(f"manifest history head mismatch for run {run_id}")

    def _load_budget_locked(self, run_id: str) -> tuple[BudgetUsage, BudgetUsage]:
        row = self._connection.execute(
            """
            SELECT limits_json, used_json, reserved_json
            FROM budget_state WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        raw_limits = row["limits_json"]
        raw_used = row["used_json"]
        raw_reserved = row["reserved_json"]
        limits = BudgetLimits.model_validate(json.loads(raw_limits))
        used = BudgetUsage.model_validate(json.loads(raw_used))
        reserved = BudgetUsage.model_validate(json.loads(raw_reserved))
        if limits != self._load_manifest_locked(run_id).budget_limits:
            raise StoreIntegrityError(f"budget limits index mismatch for run {run_id}")
        if raw_limits != canonical_json(limits):
            raise StoreIntegrityError(
                f"budget limits index mismatch: non-canonical encoding for run {run_id}"
            )
        if raw_used != canonical_json(used):
            raise StoreIntegrityError(
                f"budget index disagrees: non-canonical usage for run {run_id}"
            )
        if raw_reserved != canonical_json(reserved):
            raise StoreIntegrityError(
                f"budget index disagrees: non-canonical reservation for run {run_id}"
            )
        return used, reserved

    def _list_events_locked(self, run_id: str) -> list[EventRecord]:
        rows = self._connection.execute(
            """
            SELECT run_id, sequence, event_id, event_type, record_json, event_hash
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
            raw_record = row["record_json"]
            event = EventRecord.model_validate(json.loads(raw_record))
            event_hash = event.content_hash()
            if row["run_id"] != run_id or event.run_id != run_id:
                raise StoreIntegrityError(f"event run ownership mismatch for run {run_id}")
            if row["sequence"] != expected_sequence or event.sequence != expected_sequence:
                raise StoreIntegrityError(f"non-contiguous event sequence for run {run_id}")
            if row["event_id"] != event.event_id:
                raise StoreIntegrityError(
                    f"event ID index mismatch at sequence {expected_sequence}"
                )
            if row["event_type"] != event.event_type:
                raise StoreIntegrityError(
                    f"event type index mismatch at sequence {expected_sequence}"
                )
            if raw_record != canonical_json(event):
                raise StoreIntegrityError(
                    f"non-canonical event record at sequence {expected_sequence}"
                )
            if event.previous_event_hash != previous_hash:
                raise StoreIntegrityError(f"broken event predecessor for run {run_id}")
            if event_hash != row["event_hash"]:
                raise StoreIntegrityError(f"event hash mismatch at sequence {expected_sequence}")
            events.append(event)
            previous_hash = event_hash
        return events

    def _validated_state_locked(self, run_id: str, manifest: RunManifest) -> ReplayState:
        events = self._list_events_locked(run_id)
        try:
            state = replay_events(events)
        except ReplayIntegrityError as error:
            raise StoreIntegrityError(f"invalid event stream for run {run_id}: {error}") from error
        if events[0].payload != _manifest_creation_payload(manifest):
            raise StoreIntegrityError("manifest identity disagrees with run.created")
        used, reserved = self._load_budget_locked(run_id)
        if state.status is not manifest.status:
            raise StoreIntegrityError("manifest status disagrees with replayed status")
        if state.budget_used != used or state.budget_reserved != reserved:
            raise StoreIntegrityError("budget index disagrees with replayed budget")
        manifest_artifacts = {
            name: artifact.sha256 for name, artifact in manifest.artifacts.items()
        }
        if state.artifacts != manifest_artifacts:
            raise StoreIntegrityError("manifest artifacts disagree with replayed bindings")
        if state.returned_model != manifest.returned_model:
            raise StoreIntegrityError("manifest returned model disagrees with replay")
        return state

    def _validate_terminal_state_locked(
        self,
        run_id: str,
        manifest: RunManifest,
    ) -> None:
        state = self._validated_state_locked(run_id, manifest)
        try:
            replay_events(self._list_events_locked(run_id), require_terminal=True)
        except ReplayIntegrityError as error:
            raise StoreIntegrityError(f"incomplete terminal run {run_id}: {error}") from error
        if state.terminal_reason != manifest.terminal_reason:
            raise StoreIntegrityError("manifest terminal reason disagrees with replay")

    def _validate_budget_update_locked(
        self,
        run_id: str,
        used: BudgetUsage,
        reserved: BudgetUsage,
    ) -> None:
        current_used, current_reserved = self._load_budget_locked(run_id)
        if used != current_used:
            raise StoreIntegrityError("budget usage changes require a lifecycle operation")
        if reserved != current_reserved:
            raise StoreIntegrityError("reservation changes require a lifecycle operation")

    def _update_budget_locked(
        self,
        run_id: str,
        used: BudgetUsage,
        reserved: BudgetUsage,
    ) -> None:
        self._connection.execute(
            """
            UPDATE budget_state SET used_json = ?, reserved_json = ? WHERE run_id = ?
            """,
            (canonical_json(used), canonical_json(reserved), run_id),
        )

    def _require_run(self, run_id: str) -> None:
        row = self._connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")

    def _configure_connection(self) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = FULL")
        self._connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        self._verify_connection_settings()

    def _configure_read_only_connection(self) -> None:
        try:
            # These are connection-local safeguards; journal mode is intentionally
            # never assigned on the read-only reader.
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA query_only = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError("could not configure read-only SQLite connection") from error
        self._verify_connection_settings()

    def _configure_existing_writable_connection(self) -> None:
        try:
            # Preserve the persisted journal mode: recovery may only proceed if the
            # existing database already satisfies the WAL contract.
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError(
                "could not configure existing writable SQLite connection"
            ) from error
        self._verify_connection_settings()

    def _verify_connection_settings(self) -> None:
        expected_settings = (
            _EXPECTED_READ_ONLY_CONNECTION_SETTINGS
            if self._read_only
            else _EXPECTED_CONNECTION_SETTINGS
        )
        if self._read_only:
            expected_settings = (
                *expected_settings,
                ("journal_mode", self._read_only_journal_mode),
            )
        for pragma, expected in expected_settings:
            try:
                row = self._connection.execute(f"PRAGMA {pragma}").fetchone()
            except sqlite3.DatabaseError as error:
                raise StoreIntegrityError(
                    f"could not query SQLite connection setting {pragma}"
                ) from error
            if row is None:
                raise StoreIntegrityError(f"SQLite connection setting {pragma} has no value")
            actual = row[0]
            normalized = str(actual).lower() if isinstance(expected, str) else actual
            if normalized != expected:
                raise StoreIntegrityError(
                    f"SQLite connection setting {pragma} is {actual!r}; expected {expected!r}"
                )

    def _verify_sqlite_integrity_locked(self) -> None:
        try:
            quick_check = tuple(
                str(row[0]) for row in self._connection.execute("PRAGMA quick_check").fetchall()
            )
            foreign_key_violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError("SQLite structural integrity check failed") from error
        if quick_check != ("ok",):
            raise StoreIntegrityError(
                f"SQLite quick_check failed with {len(quick_check)} finding(s)"
            )
        if foreign_key_violations:
            raise StoreIntegrityError(
                f"SQLite foreign_key_check failed with {len(foreign_key_violations)} violation(s)"
            )

    def _validate_existing_database(self) -> None:
        try:
            self._connection.execute("BEGIN")
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError(
                "existing event store could not begin a read snapshot"
            ) from error
        try:
            self._validate_schema_locked()
            self._verify_sqlite_integrity_locked()
        except StoreIntegrityError:
            raise
        except sqlite3.DatabaseError as error:
            raise StoreIntegrityError("existing event store schema is invalid") from error
        finally:
            self._connection.rollback()

    def _validate_schema_locked(self) -> None:
        """Require the exact schema emitted by the current GuildMind constructor."""

        if not self._connection.in_transaction:
            raise StoreIntegrityError("schema validation requires an active transaction")
        expected_schema_objects: set[tuple[str, str, str, str | None]] = {
            ("table", name, name, sql) for name, sql in _EXPECTED_TABLE_SQL
        }
        for table, indexes in _EXPECTED_TABLE_INDEXES.items():
            expected_schema_objects.update(
                ("index", name, table, None) for name, _, _, _ in indexes
            )
        try:
            observed_schema_objects = {
                tuple(row)
                for row in self._connection.execute(
                    """
                    SELECT type, name, tbl_name, sql
                    FROM main.sqlite_schema
                    """
                ).fetchall()
            }
            if observed_schema_objects != expected_schema_objects:
                raise StoreIntegrityError("existing event store schema is invalid")

            for table, expected_columns in _EXPECTED_TABLE_COLUMNS.items():
                observed_columns = tuple(
                    sorted(
                        (
                            tuple(row)
                            for row in self._connection.execute(
                                f'PRAGMA main.table_xinfo("{table}")'
                            ).fetchall()
                        ),
                        key=lambda row: int(row[0]),
                    )
                )
                if observed_columns != expected_columns:
                    raise StoreIntegrityError("existing event store schema is invalid")

                observed_foreign_keys = tuple(
                    sorted(
                        (
                            tuple(row)
                            for row in self._connection.execute(
                                f'PRAGMA main.foreign_key_list("{table}")'
                            ).fetchall()
                        ),
                        key=lambda row: (int(row[0]), int(row[1])),
                    )
                )
                if observed_foreign_keys != _EXPECTED_FOREIGN_KEYS[table]:
                    raise StoreIntegrityError("existing event store schema is invalid")

                observed_indexes = tuple(
                    sorted(
                        (
                            (str(row[1]), int(row[2]), str(row[3]), int(row[4]))
                            for row in self._connection.execute(
                                f'PRAGMA main.index_list("{table}")'
                            ).fetchall()
                        ),
                        key=lambda row: row[0],
                    )
                )
                if observed_indexes != _EXPECTED_TABLE_INDEXES[table]:
                    raise StoreIntegrityError("existing event store schema is invalid")

            for index, expected_index_columns in _EXPECTED_INDEX_COLUMNS.items():
                observed_index_columns = tuple(
                    sorted(
                        (
                            tuple(row)
                            for row in self._connection.execute(
                                f'PRAGMA main.index_xinfo("{index}")'
                            ).fetchall()
                        ),
                        key=lambda row: int(row[0]),
                    )
                )
                if observed_index_columns != expected_index_columns:
                    raise StoreIntegrityError("existing event store schema is invalid")
        except StoreIntegrityError:
            raise
        except (IndexError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreIntegrityError("existing event store schema is invalid") from error

    @contextmanager
    def _transaction(
        self,
        *,
        precommit_integrity_guard: Callable[[tuple[VerifiedRunRoot, ...]], None] | None = None,
    ) -> Iterator[None]:
        if self._read_only:
            raise StoreIntegrityError("event store is read-only")
        self._validate_tracked_path()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._validate_tracked_path()
            if self._existing_path_identity is not None:
                self._validate_schema_locked()
                self._validate_full_ledger_locked()
                self._validate_tracked_path()
            yield
            self._validate_tracked_path()
            if self._existing_path_identity is not None:
                self._validate_schema_locked()
                final_roots = self._validate_full_ledger_locked()
            else:
                final_roots = self._verified_roots_locked()
            if precommit_integrity_guard is not None:
                precommit_integrity_guard(final_roots)
            self._validate_tracked_path()
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _validate_full_ledger_locked(self) -> tuple[VerifiedRunRoot, ...]:
        try:
            return self._verified_roots_locked()
        except StoreIntegrityError:
            raise
        except (IndexError, KeyError, TypeError, ValueError, sqlite3.DatabaseError) as error:
            raise StoreIntegrityError("database integrity validation failed") from error

    def _initialize_schema(self) -> None:
        self._connection.executescript(_SCHEMA_INITIALIZATION_SQL)
        self._connection.commit()

    def _validate_tracked_path(self) -> None:
        if self._trusted_base is None or self._existing_path_identity is None:
            return
        _validate_existing_database_path(
            self.path,
            trusted_base=self._trusted_base,
            expected_identity=self._existing_path_identity,
        )
        _validate_persisted_wal_header(self.path, self._existing_path_identity)
        _inspect_existing_sidecars(self.path)


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonicalize_existing_database_path(
    path: Path,
    *,
    trusted_base: Path | None,
) -> tuple[Path, Path]:
    configured_path = Path(os.path.abspath(path))
    configured_base = Path(
        os.path.abspath(trusted_base if trusted_base is not None else configured_path.parent)
    )
    try:
        relative_path = configured_path.relative_to(configured_base)
    except ValueError as error:
        raise ValueError("event store path must be below its trusted base") from error
    if not relative_path.parts:
        raise ValueError("event store path must be below its trusted base")
    try:
        resolved_base = configured_base.resolve(strict=True)
    except OSError as error:
        raise StoreIntegrityError("existing event store trusted base is unavailable") from error
    return resolved_base.joinpath(relative_path), resolved_base


def _validate_existing_database_path(
    path: Path,
    *,
    trusted_base: Path,
    expected_identity: _DatabasePathIdentity | None = None,
    allow_empty: bool = False,
) -> _DatabasePathIdentity:
    try:
        relative_path = path.relative_to(trusted_base)
    except ValueError as error:
        raise StoreIntegrityError("existing event store escaped its trusted base") from error
    current = trusted_base
    directories = [current]
    for component in relative_path.parts[:-1]:
        current /= component
        directories.append(current)
    directory_identities: list[_DatabaseFileIdentity] = []
    for directory in directories:
        try:
            metadata = directory.lstat()
        except FileNotFoundError as error:
            raise StoreIntegrityError("existing event store parent directory is missing") from error
        except OSError as error:
            raise StoreIntegrityError(
                "existing event store parent directory could not be inspected"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise StoreIntegrityError(
                "existing event store parent path must contain only real directories"
            )
        directory_identities.append(
            _DatabaseFileIdentity(device=metadata.st_dev, inode=metadata.st_ino)
        )
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise StoreIntegrityError("existing event store does not exist") from error
    except OSError as error:
        raise StoreIntegrityError("existing event store could not be inspected") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise StoreIntegrityError("existing event store must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise StoreIntegrityError("existing event store must be a regular file")
    if metadata.st_size == 0 and not allow_empty:
        raise StoreIntegrityError("existing event store must not be empty")
    if metadata.st_nlink != 1:
        raise StoreIntegrityError("existing event store must not be hard linked")
    identity = _DatabasePathIdentity(
        directory_identities=tuple(directory_identities),
        leaf_identity=_DatabaseFileIdentity(device=metadata.st_dev, inode=metadata.st_ino),
    )
    if expected_identity is not None and identity != expected_identity:
        raise StoreIntegrityError(
            "existing event store path changed while it was being opened or used"
        )
    return identity


def _create_database_exclusively(path: Path) -> _DatabaseFileIdentity | None:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return None
    except OSError as error:
        raise StoreIntegrityError("new event store could not be created safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise StoreIntegrityError("new event store is not an exclusively owned regular file")
        identity = _DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
        _fsync_directory(path.parent)
        return identity
    except OSError as error:
        raise StoreIntegrityError("new event store could not be inspected safely") from error
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StoreIntegrityError(
            "new event store parent directory could not be opened for synchronization"
        ) from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise StoreIntegrityError(
            "new event store parent directory could not be synchronized"
        ) from error
    finally:
        os.close(descriptor)


def _preflight_missing_database_sidecars(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        sidecars = _inspect_existing_sidecars(path)
        if sidecars.wal_identity is not None or sidecars.shm_identity is not None:
            raise StoreIntegrityError(
                "new event store path has pre-existing SQLite sidecars"
            ) from None
    except OSError as error:
        raise StoreIntegrityError("event store path could not be inspected safely") from error


def _validate_persisted_wal_header(
    path: Path,
    expected_identity: _DatabasePathIdentity,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise StoreIntegrityError(
            "existing event store header could not be opened safely"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        observed_identity = _DatabaseFileIdentity(metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or observed_identity != expected_identity.leaf_identity
        ):
            raise StoreIntegrityError("existing event store changed while reading its header")
        header = os.read(descriptor, 100)
    except OSError as error:
        raise StoreIntegrityError("existing event store header could not be read safely") from error
    finally:
        os.close(descriptor)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise StoreIntegrityError("existing event store has an invalid SQLite header")
    if header[18:20] != b"\x02\x02":
        raise StoreIntegrityError(
            "existing event store journal_mode is not persistently configured for WAL"
        )


def _inspect_existing_sidecars(path: Path) -> _SidecarState:
    wal = _optional_regular_file(Path(f"{path}-wal"), "WAL")
    shm = _optional_regular_file(Path(f"{path}-shm"), "SHM")
    rollback_journal = _optional_regular_file(Path(f"{path}-journal"), "rollback journal")
    if rollback_journal is not None:
        raise StoreIntegrityError("existing event store requires rollback-journal recovery")
    if wal is None and shm is not None:
        raise StoreIntegrityError("existing SHM sidecar has no matching WAL")
    if wal is not None and (shm is None or shm[1] == 0):
        raise StoreIntegrityError("existing WAL requires an existing usable SHM sidecar")
    return _SidecarState(
        wal_identity=None if wal is None else wal[0],
        wal_size=None if wal is None else wal[1],
        shm_identity=None if shm is None else shm[0],
        shm_size=None if shm is None else shm[1],
    )


def _optional_regular_file(
    path: Path,
    label: str,
) -> tuple[_DatabaseFileIdentity, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StoreIntegrityError(f"existing event store {label} could not be inspected") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise StoreIntegrityError(f"existing event store {label} must be a regular file")
    if metadata.st_nlink != 1:
        raise StoreIntegrityError(f"existing event store {label} must not be hard linked")
    return _DatabaseFileIdentity(metadata.st_dev, metadata.st_ino), metadata.st_size


def _transition_manifest(manifest: RunManifest, **changes: object) -> RunManifest:
    return RunManifest.model_validate({**manifest.model_dump(), **changes})


def _validate_manifest_transition(current: RunManifest, proposed: RunManifest) -> None:
    if current.run_id != proposed.run_id:
        raise ValueError("manifest run ID cannot change")
    current_fixed = current.model_dump(exclude=_MANIFEST_MUTABLE_FIELDS)
    proposed_fixed = proposed.model_dump(exclude=_MANIFEST_MUTABLE_FIELDS)
    if current_fixed != proposed_fixed:
        raise StoreIntegrityError("immutable manifest identity changed")
    if current.status.is_terminal:
        raise StoreIntegrityError("terminal manifests cannot transition")
    if current.status is RunStatus.PENDING:
        if proposed.status is RunStatus.SUCCEEDED:
            raise StoreIntegrityError("pending manifests cannot succeed without running")
    elif current.status is RunStatus.RUNNING:
        if proposed.status is RunStatus.PENDING:
            raise StoreIntegrityError("running manifests cannot return to pending")
    else:
        raise StoreIntegrityError(f"unsupported manifest state: {current.status.value}")
    if current.started_at is not None and proposed.started_at != current.started_at:
        raise StoreIntegrityError("manifest start timestamp cannot change")
    if current.returned_model is not None and proposed.returned_model != current.returned_model:
        raise StoreIntegrityError("returned model cannot be rebound")
    for name, reference in current.artifacts.items():
        if proposed.artifacts.get(name) != reference:
            raise StoreIntegrityError(f"manifest artifact cannot be removed or rebound: {name}")


def _artifact_payload(name: str, artifact: ArtifactRef) -> dict[str, JsonValue]:
    return {
        "media_type": artifact.media_type,
        "name": name,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "storage_ref": artifact.storage_ref,
    }


def _manifest_creation_payload(manifest: RunManifest) -> dict[str, JsonValue]:
    return {
        "budget_limits": manifest.budget_limits.model_dump(mode="json"),
        "candidate_id": manifest.candidate_id,
        "code_revision": manifest.code_revision,
        "environment_digest": manifest.environment_digest,
        "experiment_id": manifest.experiment_id,
        "genome_hash": manifest.genome_hash,
        "model_parameters": manifest.model_parameters,
        "requested_model": manifest.requested_model,
        "seed": manifest.seed,
        "status": RunStatus.PENDING.value,
        "task_id": manifest.task_id,
    }


def _add_usage(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
    return BudgetUsage.model_validate(
        {name: getattr(left, name) + getattr(right, name) for name in _USAGE_FIELDS}
    )


def _subtract_usage(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
    values = {name: getattr(left, name) - getattr(right, name) for name in _USAGE_FIELDS}
    if any(value < 0 for value in values.values()):
        raise StoreIntegrityError("budget reservation reconciliation underflowed")
    return BudgetUsage.model_validate(values)


def _usage_at_most(actual: BudgetUsage, ceiling: BudgetUsage) -> bool:
    return all(getattr(actual, name) <= getattr(ceiling, name) for name in _USAGE_FIELDS)


def _terminal_status_matches_outcome(status: RunStatus, outcome: str) -> bool:
    if outcome == "passed":
        return status is RunStatus.SUCCEEDED
    if outcome == "failed":
        return status in {RunStatus.FAILED, RunStatus.TIMED_OUT}
    if outcome == "error":
        return status is RunStatus.INFRASTRUCTURE_ERROR
    return False
