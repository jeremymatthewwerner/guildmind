# Same-Digest CAS Publisher Contention — 2026-08-03

**Evidence level:** local Darwin development-host process test on the host filesystem<br>
**Implementation checkpoint:** commit containing this document<br>
**Scope:** cooperative low-level `FileArtifactStore.put_bytes()` contention for one
absent digest at a time<br>
**Predeclared matrix:** 8 persistent spawned workers × 20 unique-digest rounds = 160
contested puts<br>
**Stage 1 effect:** closes the local same-digest publisher-contention checkpoint; the
broader Stage 1 gate remains **NOT PASSED**

## Claim established

On this local host and filesystem, eight cooperating spawned processes can enter the
verified-temporary-file boundary for the same absent content-addressed target, attempt
the real atomic no-replace publication primitive, and converge to one canonical file:

- all eight publishers first hold distinct single-link regular temporary files with
  mode `0600`, the exact expected bytes, and the expected digest;
- the canonical target is absent before any process is released to rename;
- exactly one real no-replace call reports publication success;
- the canonical file has the winning temporary file's device and inode;
- the other seven temporary files remain byte-for-byte and identity-exact while every
  publisher is stopped immediately after its rename attempt;
- after production cleanup resumes, all eight calls return the same exact `ArtifactRef`,
  every temporary name is absent, and the canonical file retains the winner's inode;
  and
- a final recursive audit reports exactly the twenty expected
  `valid_finalized_orphan` paths and no temporary, duplicate, or other finding.

The same assertions run for twenty deterministic payloads. Their full SHA-256 digests
and two-character shard prefixes are both required to be unique. The parent creates and
synchronizes every shard before starting workers, so the checkpoint isolates publication
contention from directory-creation races and each round has an independently exact shard
namespace.

This extends the earlier
[atomic no-replace publication](../2026-08-03-atomic-cas-publication/README.md) and
[temporary-write crash](../2026-08-03-cas-temporary-write/README.md) checkpoints. It tests
the competing-publisher path that those single-publisher process-crash matrices left
open.

## Deterministic process protocol

[`test_artifact_publication_concurrency.py`](../../../../tests/integration/test_artifact_publication_concurrency.py)
uses `multiprocessing.get_context("spawn")` and keeps eight workers alive for all twenty
rounds. Every worker owns a dedicated duplex pipe. Messages are frozen typed records,
and the parent requires the exact message type, worker index, round index, and phase.

Each process installs a test-local wrapper around the symbol actually called by
`FileArtifactStore`, `artifact_module._rename_noreplace`. The wrapper is entered only
after production has:

1. created the same-shard temporary file;
2. written and flushed all bytes;
3. file-`fsync`ed and closed the temporary descriptor; and
4. verified the temporary's exact path, bytes, SHA-256, stable identity, regular-file
   type, and single link.

The per-round protocol is:

| Phase | Worker action | Parent proof while workers are gated |
|---|---|---|
| `READY` | Send the exact source and target paths plus temporary device, inode, mode, link count, size, `mtime`, `ctime`, and content hash; wait for `GO`. | Canonical absent; shard contains exactly eight unique `.artifact-*` paths and no other entry; all eight are single-link `0600` regular files with identical expected bytes and digest. |
| `GO` | Call the real host no-replace helper exactly once. | Sent only after all eight `READY` records and independent parent path checks succeed. The first recipient rotates deterministically by round; no winner fairness is inferred. |
| `RENAMED` | Send the real publication boolean, surviving temporary evidence if any, and canonical evidence; wait for `FINISH`. | Exactly one `True`; winner temporary absent; canonical device/inode equals the winner temporary; exactly seven loser temporaries remain with their complete `READY` evidence unchanged; shard contains only those seven names plus the canonical digest. |
| `FINISH` | Return from the wrapper, allowing the production `finally` cleanup and shard-directory `fsync` to run. | No publisher can clean a loser before the complete post-rename namespace has been inspected. |
| `RESULT` | Send the returned reference and final canonical evidence. | Eight identical exact references; no temporary entry; shard contains only the canonical digest; canonical bytes, single-link identity, and winner inode remain exact. |

Before each later round and again after all workers exit, the parent reopens and compares
every earlier canonical file's complete captured evidence. This guards against a later
round perturbing a prior winner. `READY`, `RENAMED`, and `RESULT` collection uses one
absolute 30-second deadline per phase, includes every process sentinel, and contains no
timing sleep. A typed `STARTED` handshake covers worker/store initialization. EOF,
out-of-phase data, duplicate or misbound messages, an early process exit, or a missed
deadline fails the test.

Cleanup is best-effort across every worker: parent pipes are all closed, live processes
are killed if necessary, `ProcessLookupError` races are tolerated, and kill, sentinel,
join, liveness, and close errors are accumulated only after cleanup has been attempted
for every process.

## Exact final audit

After round twenty, all worker processes have exited successfully and no path matching
`.artifact-*` remains below the store. The read-only recursive audit is compared to an
ordered tuple of twenty complete `ArtifactFinding` models. Each expected model binds:

- `kind = valid_finalized_orphan`;
- the exact `sha256/<shard>/<digest>` relative path;
- matching expected and observed SHA-256 values; and
- the exact payload size.

The audit must be complete, permit quarantine under its empty authoritative root set,
contain no reachable entry, and equal that tuple exactly. Set comparison is not used, so
an extra or duplicate-looking finding cannot be hidden.

## Verification

The focused static checks were:

```bash
uv run ruff format --check tests/integration/test_artifact_publication_concurrency.py
uv run ruff check tests/integration/test_artifact_publication_concurrency.py
uv run mypy tests/integration/test_artifact_publication_concurrency.py
```

All passed on the local development host. The stabilized focused command was:

```bash
uv run pytest -q tests/integration/test_artifact_publication_concurrency.py
```

Five consecutive complete repetitions, without reducing the 8 × 20 matrix, reported:

```text
1 passed in 0.34s
1 passed in 0.33s
1 passed in 0.33s
1 passed in 0.33s
1 passed in 0.33s
```

No repetition timed out, deadlocked, or failed a CAS namespace, byte, reference, audit,
winner, or loser invariant. During harness stabilization, one earlier repetition found
a test-only observation race: after a process sentinel became ready,
`join(timeout=0)` had not yet refreshed `exitcode` even though the process had exited
successfully. Confirmed-sentinel joins now use a bounded nonzero timeout. The five results
above and the repository gate were collected after that fix.

An independent follow-up then ran ten further consecutive complete repetitions. All ten
passed without reducing the matrix, in 0.32–0.50 seconds per invocation. The complete
CAS-focused selection was also rerun:

```bash
uv run pytest -q \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py \
  tests/integration/test_artifact_publication_concurrency.py
```

```text
46 passed in 1.32s
```

The full repository command was:

```bash
make check
```

Result:

```text
ruff format --check: 107 files already formatted
ruff check: all checks passed
mypy: 73 source files, no issues found
pytest: 634 passed, 29 skipped in 15.08s
```

The 29 declared skips are the existing opt-in evaluator-image/reference-host cases and
two host-filesystem invalid-filename cases. The CAS publisher-contention test did not
skip.

## Evidence boundary and nonclaims

This checkpoint is deliberately narrow:

- It exercises the low-level `FileArtifactStore` primitive directly. It does not prove
  runtime publication, shared maintenance-lease behavior, runner behavior, quarantine,
  or an end-to-end application workflow.
- All eight processes cooperate with the test gates. This is not protection against a
  hostile same-UID process that ignores coordination or races checked pathnames.
- The result is local Darwin development-host evidence on this host filesystem. It is
  not native rootless x86_64 Linux reference-host, network-filesystem, arbitrary-mount,
  kernel-version, libc-version, or general-filesystem evidence.
- All eight workers are proven ready before the first `GO`, but the eight pipe sends are
  necessarily sequential and the scheduler may serialize syscalls. The first recipient
  rotates by round only to avoid a fixed worker-index preference. No fairness, winner
  distribution, throughput, latency, scalability, or performance claim is made.
- Eight workers, twenty rounds, and 160 puts are a predeclared bounded stress matrix,
  not a statistically derived reliability campaign, linearizability proof, or claim for
  other worker/round counts. Repetition establishes harness stability, not a confidence
  interval.
- Observing namespace state and returned `fsync` calls during live processes does not
  establish sudden-power-loss or storage-controller durability. No power cut occurred.
- The test does not revoke already-open descriptors, perform deletion or garbage
  collection, exercise providers or model-generated workloads, or authorize later
  Stage 1/Stage 2 work.

Broader runtime coordination, hostile-process stress, sudden-power-loss evidence, and
reference-host repetition remain separate gates.
