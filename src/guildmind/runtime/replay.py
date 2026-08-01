"""Event-chain verification and deterministic semantic replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import JsonValue, ValidationError

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EventRecord,
    RunStatus,
    canonical_sha256,
)

EXPECTED_ARTIFACT_NAMES = frozenset(
    {
        "evaluation",
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
        "task_spec",
    }
)
OPTIONAL_EVALUATION_ARTIFACT_NAMES = frozenset(
    {
        "evaluation_candidate_stdout",
        "evaluation_scorer_stdout",
    }
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


class ReplayIntegrityError(RuntimeError):
    """Raised when an event stream violates its hash chain or lifecycle protocol."""


ModelRequestState = Literal["not_started", "started", "completed", "ambiguous"]


@dataclass
class ReplayState:
    run_id: str
    status: RunStatus = RunStatus.PENDING
    budget_used: BudgetUsage = field(default_factory=BudgetUsage)
    budget_reserved: BudgetUsage = field(default_factory=BudgetUsage)
    artifacts: dict[str, str] = field(default_factory=dict)
    absent_artifacts: dict[str, str] = field(default_factory=dict)
    evaluation_outcome: str | None = None
    requested_model: str | None = None
    model_request_id: str | None = None
    model_request_state: ModelRequestState = "not_started"
    model_request_maximum: BudgetUsage | None = None
    returned_model: str | None = None
    terminal_reason: str | None = None
    event_count: int = 0


@dataclass(frozen=True, slots=True)
class _BudgetTransition:
    kind: Literal["reserve", "reconcile", "ambiguous"]
    used_before: BudgetUsage
    reserved_before: BudgetUsage
    amount: BudgetUsage


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
            if event.causal_parent_ids:
                raise ReplayIntegrityError("first event has a causal parent")
        else:
            if event.previous_event_hash != previous.content_hash():
                raise ReplayIntegrityError(f"broken hash chain at sequence {event.sequence}")
            if event.causal_parent_ids != (previous.event_id,):
                raise ReplayIntegrityError(
                    f"unexpected causal parents at sequence {event.sequence}"
                )
        seen_event_ids.add(event.event_id)
        previous = event


def _usage_from_payload(payload: dict[str, JsonValue], key: str) -> BudgetUsage:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ReplayIntegrityError(f"event payload is missing {key}")
    try:
        return BudgetUsage.model_validate(value)
    except ValidationError as error:
        raise ReplayIntegrityError(f"event payload contains invalid {key}") from error


def _usage_values(usage: BudgetUsage) -> dict[str, int | float]:
    return {name: getattr(usage, name) for name in _USAGE_FIELDS}


def _add_usage(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
    return BudgetUsage.model_validate(
        {name: getattr(left, name) + getattr(right, name) for name in _USAGE_FIELDS}
    )


def _subtract_usage(left: BudgetUsage, right: BudgetUsage) -> BudgetUsage:
    values = {name: getattr(left, name) - getattr(right, name) for name in _USAGE_FIELDS}
    if any(value < 0 for value in values.values()):
        raise ReplayIntegrityError("budget reservation reconciliation underflowed")
    return BudgetUsage.model_validate(values)


def _usage_at_most(actual: BudgetUsage, ceiling: BudgetUsage) -> bool:
    return all(getattr(actual, name) <= getattr(ceiling, name) for name in _USAGE_FIELDS)


def _usage_is_nondecreasing(previous: BudgetUsage, current: BudgetUsage) -> bool:
    return _usage_at_most(previous, current)


def _require_nonempty_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReplayIntegrityError(f"event payload is missing {key}")
    return value


def _require_sha256(payload: dict[str, JsonValue], key: str) -> str:
    digest = _require_nonempty_string(payload, key)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ReplayIntegrityError(f"event payload has an invalid {key}")
    return digest


def _require_artifact_digest(payload: dict[str, JsonValue]) -> str:
    return _require_sha256(payload, "sha256")


def _validate_artifact_payload(payload: dict[str, JsonValue]) -> None:
    try:
        reference = ArtifactRef.model_validate(
            {
                "media_type": payload.get("media_type"),
                "sha256": payload.get("sha256"),
                "size_bytes": payload.get("size_bytes"),
                "storage_ref": payload.get("storage_ref"),
            }
        )
    except ValidationError as error:
        raise ReplayIntegrityError("artifact event has invalid reference metadata") from error
    expected_storage_ref = f"sha256/{reference.sha256[:2]}/{reference.sha256}"
    if reference.storage_ref != expected_storage_ref:
        raise ReplayIntegrityError("artifact event storage reference disagrees with its digest")


def _validate_creation_payload(payload: dict[str, JsonValue]) -> None:
    for key in (
        "candidate_id",
        "code_revision",
        "experiment_id",
        "requested_model",
        "task_id",
    ):
        _require_nonempty_string(payload, key)
    environment_digest = _require_nonempty_string(payload, "environment_digest")
    if not environment_digest.startswith("sha256:"):
        raise ReplayIntegrityError("run.created has an invalid environment digest")
    raw_environment_digest = environment_digest.removeprefix("sha256:")
    if len(raw_environment_digest) != 64 or any(
        character not in "0123456789abcdef" for character in raw_environment_digest
    ):
        raise ReplayIntegrityError("run.created has an invalid environment digest")
    genome_hash = payload.get("genome_hash")
    if genome_hash is not None and (
        not isinstance(genome_hash, str)
        or len(genome_hash) != 64
        or any(character not in "0123456789abcdef" for character in genome_hash)
    ):
        raise ReplayIntegrityError("run.created has an invalid genome hash")
    seed = payload.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ReplayIntegrityError("run.created has an invalid seed")
    if not isinstance(payload.get("model_parameters"), dict):
        raise ReplayIntegrityError("run.created has invalid model parameters")
    raw_limits = payload.get("budget_limits")
    if not isinstance(raw_limits, dict):
        raise ReplayIntegrityError("run.created has invalid budget limits")
    try:
        BudgetLimits.model_validate(raw_limits)
    except ValidationError as error:
        raise ReplayIntegrityError("run.created has invalid budget limits") from error


def _apply_budget_snapshot(
    state: ReplayState,
    payload: dict[str, JsonValue],
    transition: _BudgetTransition | None,
) -> None:
    used = _usage_from_payload(payload, "used")
    reserved = _usage_from_payload(payload, "reserved")
    if not _usage_is_nondecreasing(state.budget_used, used):
        raise ReplayIntegrityError("budget usage decreased")

    if transition is None:
        if used != state.budget_used or reserved != state.budget_reserved:
            raise ReplayIntegrityError("budget changed without a lifecycle event")
    elif transition.kind == "reserve":
        expected_used = transition.used_before
        expected_reserved = _add_usage(transition.reserved_before, transition.amount)
        if used != expected_used or reserved != expected_reserved:
            raise ReplayIntegrityError("model reservation snapshot does not match its maximum")
    elif transition.kind == "reconcile":
        expected_used = _add_usage(transition.used_before, transition.amount)
        maximum = state.model_request_maximum
        if maximum is None:
            raise ReplayIntegrityError("model response has no recorded maximum")
        expected_reserved = _subtract_usage(transition.reserved_before, maximum)
        if used != expected_used or reserved != expected_reserved:
            raise ReplayIntegrityError("model reconciliation snapshot is inconsistent")
    else:
        expected_used = _add_usage(transition.used_before, transition.reserved_before)
        if used != expected_used or reserved != BudgetUsage():
            raise ReplayIntegrityError("ambiguous request was not conservatively charged")

    state.budget_used = used
    state.budget_reserved = reserved


def replay_events(
    events: list[EventRecord],
    *,
    require_terminal: bool = False,
) -> ReplayState:
    """Replay and validate one run stream.

    Incomplete prefixes are valid by default because they are exactly what crash
    recovery consumes. ``require_terminal=True`` additionally requires a terminal,
    fully accounted stream with no outstanding reservation.
    """

    if not events:
        raise ReplayIntegrityError("cannot replay an empty event stream")
    verify_event_chain(events)
    state = ReplayState(run_id=events[0].run_id)
    created = False
    pending_budget: _BudgetTransition | None = None

    for event in events:
        if event.run_id != state.run_id:
            raise ReplayIntegrityError("event stream contains multiple run IDs")
        if state.status.is_terminal:
            raise ReplayIntegrityError("event appears after the terminal transition")
        if pending_budget is not None and event.event_type != "budget.snapshot":
            raise ReplayIntegrityError(
                f"{pending_budget.kind} must be followed by a budget snapshot"
            )

        if event.event_type == "run.created":
            if created or event.sequence != 0:
                raise ReplayIntegrityError("run.created must be the first and only creation event")
            if event.payload.get("status") != RunStatus.PENDING.value:
                raise ReplayIntegrityError("run.created must declare pending status")
            _validate_creation_payload(event.payload)
            state.requested_model = _require_nonempty_string(
                event.payload,
                "requested_model",
            )
            created = True
        elif not created:
            raise ReplayIntegrityError("event stream does not begin with run.created")
        elif event.event_type == "run.started":
            if state.status is not RunStatus.PENDING:
                raise ReplayIntegrityError("run.started is only legal from pending")
            requested_model = _require_nonempty_string(event.payload, "requested_model")
            if requested_model != state.requested_model:
                raise ReplayIntegrityError("run.started model disagrees with run.created")
            state.status = RunStatus.RUNNING
        elif event.event_type == "model.request_started":
            if state.status is not RunStatus.RUNNING:
                raise ReplayIntegrityError("model requests require a running run")
            if state.model_request_state != "not_started":
                raise ReplayIntegrityError("a model request has already been recorded")
            if set(state.artifacts) != {"task_spec"}:
                raise ReplayIntegrityError("model request requires exactly one task_spec artifact")
            state.model_request_id = _require_nonempty_string(event.payload, "request_id")
            state.model_request_maximum = _usage_from_payload(event.payload, "maximum")
            if state.model_request_maximum.model_calls != 1:
                raise ReplayIntegrityError("one model request must reserve exactly one call")
            state.model_request_state = "started"
            pending_budget = _BudgetTransition(
                kind="reserve",
                used_before=state.budget_used,
                reserved_before=state.budget_reserved,
                amount=state.model_request_maximum,
            )
        elif event.event_type == "model.response_completed":
            if state.status is not RunStatus.RUNNING:
                raise ReplayIntegrityError("model responses require a running run")
            if state.model_request_state != "started":
                raise ReplayIntegrityError("model response has no outstanding request")
            request_id = _require_nonempty_string(event.payload, "request_id")
            if request_id != state.model_request_id:
                raise ReplayIntegrityError("model response request ID does not match")
            actual = _usage_from_payload(event.payload, "actual")
            if actual.model_calls != 1:
                raise ReplayIntegrityError("one model response must report exactly one call")
            maximum = state.model_request_maximum
            if maximum is None or not _usage_at_most(actual, maximum):
                raise ReplayIntegrityError("model response exceeds its reservation")
            state.returned_model = _require_nonempty_string(event.payload, "returned_model")
            state.model_request_state = "completed"
            pending_budget = _BudgetTransition(
                kind="reconcile",
                used_before=state.budget_used,
                reserved_before=state.budget_reserved,
                amount=actual,
            )
        elif event.event_type == "model.request_ambiguous":
            if state.status is not RunStatus.RUNNING:
                raise ReplayIntegrityError("ambiguous model work requires a running run")
            if state.model_request_state != "started":
                raise ReplayIntegrityError("no outstanding model request can be ambiguous")
            request_id = _require_nonempty_string(event.payload, "request_id")
            if request_id != state.model_request_id:
                raise ReplayIntegrityError("ambiguous model request ID does not match")
            state.model_request_state = "ambiguous"
            pending_budget = _BudgetTransition(
                kind="ambiguous",
                used_before=state.budget_used,
                reserved_before=state.budget_reserved,
                amount=state.budget_reserved,
            )
        elif event.event_type == "budget.snapshot":
            _apply_budget_snapshot(state, event.payload, pending_budget)
            pending_budget = None
        elif event.event_type == "artifact.recorded":
            if state.status is not RunStatus.RUNNING:
                raise ReplayIntegrityError("artifacts can only be bound to a running run")
            name = _require_nonempty_string(event.payload, "name")
            digest = _require_artifact_digest(event.payload)
            if name in state.artifacts or name in state.absent_artifacts:
                raise ReplayIntegrityError(f"artifact name is already bound: {name}")
            _validate_artifact_payload(event.payload)
            _validate_artifact_position(state, name)
            state.artifacts[name] = digest
        elif event.event_type == "artifact.not_produced":
            name = _require_nonempty_string(event.payload, "name")
            reason = _require_nonempty_string(event.payload, "reason")
            if name not in EXPECTED_ARTIFACT_NAMES:
                raise ReplayIntegrityError(f"unknown absent artifact role: {name}")
            if name in state.artifacts or name in state.absent_artifacts:
                raise ReplayIntegrityError(f"artifact name is already accounted for: {name}")
            state.absent_artifacts[name] = reason
        elif event.event_type == "evaluation.completed":
            if state.status is not RunStatus.RUNNING:
                raise ReplayIntegrityError("evaluations require a running run")
            if state.model_request_state != "completed":
                raise ReplayIntegrityError("evaluation requires a completed model response")
            if state.evaluation_outcome is not None:
                raise ReplayIntegrityError("an evaluation has already been recorded")
            required = {"evaluation", "evaluation_stderr", "evaluation_stdout", "patch"}
            if not required.issubset(state.artifacts):
                raise ReplayIntegrityError("evaluation evidence is not fully bound")
            outcome = _require_nonempty_string(event.payload, "outcome")
            if outcome not in {"passed", "failed", "error"}:
                raise ReplayIntegrityError("evaluation has an unknown outcome")
            _require_sha256(event.payload, "result_sha256")
            state.evaluation_outcome = outcome
        elif event.event_type == "run.terminal":
            raw_status = _require_nonempty_string(event.payload, "status")
            try:
                terminal_status = RunStatus(raw_status)
            except ValueError as error:
                raise ReplayIntegrityError(f"unknown terminal run status: {raw_status}") from error
            if not terminal_status.is_terminal:
                raise ReplayIntegrityError("run.terminal cannot contain a nonterminal status")
            if state.model_request_state == "started":
                raise ReplayIntegrityError(
                    "outstanding model work must be classified before terminal"
                )
            if (
                terminal_status
                in {
                    RunStatus.SUCCEEDED,
                    RunStatus.FAILED,
                    RunStatus.TIMED_OUT,
                }
                and state.evaluation_outcome is None
            ):
                raise ReplayIntegrityError("evaluation-derived terminal status lacks an evaluation")
            declared_outcome = event.payload.get("evaluation_outcome")
            if state.evaluation_outcome is None:
                if declared_outcome is not None:
                    raise ReplayIntegrityError("terminal event invents an evaluation outcome")
            elif declared_outcome != state.evaluation_outcome:
                raise ReplayIntegrityError("terminal outcome disagrees with the evaluation")
            if not _terminal_status_matches_outcome(
                terminal_status,
                state.evaluation_outcome,
            ):
                raise ReplayIntegrityError("terminal status disagrees with the evaluation outcome")
            raw_terminal_reason = event.payload.get("terminal_reason")
            if raw_terminal_reason is not None and (
                not isinstance(raw_terminal_reason, str) or not raw_terminal_reason.strip()
            ):
                raise ReplayIntegrityError("terminal_reason must be a nonempty string or null")
            terminal_reason = raw_terminal_reason if isinstance(raw_terminal_reason, str) else None
            if terminal_status is RunStatus.SUCCEEDED:
                if terminal_reason is not None:
                    raise ReplayIntegrityError("successful runs cannot have a terminal reason")
            elif terminal_reason is None:
                raise ReplayIntegrityError("non-success terminal runs require a terminal reason")
            if state.model_request_state == "ambiguous":
                if terminal_status is not RunStatus.INFRASTRUCTURE_ERROR:
                    raise ReplayIntegrityError("ambiguous model work is an infrastructure error")
                if terminal_reason != "ambiguous_model_request":
                    raise ReplayIntegrityError("ambiguous model work has the wrong terminal reason")
            state.status = terminal_status
            state.terminal_reason = terminal_reason
        else:
            raise ReplayIntegrityError(f"unknown event type: {event.event_type}")
        state.event_count += 1

    if not created:
        raise ReplayIntegrityError("event stream does not contain run.created")
    if require_terminal:
        if pending_budget is not None:
            raise ReplayIntegrityError("terminal stream has an unapplied budget transition")
        if not state.status.is_terminal:
            raise ReplayIntegrityError("event stream is not terminal")
        if state.budget_reserved != BudgetUsage():
            raise ReplayIntegrityError("terminal stream retains a budget reservation")
        if state.model_request_state == "started":
            raise ReplayIntegrityError("terminal stream has ambiguous unclassified model work")
        accounted = set(state.artifacts) | set(state.absent_artifacts)
        missing = EXPECTED_ARTIFACT_NAMES - accounted
        if missing:
            raise ReplayIntegrityError(
                f"terminal stream does not account for artifacts: {', '.join(sorted(missing))}"
            )
    return state


def semantic_digest(events: list[EventRecord]) -> str:
    """Hash treatment-relevant event content, excluding run IDs and timestamps."""
    replay_events(events)
    normalized: list[dict[str, Any]] = []
    for event in events:
        payload = dict(event.payload)
        # The evaluation artifact is an evidence envelope containing run ID and wall
        # time. Its result hash is captured by ``evaluation.completed``; retaining
        # this envelope's content hash would make otherwise identical runs differ.
        if event.event_type == "artifact.recorded" and payload.get("name") == "evaluation":
            payload.pop("sha256", None)
            payload.pop("size_bytes", None)
            payload.pop("storage_ref", None)
        normalized.append(
            {
                "event_type": event.event_type,
                "event_version": event.event_version,
                "payload": payload,
            }
        )
    return canonical_sha256(normalized)


def _terminal_status_matches_outcome(
    status: RunStatus,
    outcome: str | None,
) -> bool:
    if outcome is None:
        return status in {
            RunStatus.CANCELLED,
            RunStatus.BUDGET_EXHAUSTED,
            RunStatus.INFRASTRUCTURE_ERROR,
        }
    if outcome == "passed":
        return status is RunStatus.SUCCEEDED
    if outcome == "failed":
        return status in {RunStatus.FAILED, RunStatus.TIMED_OUT}
    if outcome == "error":
        return status is RunStatus.INFRASTRUCTURE_ERROR
    return False


def _validate_artifact_position(state: ReplayState, name: str) -> None:
    evaluation_evidence_predecessors = {
        "evaluation_stderr",
        "evaluation_stdout",
        "patch",
        "task_spec",
    }
    expected_predecessors: dict[str, set[str]] = {
        "task_spec": set(),
        "patch": {"task_spec"},
        "evaluation_stdout": {"patch", "task_spec"},
        "evaluation_stderr": {"evaluation_stdout", "patch", "task_spec"},
    }
    recorded = set(state.artifacts)
    if name in expected_predecessors:
        correctly_positioned = recorded == expected_predecessors[name]
    elif name == "evaluation_candidate_stdout":
        correctly_positioned = recorded == evaluation_evidence_predecessors
    elif name == "evaluation_scorer_stdout":
        correctly_positioned = recorded == (
            evaluation_evidence_predecessors | {"evaluation_candidate_stdout"}
        )
    elif name == "evaluation":
        correctly_positioned = evaluation_evidence_predecessors.issubset(recorded) and (
            recorded - evaluation_evidence_predecessors
        ).issubset(OPTIONAL_EVALUATION_ARTIFACT_NAMES)
    else:
        raise ReplayIntegrityError(f"unknown artifact role: {name}")
    if not correctly_positioned:
        raise ReplayIntegrityError(f"artifact role is out of phase: {name}")
    if name == "task_spec" and state.model_request_state != "not_started":
        raise ReplayIntegrityError("task_spec must precede model work")
    if name != "task_spec" and state.model_request_state != "completed":
        raise ReplayIntegrityError(f"artifact requires a completed model response: {name}")
