# ADR 0002: SQLite and Content-Addressed Artifacts

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-07-31

## Context

The first research program runs on one trusted control plane. It needs transactional run state and budget accounting, immutable large artifacts, crash recovery, and complete evidence without prematurely operating a database service.

## Decision

Use SQLite for authoritative metadata, state transitions, events, and budget transactions, plus a filesystem content-addressed store for immutable artifact bytes.

### Write ownership

Exactly one control-plane writer owns SQLite mutations and logical event sequence assignment. Workers and evaluators return typed messages; they never write the database or artifact references directly. Enable foreign keys, WAL mode, a bounded busy timeout, and versioned migrations. Do not place the authoritative database on a network filesystem.

Supported high-level publishers and mutators hold a shared state-wide maintenance lease; maintenance holds the same persistent local lock exclusively. Lease attempts are nonblocking. A durable quarantine ACTIVE fence blocks shared mutation until an exclusive maintainer resumes or resolves it. Direct `EventStore` and `FileArtifactStore` calls remain trusted low-level primitives whose callers must obey this coordination protocol.

### Artifact identity and commit order

- Address bytes by lowercase SHA-256 and store them under a sharded path such as `sha256/ab/cd/<digest>`.
- Hash opaque artifacts as their exact bytes. Hash structured JSON only after the schema's canonical UTF-8 serialization rules are applied.
- Write a blob to a temporary file on the destination filesystem, flush and `fsync`, verify its digest and length, atomically rename it to the final path, and sync the containing directory before committing a reference.
- In one SQLite transaction, append the event, reconcile the budget reservation, update run state, and add references only to finalized blobs.
- Never overwrite a digest path. Quarantine collisions or mismatched existing bytes as a critical integrity failure.

SQLite event and budget tables are authoritative. `events.jsonl`, Parquet, reports, and OpenTelemetry are reproducible exports, not competing sources of truth. Every started run receives a terminal manifest; unavailable outputs are explicit states such as `not_produced` or `not_run`, not missing rows.

Persist an external-request intent and idempotency key before dispatch. Guildmind promises no duplicate **committed result**, not exactly-once provider execution. Accepted requests without committed responses remain `ambiguous`, with possible spend retained.

Content hashes prove identity, not authorship. Stage approvals and lockbox access require a separate trusted authorization/signature record. Garbage collection is disabled during an active or sealed campaign.

## Recovery and scaling

Startup and explicit recovery verify migration state, SQLite integrity, run-state
transitions, artifact references, and digest/length metadata. Orphan temporary files and
unreferenced finalized blobs are reported by the read-only audit. Quarantine is an
explicit maintenance action rather than an automatic startup side effect; recovery never
invents an event or free spend.

Move to PostgreSQL/object storage only when measured concurrent worker demand requires distributed leases or the local durability/throughput envelope is insufficient. That migration requires a new ADR and evidence-preserving import checks.

### 2026-08-02 implementation checkpoint

The [real-process recovery suite](../evidence/crash-recovery/2026-08-02-process-sigkill/README.md) partially exercises this decision. A spawned child announces an exact durable or pre-`COMMIT` boundary over a pipe and is then killed by its parent with `SIGKILL`. Five EventStore cases cover committed prefixes from run creation through successful evaluation; four cases block the commit after response, evaluation, or either recovery transaction has executed its SQL body; two FixtureRunner cases kill the process from inside model and evaluator work after the preceding persistence phase has committed. A newly opened store passes SQLite integrity and foreign-key checks, preserves or restores the exact prefix, applies conservative budget semantics, creates at most one terminal event, replays to the manifest state, and treats a second recovery as an exact no-op. Referenced CAS bytes present at those boundaries are verified in the tests.

The pre-commit response case observes one finalized, unreferenced patch after rollback; the evaluation case observes three finalized, unreferenced evaluation blobs; neither recovery rollback creates a new orphan. Recovery does not infer any orphan into the manifest. This was a test-owned inventory, not yet the automatic audit required by this ADR. At that checkpoint the suite had no kill during the CAS temp-write/`fsync`/rename/directory-`fsync` sequence or in a concurrent writer race. The later [atomic no-replace publication checkpoint](../evidence/crash-recovery/2026-08-03-atomic-cas-publication/README.md) adds six exact Darwin kills: root, `sha256`, and shard `mkdir` after creation but before parent `fsync`, plus immediately before rename, immediately after rename, and after rename before directory `fsync`. It does not complete the earlier temporary-create/partial-write/file-`fsync` matrix or concurrent-writer coverage. Automatic startup/recovery verification, product orphan reporting/quarantine, writer-exclusion stress, and real-provider idempotency also remained unimplemented at the 2026-08-02 checkpoint. The normative startup and acceptance language above and below therefore remained a target, not a statement that the complete boundary had passed.

The subsequent [recursive storage-integrity audit](../evidence/storage-integrity/2026-08-02-recursive-audit/README.md) implements the read-only verification half of that target. `EventStore.verify_integrity()` commits to every validated manifest revision and event-chain head; `audit_artifact_store()` binds that ledger snapshot to direct and typed-recursive CAS references, verifies canonical structured artifacts and cross-object identities, and inventories bounded unreferenced entries without following links. It distinguishes audit completeness from the stronger condition that could permit quarantine. The implementation also captures filesystem directory identities and rejects deterministic configured-path replacement without breaking trusted operating-system aliases.

Using that audit to authorize quarantine requires a quiescent exclusive maintenance lease and candidate revalidation. A subsequent read-only coordinator opens existing SQLite/CAS stores without initializing them, holds the verified ledger snapshot through the CAS scan, classifies missing/invalid/empty/damaged/orphaned/healthy pairs, and derives conservative operation gates. Guarded recovery may audit under a shared lease only because it never acts on ownerless findings and revalidates the complete ledger/reachable-CAS graph under the SQLite writer lock before and after its narrow mutation. Existing-only writable SQLite opening and an in-transaction whole-ledger snapshot precondition supply that mutation foundation.

### 2026-08-03 guarded recovery checkpoint

The [guarded-recovery checkpoint](../evidence/crash-recovery/2026-08-03-guarded-recovery/README.md) routes explicit local-fixture runtime/CLI recovery through a fresh coordinator audit. It requires the target run in the verified snapshot, opens SQLite existing-only, recomputes the all-run commitment inside `BEGIN IMMEDIATE`, and re-audits recursively reachable CAS bytes from those locked roots before staging recovery. After staging, the transaction validates the complete ledger and invokes the recursive-CAS/path guard a second time against the post-mutation roots at the final pre-commit boundary. The recovered manifest and event stream are captured inside that transaction and are returned only after the final guard and commit succeed. Any failed guard rolls the transaction back; SQLite open/lock/integrity failures are normalized to a stable denial, and a second recovery is an exact no-op. Exceptions raised by `FixtureRunner` after run creation use this same guarded existing-only path rather than an unguarded same-connection terminalization. A pre-dispatch budget refusal uses an equivalent guarded terminalization, while a budget error after dispatch receives conservative general recovery. Normal evaluation completion also captures its terminal manifest and event stream inside the completion transaction rather than reading them after commit. Existing ownerless orphans remain untouched. `replay` and `report` use existing-only read-only handles and verified ledger snapshots, returning typed denials rather than creating absent state.

This closes the explicit recovery-integration gap, not the full normative startup/quarantine requirement. Recovery is not an automatic startup sweep, no orphan is moved, and individual pathname check/open/traversal pairs are not atomic against an actively racing same-UID process.

### 2026-08-03 publication and maintenance prerequisites

The [atomic no-replace publication checkpoint](../evidence/crash-recovery/2026-08-03-atomic-cas-publication/README.md) replaces the crash-exposed hard-link/unlink publication pair with Darwin `renamex_np(..., RENAME_EXCL)` and Linux `renameat2(..., RENAME_NOREPLACE)`, with no overwrite-capable fallback. Six pipe-synchronized Darwin `SIGKILL` cases cover root, `sha256`, and shard `mkdir` after creation but before parent `fsync`, immediately before rename, immediately after rename, and after rename before directory `fsync`. A narrow Linux/arm64 container smoke exercises the production syscall helper. These results are not power-loss evidence or full rootless x86_64 Linux integration, and temporary creation/partial-write/file-`fsync` kill points remain open.

The [cooperative maintenance-lease checkpoint](../evidence/storage-integrity/2026-08-03-maintenance-lease/README.md) adds one persistent state-local single-link lock opened and created without following its leaf. Fixture publication holds a shared nonblocking `flock` from before its first CAS write through the final SQLite bind. Guarded recovery and budget terminalization hold the same shared lease from before their fresh audit through final commit; nested same-process recovery safely reuses the shared kernel handle. Exclusive maintenance conflicts with either mode, and a valid `quarantine/v1/ACTIVE` marker blocks shared mutation. A missing, non-directory, or symlinked state leaf remains no-create. Any existing real state directory with a usable lock path—including empty or damaged storage, an unknown run, or an ACTIVE fence—may gain and synchronize the persistent lock before later classification or denial. Spawned-process tests cover shared coexistence, exclusive exclusion, inherited-fork cleanup, abrupt parent exit, and kernel release after `SIGKILL`.

This implements the cooperative maintenance window for supported high-level paths, but
it is not a same-UID hostile-process security boundary. Direct low-level store users must
participate explicitly, and a hostile co-tenant can ignore `flock` or race checked
pathnames.

### 2026-08-03 resumable quarantine checkpoint

The [resumable orphan-quarantine checkpoint](../evidence/storage-integrity/2026-08-03-resumable-quarantine/README.md)
adds the explicit `guildmind quarantine --state-dir ...` maintenance action specified by
[ADR 0005](0005-resumable-orphan-quarantine.md). It takes exclusive mode before obtaining
its own fresh top-level audit and moves nothing unless the complete finding set contains
only ownerless valid-finalized, corrupt-finalized, or temporary regular files. A
content-addressed BEFORE/PLAN/ACTIVE record chain precedes any move. Candidate publication
uses descriptor-relative atomic no-replace rename, syncs the destination and source
directories, and writes deterministic receipts; AFTER/COMPLETE evidence precedes durable
fence removal. Resume accepts exactly one of the planned source or destination and can
repair the post-rename/pre-receipt window without overwriting or deleting either side.

The implementation and in-process interruption/fault regressions establish this protocol
surface. A follow-up [local Darwin matrix](../evidence/storage-integrity/2026-08-03-quarantine-process-crash/README.md)
covers all 16 predeclared quarantine `SIGKILL` prefixes plus six cooperating
publisher/maintainer/resumer cases, fresh-process completion, and an identity-preserving
second no-op. Broader open-process and hostile same-UID stress,
remaining CAS publication kill points, power-loss testing, and rootless x86_64
reference-host repetition remain required.

## Consequences

The initial system is operationally small and supports atomic evidence updates. A single writer limits write throughput by design; evaluation workers may run concurrently while their result messages serialize at commitment. The artifact directory and SQLite database must be backed up and restored as one evidence set.

## Alternatives considered

- Store all payloads in SQLite: simple, but poor for large logs, patches, and future object-storage migration.
- JSONL as the primary store: append-friendly but cannot atomically coordinate events, budget, state, and artifact references.
- PostgreSQL and object storage immediately: useful later, but unnecessary operational surface for the local pilot.

## Acceptance checks

- Kill tests at every blob, request, event, ledger, and transaction boundary leave a reconstructable state with no dangling committed reference or erased spend.
- Concurrent workers cannot assign event sequence numbers or mutate the ledger.
- Partial writes, corrupt bytes, hash mismatches, illegal state transitions, broken previous-event links, orphan files, and absent terminal artifacts are detected.
- JSONL and analysis exports regenerate deterministically from committed state after normalizing declared diagnostic timestamps.
