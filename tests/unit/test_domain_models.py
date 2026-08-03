from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import JsonValue, ValidationError

from guildmind.domain import (
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    EvaluationResult,
    EventRecord,
    ExperimentSpec,
    RunManifest,
    RunStatus,
    TaskSpec,
    canonical_json,
    canonical_sha256,
    export_json_schemas,
    sha256_bytes,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
DIGEST_A = f"sha256:{HASH_A}"
CREATED_AT = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)


def artifact(*, digest: str = HASH_A) -> ArtifactRef:
    return ArtifactRef(
        media_type="application/json",
        size_bytes=12,
        sha256=digest,
        storage_ref=f"cas/{digest}",
    )


def manifest(**overrides: object) -> RunManifest:
    values: dict[str, object] = {
        "run_id": "run-001",
        "experiment_id": "experiment-001",
        "task_id": "task-001",
        "candidate_id": "solo-v0",
        "requested_model": "fake-model-v1",
        "seed": 7,
        "environment_digest": DIGEST_A,
        "code_revision": "abc123",
        "budget_limits": BudgetLimits(max_total_tokens=1_000),
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return RunManifest.model_validate(values)


def test_canonical_json_and_hash_are_deterministic() -> None:
    local_time = datetime(2026, 7, 31, 12, 30, tzinfo=timezone(timedelta(hours=2)))
    value = {"z": 3, "unicode": "Grüße", "at": local_time}

    encoded = canonical_json(value)

    assert encoded == '{"at":"2026-07-31T10:30:00.000000Z","unicode":"Grüße","z":3}'
    assert canonical_sha256(value) == sha256_bytes(encoded.encode("utf-8"))
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})


def test_artifact_is_strict_frozen_and_forbids_extra_fields() -> None:
    value = artifact()

    assert value.schema_version == "0.1"
    with pytest.raises(ValidationError, match="frozen"):
        value.size_bytes = 13  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ArtifactRef.model_validate(
            {
                "media_type": "text/plain",
                "size_bytes": 1,
                "sha256": HASH_A,
                "storage_ref": "cas/a",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        ArtifactRef(
            media_type="text/plain",
            size_bytes=True,
            sha256=HASH_A,
            storage_ref="cas/a",
        )


def test_budget_usage_enforces_constraints_and_reports_exceeded_caps() -> None:
    usage = BudgetUsage(
        uncached_input_tokens=40,
        cache_read_input_tokens=10,
        output_tokens=20,
        reasoning_tokens=5,
        model_calls=2,
        elapsed_seconds=3.0,
    )

    assert usage.input_tokens == 50
    assert usage.total_tokens == 70
    assert usage.exceeded_limits(
        BudgetLimits(max_total_tokens=60, max_model_calls=1, max_elapsed_seconds=3.0)
    ) == ("total_tokens", "model_calls")

    with pytest.raises(ValidationError, match="reasoning_tokens cannot exceed"):
        BudgetUsage(output_tokens=4, reasoning_tokens=5)
    with pytest.raises(ValidationError):
        BudgetLimits(max_model_calls=-1)


def test_task_and_experiment_specs_capture_reproducible_identity() -> None:
    task = TaskSpec(
        task_id="task-001",
        source="fixture-suite-v1",
        split="development",
        repository="example/project",
        repository_commit="deadbeef",
        image_digest=DIGEST_A,
        task_content_hash=HASH_A,
        problem_statement=artifact(),
        repository_snapshot=artifact(digest=HASH_B),
        visible_tests=(artifact(),),
    )
    experiment = ExperimentSpec(
        experiment_id="experiment-001",
        hypothesis="A scripted agent completes the deterministic fixture.",
        candidate_ids=("solo-v0",),
        task_ids=(task.task_id,),
        task_split=task.split,
        repeats=3,
        budget_limits=BudgetLimits(max_model_calls=4),
        metrics=("resolved", "total_tokens"),
        stopping_rule="run all declared repetitions",
    )

    assert task.schema_version == experiment.schema_version == "0.1"
    with pytest.raises(ValidationError, match="must be unique"):
        ExperimentSpec.model_validate(
            {**experiment.model_dump(), "candidate_ids": ["solo-v0", "solo-v0"]}
        )


def test_run_manifest_enforces_lifecycle_and_normalizes_utc() -> None:
    offset_start = datetime(2026, 7, 31, 12, 5, tzinfo=timezone(timedelta(hours=2)))
    succeeded = manifest(
        status=RunStatus.SUCCEEDED,
        started_at=offset_start,
        finished_at=CREATED_AT + timedelta(minutes=10),
    )

    assert succeeded.started_at == CREATED_AT + timedelta(minutes=5)
    assert succeeded.started_at is not None
    assert succeeded.started_at.tzinfo is UTC
    assert succeeded.status.is_terminal

    with pytest.raises(ValidationError, match="running runs require started_at"):
        manifest(status=RunStatus.RUNNING)
    with pytest.raises(ValidationError, match="terminal runs require finished_at"):
        manifest(status=RunStatus.FAILED, terminal_reason="sandbox failed")
    with pytest.raises(ValidationError, match="timezone-aware"):
        manifest(created_at=datetime(2026, 7, 31, 10, 0))


def test_event_record_validates_zero_based_hash_chain_and_payload_hash() -> None:
    payload: dict[str, JsonValue] = {"state": "started", "attempt": 1}
    first = EventRecord(
        event_id="event-000",
        run_id="run-001",
        sequence=0,
        event_type="run.started",
        monotonic_ns=100,
        occurred_at=CREATED_AT,
        payload=payload,
        payload_sha256=canonical_sha256(payload),
    )
    second_payload: dict[str, JsonValue] = {"state": "finished"}
    second = EventRecord(
        event_id="event-001",
        run_id="run-001",
        sequence=1,
        causal_parent_ids=(first.event_id,),
        event_type="run.finished",
        monotonic_ns=200,
        occurred_at=CREATED_AT + timedelta(seconds=1),
        payload=second_payload,
        payload_sha256=canonical_sha256(second_payload),
        previous_event_hash=first.content_hash(),
    )

    assert second.previous_event_hash == canonical_sha256(first)
    with pytest.raises(ValidationError, match="first event cannot"):
        EventRecord.model_validate({**first.model_dump(), "previous_event_hash": HASH_A})
    with pytest.raises(ValidationError, match="require a previous event hash"):
        EventRecord.model_validate({**second.model_dump(), "previous_event_hash": None})
    with pytest.raises(ValidationError, match="does not match"):
        EventRecord.model_validate({**first.model_dump(), "payload_sha256": HASH_A})
    with pytest.raises(ValidationError):
        EventRecord.model_validate({**first.model_dump(), "payload_sha256": HASH_A.upper()})


def test_evaluation_requires_terminal_run_and_verified_result() -> None:
    result = {"tests_passed": 4, "tests_failed": 0}
    values: dict[str, object] = {
        "evaluation_id": "evaluation-001",
        "run_id": "run-001",
        "run_status": RunStatus.SUCCEEDED,
        "evaluator_version": "fixture-evaluator-v1",
        "task_hash": HASH_A,
        "patch_hash": HASH_B,
        "outcome": "passed",
        "score": 1.0,
        "result": result,
        "result_sha256": canonical_sha256(result),
        "evidence": (artifact(),),
        "evaluated_at": CREATED_AT,
    }

    evaluation = EvaluationResult.model_validate(values)

    assert evaluation.outcome == "passed"
    with pytest.raises(ValidationError, match="terminal run state"):
        EvaluationResult.model_validate({**values, "run_status": RunStatus.RUNNING})
    with pytest.raises(ValidationError, match="does not match"):
        EvaluationResult.model_validate({**values, "result_sha256": HASH_A})
    with pytest.raises(ValidationError, match="terminal run state"):
        EvaluationResult.model_validate({**values, "outcome": "failed"})
    with pytest.raises(ValidationError, match="cannot have a patch hash"):
        EvaluationResult.model_validate({**values, "outcome": "not_run"})


def test_json_schema_export_covers_public_models_and_forbids_extras() -> None:
    schemas = export_json_schemas()

    assert tuple(schemas) == (
        "ArtifactRef",
        "BudgetLimits",
        "BudgetUsage",
        "EvaluationResult",
        "EventRecord",
        "ExperimentSpec",
        "ReliabilityCampaignManifest",
        "ReliabilityCampaignReport",
        "RunManifest",
        "TaskSpec",
    )
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["EventRecord"]["properties"]["schema_version"]["const"] == "0.1"
