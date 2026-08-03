"""Cooperative real-process concurrency checks for orphan quarantine.

Spawned children install process-local test wrappers at existing durable protocol
boundaries and announce those boundaries over pipes. Parents act only after an exact
announcement, so no case depends on sleeps or scheduler timing.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

import guildmind.storage.artifacts as artifact_module
import guildmind.storage.quarantine as quarantine_module
from guildmind.domain import canonical_sha256, sha256_bytes
from guildmind.storage import (
    ArtifactFindingKind,
    EventStore,
    FileArtifactStore,
    MaintenanceBusyError,
    MaintenanceBusyReason,
    MaintenanceLease,
    QuarantineActive,
    QuarantineAfter,
    QuarantineBefore,
    QuarantineCandidate,
    QuarantineComplete,
    QuarantineDeniedError,
    QuarantineFailureReason,
    QuarantineFinalizationError,
    QuarantineIncompleteError,
    QuarantineOutcome,
    QuarantinePlan,
    QuarantineReceipt,
    QuarantineResult,
    audit_storage,
    quarantine_orphans,
)

_PUBLISHER_DATA = b"cooperating publisher unbound evidence\n"
_QUARANTINE_DATA = b"cooperative quarantine candidate\n"
_SHARED_MUTATION_DATA = b"shared mutation must not publish while maintenance owns the state\n"


class _PublicationBoundary(StrEnum):
    TEMPORARY_UNBOUND = "temporary_unbound"
    FINALIZED_UNBOUND = "finalized_unbound"


class _QuarantineBoundary(StrEnum):
    NONE = "none"
    PRE_MOVE = "pre_move"
    POST_UNFENCE_SYNC = "post_unfence_sync"


def _block_child(barrier: Connection, message: tuple[object, ...]) -> None:
    barrier.send(message)
    command = barrier.recv()
    if command != ("continue",):
        raise AssertionError(f"unexpected barrier command: {command!r}")


def _child_publish_unbound(
    state_text: str,
    boundary_text: str,
    barrier: Connection,
) -> None:
    state = Path(state_text)
    boundary = _PublicationBoundary(boundary_text)
    try:
        with MaintenanceLease.acquire_shared(state):
            store = FileArtifactStore(state / "artifacts", trusted_base=state.parent)
            real_rename = artifact_module._rename_noreplace

            if boundary is _PublicationBoundary.TEMPORARY_UNBOUND:

                def wrapped_rename(source: Path, target: Path) -> bool:
                    _block_child(
                        barrier,
                        (
                            "entered",
                            boundary.value,
                            source.relative_to(store.root).as_posix(),
                            target.relative_to(store.root).as_posix(),
                        ),
                    )
                    return real_rename(source, target)

            else:

                def wrapped_rename(source: Path, target: Path) -> bool:
                    published = real_rename(source, target)
                    if not published:
                        raise AssertionError("unexpected competing artifact publisher")
                    _block_child(
                        barrier,
                        (
                            "entered",
                            boundary.value,
                            source.relative_to(store.root).as_posix(),
                            target.relative_to(store.root).as_posix(),
                        ),
                    )
                    return published

            artifact_module._rename_noreplace = wrapped_rename
            store.put_bytes(_PUBLISHER_DATA, media_type="application/octet-stream")
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(("error", boundary.value, type(error).__name__, str(error)))
        raise
    else:
        barrier.send(("error", boundary.value, "UnexpectedReturn", "publication completed"))
    finally:
        barrier.close()


def _child_quarantine_at_boundary(
    state_text: str,
    boundary_text: str,
    start_gated: bool,
    barrier: Connection,
) -> None:
    state = Path(state_text)
    boundary = _QuarantineBoundary(boundary_text)
    try:
        if start_gated:
            barrier.send(("ready", boundary.value))
            command = barrier.recv()
            if command != ("start",):
                raise AssertionError(f"unexpected start command: {command!r}")

        if boundary is _QuarantineBoundary.PRE_MOVE:
            real_move = quarantine_module._move_candidate
            blocked = False

            def wrapped_move(
                artifact_descriptor: int,
                payload_descriptor: int,
                receipts_descriptor: int,
                plan: QuarantinePlan,
                plan_sha256: str,
                candidate: QuarantineCandidate,
            ) -> None:
                nonlocal blocked
                if not blocked:
                    blocked = True
                    _block_child(
                        barrier,
                        (
                            "entered",
                            boundary.value,
                            plan.transaction_id,
                            candidate.candidate_id,
                        ),
                    )
                real_move(
                    artifact_descriptor,
                    payload_descriptor,
                    receipts_descriptor,
                    plan,
                    plan_sha256,
                    candidate,
                )

            quarantine_module._move_candidate = wrapped_move
        elif boundary is _QuarantineBoundary.POST_UNFENCE_SYNC:
            real_remove_active = quarantine_module._remove_active

            def wrapped_remove_active(
                version_descriptor: int,
                expected: QuarantineActive,
                result: QuarantineResult,
            ) -> None:
                real_remove_active(version_descriptor, expected, result)
                _block_child(
                    barrier,
                    (
                        "entered",
                        boundary.value,
                        result.model_dump_json(),
                    ),
                )

            quarantine_module._remove_active = wrapped_remove_active

        result = quarantine_orphans(state)
    except QuarantineDeniedError as error:
        cause_reason = (
            error.__cause__.reason.value
            if isinstance(error.__cause__, MaintenanceBusyError)
            else None
        )
        barrier.send(("denied", error.reason.value, cause_reason))
    except QuarantineIncompleteError as error:
        barrier.send(("incomplete", error.reason.value, error.transaction_id))
    except QuarantineFinalizationError as error:
        barrier.send(
            (
                "finalization_error",
                error.result.model_dump_json(),
                error.detail,
            )
        )
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            barrier.send(("error", boundary.value, type(error).__name__, str(error)))
        raise
    else:
        barrier.send(("result", result.model_dump_json()))
    finally:
        barrier.close()


def _start_process(
    target: Callable[..., object],
    *arguments: object,
    name: str,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    parent_barrier, child_barrier = context.Pipe(duplex=True)
    process = context.Process(
        target=target,
        args=(*arguments, child_barrier),
        name=name,
    )
    process.start()
    child_barrier.close()
    return process, parent_barrier


def _receive(process: BaseProcess, barrier: Connection) -> tuple[object, ...]:
    ready = wait((barrier, process.sentinel), timeout=30)
    if barrier not in ready:
        process.join(timeout=1)
        pytest.fail(f"child exited before its barrier message: exitcode={process.exitcode}")
    try:
        message: object = barrier.recv()
    except EOFError:
        process.join(timeout=1)
        pytest.fail(f"child closed its barrier without a message: exitcode={process.exitcode}")
    if not isinstance(message, tuple):
        pytest.fail(f"child sent a non-tuple barrier message: {message!r}")
    if message and message[0] == "error":
        process.join(timeout=5)
        pytest.fail(f"child reported an unexpected error: {message!r}")
    return message


def _kill_blocked_process(process: BaseProcess) -> None:
    pid = process.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    process.join(timeout=10)
    assert not process.is_alive()
    assert process.exitcode == -signal.SIGKILL


def _join_successful_process(process: BaseProcess) -> None:
    process.join(timeout=20)
    assert not process.is_alive()
    assert process.exitcode == 0


def _cleanup_process(process: BaseProcess, barrier: Connection) -> None:
    barrier.close()
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    elif process.exitcode is None:
        process.join(timeout=5)
    process.close()


def _initialize_state(tmp_path: Path) -> tuple[Path, FileArtifactStore]:
    state = tmp_path / "state"
    state.mkdir()
    with EventStore(state / "runs.db"):
        pass
    store = FileArtifactStore(state / "artifacts", trusted_base=state.parent)
    return state, store


def _put_orphan(store: FileArtifactStore, data: bytes = _QUARANTINE_DATA) -> Path:
    reference = store.put_bytes(data, media_type="application/octet-stream")
    return store.path_for(reference)


def _start_quarantine_child(
    state: Path,
    boundary: _QuarantineBoundary,
    *,
    start_gated: bool = False,
    name: str,
) -> tuple[BaseProcess, Connection]:
    return _start_process(
        _child_quarantine_at_boundary,
        str(state),
        boundary.value,
        start_gated,
        name=name,
    )


def _leave_durable_active_by_killing_before_move(state: Path) -> tuple[str, str]:
    process, barrier = _start_quarantine_child(
        state,
        _QuarantineBoundary.PRE_MOVE,
        name="guildmind-quarantine-active-setup",
    )
    try:
        message = _receive(process, barrier)
        assert message[:2] == ("entered", _QuarantineBoundary.PRE_MOVE.value)
        transaction_id = str(message[2])
        candidate_id = str(message[3])
        active = state / "quarantine" / "v1" / "ACTIVE"
        assert active.is_file()
        _kill_blocked_process(process)
        return transaction_id, candidate_id
    finally:
        _cleanup_process(process, barrier)


def _assert_one_completed_transaction(
    state: Path,
    result: QuarantineResult,
    *,
    expected_kind: ArtifactFindingKind = ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
    expected_source: str | None = None,
    expected_bytes: bytes = _QUARANTINE_DATA,
) -> QuarantineCandidate:
    assert result.outcome is QuarantineOutcome.COMPLETED
    assert result.quarantined_count == 1
    assert result.final_report.clean
    assert result.transaction_id is not None
    assert result.completion_sha256 is not None
    assert not (state / "quarantine" / "v1" / "ACTIVE").exists()

    transaction = state / "quarantine" / "v1" / "transactions" / result.transaction_id
    plan = QuarantinePlan.model_validate_json(
        (transaction / "PLAN.json").read_bytes(),
        strict=True,
    )
    before = QuarantineBefore.model_validate_json(
        (transaction / "BEFORE.json").read_bytes(),
        strict=True,
    )
    after = QuarantineAfter.model_validate_json(
        (transaction / "AFTER.json").read_bytes(),
        strict=True,
    )
    assert plan.transaction_id == result.transaction_id
    assert len(plan.body.candidates) == 1
    candidate = plan.body.candidates[0]
    assert candidate.body.finding.kind is expected_kind
    if expected_source is not None:
        assert candidate.body.source_relative_path == expected_source

    payload_names = tuple(sorted(path.name for path in (transaction / "payload").iterdir()))
    receipt_names = tuple(sorted(path.name for path in (transaction / "receipts").iterdir()))
    assert payload_names == (candidate.candidate_id,)
    assert receipt_names == (f"{candidate.candidate_id}.json",)
    payload = transaction / "payload" / candidate.candidate_id
    assert payload.read_bytes() == expected_bytes
    assert payload.stat().st_nlink == 1
    receipt = QuarantineReceipt.model_validate_json(
        (transaction / "receipts" / receipt_names[0]).read_bytes(),
        strict=True,
    )
    assert receipt.candidate_id == candidate.candidate_id
    assert receipt.transaction_id == result.transaction_id
    assert receipt.source_relative_path == candidate.body.source_relative_path
    assert receipt.destination_relative_path == candidate.destination_relative_path
    complete = QuarantineComplete.model_validate_json(
        (transaction / "COMPLETE.json").read_bytes(),
        strict=True,
    )
    assert complete.transaction_id == result.transaction_id
    assert tuple(item.candidate_id for item in complete.receipts) == (candidate.candidate_id,)
    assert complete.plan_sha256 == canonical_sha256(plan)
    assert complete.before_sha256 == canonical_sha256(before)
    assert complete.after_sha256 == canonical_sha256(after)
    assert complete.receipts[0].receipt_sha256 == canonical_sha256(receipt)
    assert result.completion_sha256 == canonical_sha256(complete)
    return candidate


def _assert_shared_busy(state: Path, reason: MaintenanceBusyReason) -> None:
    with pytest.raises(MaintenanceBusyError) as raised:
        MaintenanceLease.acquire_shared(state)
    assert raised.value.reason is reason


pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or (sys.platform != "darwin" and not sys.platform.startswith("linux")),
    reason="requires POSIX SIGKILL and a supported no-replace rename host",
)


@pytest.mark.parametrize(
    ("boundary", "expected_kind"),
    (
        (_PublicationBoundary.TEMPORARY_UNBOUND, ArtifactFindingKind.TEMP_ORPHAN),
        (
            _PublicationBoundary.FINALIZED_UNBOUND,
            ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
        ),
    ),
    ids=("temporary-unbound", "finalized-unbound"),
)
def test_shared_publisher_blocks_quarantine_until_synchronized_death(
    tmp_path: Path,
    boundary: _PublicationBoundary,
    expected_kind: ArtifactFindingKind,
) -> None:
    state, _ = _initialize_state(tmp_path)
    process, barrier = _start_process(
        _child_publish_unbound,
        str(state),
        boundary.value,
        name=f"guildmind-shared-publisher-{boundary.value}",
    )
    try:
        message = _receive(process, barrier)
        assert message[:2] == ("entered", boundary.value)
        source_relative_path = str(message[2])
        target_relative_path = str(message[3])
        source = state / "artifacts" / source_relative_path
        target = state / "artifacts" / target_relative_path
        if boundary is _PublicationBoundary.TEMPORARY_UNBOUND:
            assert source.is_file()
            assert not target.exists()
        else:
            assert not source.exists()
            assert target.is_file()

        with pytest.raises(QuarantineDeniedError) as raised:
            quarantine_orphans(state)
        assert raised.value.reason is QuarantineFailureReason.MAINTENANCE_DENIED
        assert isinstance(raised.value.__cause__, MaintenanceBusyError)
        assert raised.value.__cause__.reason is MaintenanceBusyReason.LEASE_HELD
        assert not (state / "quarantine").exists()

        _kill_blocked_process(process)
        report = audit_storage(state)
        assert report.artifact_audit is not None
        assert tuple(finding.kind for finding in report.artifact_audit.findings) == (expected_kind,)

        result = quarantine_orphans(state)
        _assert_one_completed_transaction(
            state,
            result,
            expected_kind=expected_kind,
            expected_source=(
                source_relative_path
                if boundary is _PublicationBoundary.TEMPORARY_UNBOUND
                else target_relative_path
            ),
            expected_bytes=_PUBLISHER_DATA,
        )
        assert not source.exists()
        assert not target.exists()
    finally:
        _cleanup_process(process, barrier)


def test_exclusive_quarantine_blocks_shared_mutation_and_second_maintainer(
    tmp_path: Path,
) -> None:
    state, store = _initialize_state(tmp_path)
    source = _put_orphan(store)
    process, barrier = _start_quarantine_child(
        state,
        _QuarantineBoundary.PRE_MOVE,
        name="guildmind-quarantine-exclusive-holder",
    )
    try:
        message = _receive(process, barrier)
        assert message[:2] == ("entered", _QuarantineBoundary.PRE_MOVE.value)
        assert source.is_file()
        assert (state / "quarantine" / "v1" / "ACTIVE").is_file()

        _assert_shared_busy(state, MaintenanceBusyReason.LEASE_HELD)
        mutation_digest = sha256_bytes(_SHARED_MUTATION_DATA)
        mutation_target = state / "artifacts" / "sha256" / mutation_digest[:2] / mutation_digest
        with (
            pytest.raises(MaintenanceBusyError) as mutation_busy,
            MaintenanceLease.acquire_shared(state),
        ):
            store.put_bytes(_SHARED_MUTATION_DATA, media_type="application/octet-stream")
        assert mutation_busy.value.reason is MaintenanceBusyReason.LEASE_HELD
        assert not mutation_target.exists()

        with pytest.raises(QuarantineDeniedError) as maintainer_busy:
            quarantine_orphans(state)
        assert maintainer_busy.value.reason is QuarantineFailureReason.MAINTENANCE_DENIED
        assert isinstance(maintainer_busy.value.__cause__, MaintenanceBusyError)
        assert maintainer_busy.value.__cause__.reason is MaintenanceBusyReason.LEASE_HELD

        barrier.send(("continue",))
        result_message = _receive(process, barrier)
        assert result_message[0] == "result"
        result = QuarantineResult.model_validate_json(str(result_message[1]), strict=True)
        _join_successful_process(process)
        _assert_one_completed_transaction(state, result)
        assert not source.exists()
    finally:
        _cleanup_process(process, barrier)


def test_kill_after_durable_active_keeps_shared_mutation_fenced_until_resume(
    tmp_path: Path,
) -> None:
    state, store = _initialize_state(tmp_path)
    source = _put_orphan(store)

    transaction_id, candidate_id = _leave_durable_active_by_killing_before_move(state)

    active = state / "quarantine" / "v1" / "ACTIVE"
    assert active.is_file()
    assert source.is_file()
    _assert_shared_busy(state, MaintenanceBusyReason.QUARANTINE_ACTIVE)

    result = quarantine_orphans(state)

    assert result.resumed
    assert result.transaction_id == transaction_id
    candidate = _assert_one_completed_transaction(state, result)
    assert candidate.candidate_id == candidate_id
    assert not source.exists()


def test_two_overlapping_resumers_have_one_winner_and_one_busy_denial(
    tmp_path: Path,
) -> None:
    state, store = _initialize_state(tmp_path)
    source = _put_orphan(store)
    transaction_id, candidate_id = _leave_durable_active_by_killing_before_move(state)
    transactions = state / "quarantine" / "v1" / "transactions"
    assert {path.name for path in transactions.iterdir()} == {transaction_id}

    winner, winner_barrier = _start_quarantine_child(
        state,
        _QuarantineBoundary.PRE_MOVE,
        start_gated=True,
        name="guildmind-quarantine-resumer-winner",
    )
    contender, contender_barrier = _start_quarantine_child(
        state,
        _QuarantineBoundary.NONE,
        start_gated=True,
        name="guildmind-quarantine-resumer-contender",
    )
    try:
        assert _receive(winner, winner_barrier) == (
            "ready",
            _QuarantineBoundary.PRE_MOVE.value,
        )
        assert _receive(contender, contender_barrier) == (
            "ready",
            _QuarantineBoundary.NONE.value,
        )

        winner_barrier.send(("start",))
        winner_entered = _receive(winner, winner_barrier)
        assert winner_entered[:2] == ("entered", _QuarantineBoundary.PRE_MOVE.value)
        assert winner_entered[2:] == (transaction_id, candidate_id)

        contender_barrier.send(("start",))
        denied = _receive(contender, contender_barrier)
        assert denied == (
            "denied",
            QuarantineFailureReason.MAINTENANCE_DENIED.value,
            MaintenanceBusyReason.LEASE_HELD.value,
        )
        _join_successful_process(contender)
        assert winner.is_alive()

        winner_barrier.send(("continue",))
        winner_result_message = _receive(winner, winner_barrier)
        assert winner_result_message[0] == "result"
        winner_result = QuarantineResult.model_validate_json(
            str(winner_result_message[1]),
            strict=True,
        )
        _join_successful_process(winner)

        assert winner_result.resumed
        assert winner_result.transaction_id == transaction_id
        candidate = _assert_one_completed_transaction(state, winner_result)
        assert candidate.candidate_id == candidate_id
        assert not source.exists()
        assert {path.name for path in transactions.iterdir()} == {transaction_id}
    finally:
        _cleanup_process(contender, contender_barrier)
        _cleanup_process(winner, winner_barrier)


def test_unfenced_quarantine_keeps_shared_busy_until_process_death(
    tmp_path: Path,
) -> None:
    state, store = _initialize_state(tmp_path)
    source = _put_orphan(store)
    process, barrier = _start_quarantine_child(
        state,
        _QuarantineBoundary.POST_UNFENCE_SYNC,
        name="guildmind-quarantine-post-unfence-holder",
    )
    try:
        message = _receive(process, barrier)
        assert message[:2] == (
            "entered",
            _QuarantineBoundary.POST_UNFENCE_SYNC.value,
        )
        result = QuarantineResult.model_validate_json(str(message[2]), strict=True)
        assert not (state / "quarantine" / "v1" / "ACTIVE").exists()
        assert not source.exists()
        _assert_shared_busy(state, MaintenanceBusyReason.LEASE_HELD)

        _kill_blocked_process(process)
        with MaintenanceLease.acquire_shared(state):
            pass

        candidate = _assert_one_completed_transaction(state, result)
        no_op = quarantine_orphans(state)
        assert no_op.outcome is QuarantineOutcome.NO_OP
        transaction_id = result.transaction_id
        assert transaction_id is not None
        transaction = state / "quarantine" / "v1" / "transactions" / transaction_id
        assert tuple(path.name for path in (transaction / "payload").iterdir()) == (
            candidate.candidate_id,
        )
        assert tuple(path.name for path in (transaction / "receipts").iterdir()) == (
            f"{candidate.candidate_id}.json",
        )
    finally:
        _cleanup_process(process, barrier)
