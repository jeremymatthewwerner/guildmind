"""Event-chain verification and deterministic semantic replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue

from guildmind.domain import BudgetUsage, EventRecord, RunStatus, canonical_sha256


class ReplayIntegrityError(RuntimeError):
    pass


@dataclass
class ReplayState:
    run_id: str
    status: RunStatus = RunStatus.PENDING
    budget_used: BudgetUsage = field(default_factory=BudgetUsage)
    budget_reserved: BudgetUsage = field(default_factory=BudgetUsage)
    artifacts: dict[str, str] = field(default_factory=dict)
    evaluation_outcome: str | None = None
    event_count: int = 0


def verify_event_chain(events: list[EventRecord]) -> None:
    previous: EventRecord | None = None
    seen_event_ids: set[str] = set()
    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise ReplayIntegrityError(
                f"expected event sequence {expected_sequence}, got {event.sequence}"
            )
        if event.event_id in seen_event_ids:
            raise ReplayIntegrityError(f"duplicate event ID: {event.event_id}")
        if previous is None:
            if event.previous_event_hash is not None:
                raise ReplayIntegrityError("first event has a predecessor")
        elif event.previous_event_hash != previous.content_hash():
            raise ReplayIntegrityError(f"broken hash chain at sequence {event.sequence}")
        seen_event_ids.add(event.event_id)
        previous = event


def _usage_from_payload(payload: dict[str, JsonValue], key: str) -> BudgetUsage:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ReplayIntegrityError(f"event payload is missing {key}")
    return BudgetUsage.model_validate(value)


def replay_events(events: list[EventRecord]) -> ReplayState:
    if not events:
        raise ReplayIntegrityError("cannot replay an empty event stream")
    verify_event_chain(events)
    state = ReplayState(run_id=events[0].run_id)
    for event in events:
        if event.run_id != state.run_id:
            raise ReplayIntegrityError("event stream contains multiple run IDs")
        if event.event_type == "run.started":
            state.status = RunStatus.RUNNING
        elif event.event_type == "budget.snapshot":
            state.budget_used = _usage_from_payload(event.payload, "used")
            state.budget_reserved = _usage_from_payload(event.payload, "reserved")
        elif event.event_type == "artifact.recorded":
            name = event.payload.get("name")
            digest = event.payload.get("sha256")
            if not isinstance(name, str) or not isinstance(digest, str):
                raise ReplayIntegrityError("artifact event is malformed")
            state.artifacts[name] = digest
        elif event.event_type == "evaluation.completed":
            outcome = event.payload.get("outcome")
            if not isinstance(outcome, str):
                raise ReplayIntegrityError("evaluation event is missing an outcome")
            state.evaluation_outcome = outcome
        elif event.event_type == "run.terminal":
            raw_status = event.payload.get("status")
            if not isinstance(raw_status, str):
                raise ReplayIntegrityError("terminal event is missing status")
            state.status = RunStatus(raw_status)
        state.event_count += 1
    return state


def semantic_digest(events: list[EventRecord]) -> str:
    """Hash treatment-relevant event content, excluding run IDs and timestamps."""
    verify_event_chain(events)
    normalized: list[dict[str, Any]] = []
    for event in events:
        payload = dict(event.payload)
        # The evaluation artifact is an evidence envelope containing run ID and wall
        # time. Its result hash is captured by ``evaluation.completed``; retaining
        # this envelope's content hash would make otherwise identical runs differ.
        if event.event_type == "artifact.recorded" and payload.get("name") == "evaluation":
            payload.pop("sha256", None)
            payload.pop("size_bytes", None)
        normalized.append(
            {
                "event_type": event.event_type,
                "event_version": event.event_version,
                "payload": payload,
            }
        )
    return canonical_sha256(normalized)
