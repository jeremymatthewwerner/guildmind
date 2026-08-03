"""Deterministic real-process contention for same-digest CAS publication.

Eight persistent spawned workers each publish the same bytes in twenty unique-digest
rounds. Test-only wrappers stop every worker immediately before and after the real
atomic no-replace syscall, allowing the parent to inspect the complete contender and
winner namespaces without sleeps or production hooks.

This is evidence for 160 cooperative low-level puts on this run and host. It is not a
claim about hostile same-UID processes, arbitrary worker counts, power loss, or other
filesystems and deployment environments.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import stat
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from pathlib import Path

import pytest

import guildmind.storage.artifacts as artifact_module
from guildmind.domain import sha256_bytes
from guildmind.storage import ArtifactFinding, ArtifactFindingKind, FileArtifactStore
from guildmind.storage.integrity import audit_artifact_store

_WORKER_COUNT = 8
_ROUND_COUNT = 20
_MEDIA_TYPE = "application/octet-stream"


def _round_bytes(round_index: int) -> bytes:
    return f"guildmind same-digest contention round {round_index:02d}\n".encode()


@dataclass(frozen=True, slots=True)
class _FileEvidence:
    path: str
    device: int
    inode: int
    mode: int
    link_count: int
    size_bytes: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ReferenceEvidence:
    media_type: str
    size_bytes: int
    sha256: str
    storage_ref: str


@dataclass(frozen=True, slots=True)
class _RoundMessage:
    round_index: int
    worker_index: int


@dataclass(frozen=True, slots=True)
class _Started(_RoundMessage):
    pass


@dataclass(frozen=True, slots=True)
class _Ready(_RoundMessage):
    source: str
    target: str
    temporary: _FileEvidence


@dataclass(frozen=True, slots=True)
class _Renamed(_RoundMessage):
    published: bool
    temporary: _FileEvidence | None
    canonical: _FileEvidence


@dataclass(frozen=True, slots=True)
class _Result(_RoundMessage):
    reference: _ReferenceEvidence
    canonical: _FileEvidence


@dataclass(frozen=True, slots=True)
class _Done(_RoundMessage):
    pass


@dataclass(frozen=True, slots=True)
class _WorkerError(_RoundMessage):
    error_type: str
    detail: str


@dataclass(frozen=True, slots=True)
class _Go:
    round_index: int


@dataclass(frozen=True, slots=True)
class _Finish:
    round_index: int


def _file_evidence(path: Path) -> _FileEvidence:
    metadata = path.lstat()
    return _FileEvidence(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        link_count=metadata.st_nlink,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        ctime_ns=metadata.st_ctime_ns,
        sha256=sha256_bytes(path.read_bytes()),
    )


def _require_command(
    connection: Connection,
    command_type: type[_Go] | type[_Finish],
    round_index: int,
) -> None:
    command: object = connection.recv()
    if type(command) is not command_type or command.round_index != round_index:
        raise AssertionError(f"unexpected contention command for round {round_index}: {command!r}")


def _child_publish_all_rounds(
    root_text: str,
    worker_index: int,
    connection: Connection,
) -> None:
    root = Path(root_text)
    try:
        store = FileArtifactStore(root, trusted_base=root.parent)
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send(
                _WorkerError(
                    round_index=-1,
                    worker_index=worker_index,
                    error_type=type(error).__name__,
                    detail=str(error),
                )
            )
        raise
    real_rename = artifact_module._rename_noreplace
    current_round = -1

    def gated_rename(source: Path, target: Path) -> bool:
        round_index = current_round
        data = _round_bytes(round_index)
        digest = sha256_bytes(data)
        expected_target = root / "sha256" / digest[:2] / digest
        if target != expected_target:
            raise AssertionError(f"worker received an unexpected canonical target: {target}")
        if source.parent != target.parent or not source.name.startswith(".artifact-"):
            raise AssertionError(f"worker received an unexpected temporary source: {source}")

        ready_temporary = _file_evidence(source)
        connection.send(
            _Ready(
                round_index=round_index,
                worker_index=worker_index,
                source=str(source),
                target=str(target),
                temporary=ready_temporary,
            )
        )
        _require_command(connection, _Go, round_index)

        published = real_rename(source, target)
        temporary_after = None if published else _file_evidence(source)
        canonical_after = _file_evidence(target)
        connection.send(
            _Renamed(
                round_index=round_index,
                worker_index=worker_index,
                published=published,
                temporary=temporary_after,
                canonical=canonical_after,
            )
        )
        _require_command(connection, _Finish, round_index)
        return published

    artifact_module._rename_noreplace = gated_rename
    try:
        connection.send(_Started(round_index=-1, worker_index=worker_index))
        for round_index in range(_ROUND_COUNT):
            current_round = round_index
            reference = store.put_bytes(_round_bytes(round_index), media_type=_MEDIA_TYPE)
            connection.send(
                _Result(
                    round_index=round_index,
                    worker_index=worker_index,
                    reference=_ReferenceEvidence(
                        media_type=reference.media_type,
                        size_bytes=reference.size_bytes,
                        sha256=reference.sha256,
                        storage_ref=reference.storage_ref,
                    ),
                    canonical=_file_evidence(store.path_for(reference)),
                )
            )
        connection.send(
            _Done(
                round_index=_ROUND_COUNT,
                worker_index=worker_index,
            )
        )
    except BaseException as error:
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send(
                _WorkerError(
                    round_index=current_round,
                    worker_index=worker_index,
                    error_type=type(error).__name__,
                    detail=str(error),
                )
            )
        raise
    finally:
        connection.close()


def _start_workers(root: Path) -> tuple[tuple[BaseProcess, ...], tuple[Connection, ...]]:
    context = multiprocessing.get_context("spawn")
    processes: list[BaseProcess] = []
    parents: list[Connection] = []
    child_endpoints: list[Connection] = []
    try:
        for worker_index in range(_WORKER_COUNT):
            parent, child = context.Pipe(duplex=True)
            child_endpoints.append(child)
            spawned_process = context.Process(
                target=_child_publish_all_rounds,
                args=(str(root), worker_index, child),
                name=f"guildmind-cas-contender-{worker_index}",
            )
            try:
                spawned_process.start()
            except BaseException:
                with suppress(BaseException):
                    parent.close()
                with suppress(BaseException):
                    child.close()
                with suppress(BaseException):
                    spawned_process.close()
                raise
            processes.append(spawned_process)
            parents.append(parent)
            try:
                child.close()
            except BaseException:
                with suppress(BaseException):
                    child.close()
                raise
    except BaseException:
        for child_endpoint in child_endpoints:
            with suppress(BaseException):
                child_endpoint.close()
        _cleanup_workers(tuple(processes), tuple(parents))
        raise
    return tuple(processes), tuple(parents)


def _collect_round_messages[MessageT: _RoundMessage](
    processes: tuple[BaseProcess, ...],
    connections: tuple[Connection, ...],
    message_type: type[MessageT],
    round_index: int,
) -> tuple[MessageT, ...]:
    pending = set(range(_WORKER_COUNT))
    observed: dict[int, MessageT] = {}
    deadline = time.monotonic() + 30
    while pending:
        waitables: tuple[Connection | int, ...] = tuple(
            connections[index] for index in pending
        ) + tuple(processes[index].sentinel for index in pending)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                f"contention phase timed out: round={round_index}, "
                f"message={message_type.__name__}, pending={sorted(pending)}"
            )
        ready = wait(waitables, timeout=remaining)
        if not ready:
            pytest.fail(
                f"contention phase timed out: round={round_index}, "
                f"message={message_type.__name__}, pending={sorted(pending)}"
            )
        for worker_index in tuple(pending):
            connection = connections[worker_index]
            process = processes[worker_index]
            if connection in ready:
                try:
                    message: object = connection.recv()
                except EOFError:
                    process.join(timeout=1)
                    pytest.fail(
                        f"worker {worker_index} closed before {message_type.__name__}: "
                        f"exitcode={process.exitcode}"
                    )
                if type(message) is not message_type:
                    pytest.fail(
                        f"worker {worker_index} sent unexpected contention IPC: {message!r}"
                    )
                if message.worker_index != worker_index or message.round_index != round_index:
                    pytest.fail(f"worker {worker_index} sent misbound contention IPC: {message!r}")
                observed[worker_index] = message
                pending.remove(worker_index)
            elif process.sentinel in ready:
                process.join(timeout=0)
                pytest.fail(
                    f"worker {worker_index} exited before {message_type.__name__}: "
                    f"round={round_index}, exitcode={process.exitcode}"
                )
    return tuple(observed[index] for index in range(_WORKER_COUNT))


def _send_all(
    connections: tuple[Connection, ...],
    command: _Go | _Finish,
    *,
    first_worker: int = 0,
) -> None:
    for offset in range(_WORKER_COUNT):
        connections[(first_worker + offset) % _WORKER_COUNT].send(command)


def _cleanup_workers(
    processes: tuple[BaseProcess, ...],
    connections: tuple[Connection, ...],
) -> None:
    cleanup_errors: list[str] = []
    for worker_index, connection in enumerate(connections):
        try:
            connection.close()
        except BaseException as error:
            cleanup_errors.append(
                f"worker {worker_index} parent connection close failed: {error!r}"
            )
    for process in processes:
        try:
            alive = process.is_alive()
        except BaseException as error:
            cleanup_errors.append(f"{process.name} liveness check failed: {error!r}")
            alive = True
        if alive:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except BaseException as error:
                cleanup_errors.append(f"{process.name} kill failed: {error!r}")
        try:
            sentinel = process.sentinel
            terminated = wait((sentinel,), timeout=10)
        except BaseException as error:
            cleanup_errors.append(f"{process.name} sentinel wait failed: {error!r}")
            continue
        if sentinel not in terminated:
            cleanup_errors.append(f"{process.name} did not terminate after kill")
            continue
        try:
            process.join(timeout=1)
        except BaseException as error:
            cleanup_errors.append(f"{process.name} join failed: {error!r}")
            continue
        try:
            still_alive = process.is_alive()
        except BaseException as error:
            cleanup_errors.append(f"{process.name} final liveness check failed: {error!r}")
            continue
        if still_alive:
            cleanup_errors.append(f"{process.name} remained alive after sentinel readiness")
            continue
        try:
            process.close()
        except BaseException as error:
            cleanup_errors.append(f"{process.name} close failed: {error!r}")
    if cleanup_errors:
        raise AssertionError("; ".join(cleanup_errors))


def _assert_regular_single_link_0600(evidence: _FileEvidence) -> None:
    metadata = os.lstat(evidence.path)
    assert stat.S_ISREG(metadata.st_mode)
    assert evidence.mode == 0o600
    assert evidence.link_count == 1


def _assert_contender(
    ready: _Ready,
    *,
    round_index: int,
    worker_index: int,
    shard: Path,
    canonical: Path,
    data: bytes,
    digest: str,
) -> None:
    source = Path(ready.source)
    assert ready.round_index == round_index
    assert ready.worker_index == worker_index
    assert Path(ready.target) == canonical
    assert source.parent == shard
    assert source.name.startswith(".artifact-")
    assert ready.temporary.path == ready.source
    assert ready.temporary.size_bytes == len(data)
    assert ready.temporary.sha256 == digest
    assert source.read_bytes() == data
    assert _file_evidence(source) == ready.temporary
    _assert_regular_single_link_0600(ready.temporary)


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL")
    or (sys.platform != "darwin" and not sys.platform.startswith("linux")),
    reason="requires spawn-capable POSIX processes and a supported no-replace host",
)
def test_eight_persistent_publishers_contend_for_twenty_unique_digests(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    preparer = FileArtifactStore(root, trusted_base=tmp_path)
    round_data = tuple(_round_bytes(index) for index in range(_ROUND_COUNT))
    round_digests = tuple(sha256_bytes(data) for data in round_data)
    assert len(round_digests) == _ROUND_COUNT
    assert len(set(round_digests)) == _ROUND_COUNT
    assert len({digest[:2] for digest in round_digests}) == _ROUND_COUNT
    shards = tuple(preparer._artifact_parent(digest, create=True) for digest in round_digests)
    for shard in shards:
        FileArtifactStore._fsync_directory(shard)

    processes, connections = _start_workers(root)
    winner_inodes: list[tuple[int, int]] = []
    completed_canonical_evidence: list[_FileEvidence] = []
    try:
        _collect_round_messages(
            processes,
            connections,
            _Started,
            -1,
        )
        for round_index, (data, digest, shard) in enumerate(
            zip(round_data, round_digests, shards, strict=True)
        ):
            for prior_evidence in completed_canonical_evidence:
                assert _file_evidence(Path(prior_evidence.path)) == prior_evidence
            canonical = shard / digest
            ready = _collect_round_messages(
                processes,
                connections,
                _Ready,
                round_index,
            )
            assert not canonical.exists()
            for worker_index, message in enumerate(ready):
                _assert_contender(
                    message,
                    round_index=round_index,
                    worker_index=worker_index,
                    shard=shard,
                    canonical=canonical,
                    data=data,
                    digest=digest,
                )
            contender_paths = tuple(message.source for message in ready)
            contender_inodes = tuple(
                (message.temporary.device, message.temporary.inode) for message in ready
            )
            assert len(set(contender_paths)) == _WORKER_COUNT
            assert len(set(contender_inodes)) == _WORKER_COUNT
            assert tuple(sorted(str(path) for path in shard.glob(".artifact-*"))) == tuple(
                sorted(contender_paths)
            )
            assert tuple(sorted(str(path) for path in shard.iterdir())) == tuple(
                sorted(contender_paths)
            )

            # Rotate the first GO recipient deterministically. This avoids baking a
            # worker-index preference into the harness without claiming scheduler fairness.
            _send_all(
                connections,
                _Go(round_index),
                first_worker=round_index % _WORKER_COUNT,
            )
            renamed = _collect_round_messages(
                processes,
                connections,
                _Renamed,
                round_index,
            )
            published = tuple(message.published for message in renamed)
            assert published.count(True) == 1
            assert published.count(False) == _WORKER_COUNT - 1
            winner_index = published.index(True)
            winner_ready = ready[winner_index]
            winner_inode = (
                winner_ready.temporary.device,
                winner_ready.temporary.inode,
            )
            winner_inodes.append(winner_inode)

            canonical_evidence = _file_evidence(canonical)
            _assert_regular_single_link_0600(canonical_evidence)
            assert canonical_evidence.sha256 == digest
            assert canonical_evidence.size_bytes == len(data)
            assert canonical.read_bytes() == data
            assert (canonical_evidence.device, canonical_evidence.inode) == winner_inode
            remaining_losers = tuple(sorted(str(path) for path in shard.glob(".artifact-*")))
            expected_losers = tuple(
                sorted(
                    message.source
                    for worker_index, message in enumerate(ready)
                    if worker_index != winner_index
                )
            )
            assert remaining_losers == expected_losers
            assert len(remaining_losers) == _WORKER_COUNT - 1
            assert tuple(sorted(str(path) for path in shard.iterdir())) == tuple(
                sorted((*expected_losers, str(canonical)))
            )

            for worker_index, renamed_message in enumerate(renamed):
                assert renamed_message.canonical == canonical_evidence
                if worker_index == winner_index:
                    assert renamed_message.temporary is None
                    assert not Path(ready[worker_index].source).exists()
                else:
                    assert renamed_message.temporary == ready[worker_index].temporary
                    assert (
                        _file_evidence(Path(ready[worker_index].source))
                        == ready[worker_index].temporary
                    )

            _send_all(connections, _Finish(round_index))
            results = _collect_round_messages(
                processes,
                connections,
                _Result,
                round_index,
            )
            expected_reference = _ReferenceEvidence(
                media_type=_MEDIA_TYPE,
                size_bytes=len(data),
                sha256=digest,
                storage_ref=f"sha256/{digest[:2]}/{digest}",
            )
            assert (
                tuple(message.reference for message in results)
                == (expected_reference,) * _WORKER_COUNT
            )
            assert (
                tuple(message.canonical for message in results)
                == (_file_evidence(canonical),) * _WORKER_COUNT
            )
            assert tuple(shard.glob(".artifact-*")) == ()
            assert (os.lstat(canonical).st_dev, os.lstat(canonical).st_ino) == winner_inode
            assert tuple(shard.iterdir()) == (canonical,)
            completed_canonical_evidence.append(_file_evidence(canonical))

        _collect_round_messages(
            processes,
            connections,
            _Done,
            _ROUND_COUNT,
        )
        for process in processes:
            terminated = wait((process.sentinel,), timeout=30)
            assert process.sentinel in terminated
            process.join(timeout=1)
            assert process.exitcode == 0

        assert len(winner_inodes) == _ROUND_COUNT
        assert len(completed_canonical_evidence) == _ROUND_COUNT
        for final_evidence in completed_canonical_evidence:
            assert _file_evidence(Path(final_evidence.path)) == final_evidence
        assert tuple(root.rglob(".artifact-*")) == ()
        reader = FileArtifactStore.open_existing_read_only(root, trusted_base=tmp_path)
        audit = audit_artifact_store((), reader)
        expected_findings = tuple(
            ArtifactFinding(
                kind=ArtifactFindingKind.VALID_FINALIZED_ORPHAN,
                relative_path=f"sha256/{digest[:2]}/{digest}",
                expected_sha256=digest,
                observed_sha256=digest,
                size_bytes=len(data),
            )
            for data, digest in sorted(
                zip(round_data, round_digests, strict=True),
                key=lambda item: f"sha256/{item[1][:2]}/{item[1]}",
            )
        )
        assert audit.complete
        assert audit.quarantine_allowed
        assert audit.reachable == ()
        assert audit.findings == expected_findings
        assert len(audit.findings) == _ROUND_COUNT
        for final_evidence in completed_canonical_evidence:
            assert _file_evidence(Path(final_evidence.path)) == final_evidence
    finally:
        _cleanup_workers(processes, connections)
