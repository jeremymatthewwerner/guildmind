# 2026-08-02 Recursive Storage-Integrity Audit

**Evidence tier:** host-independent development tests and adversarial code review<br>
**Implementation checkpoint:** `6527431` (`Add typed recursive artifact integrity audit`)<br>
**Stage 1 effect:** integrity-audit capability implemented; automatic recovery gating and
orphan quarantine were not yet implemented at this checkpoint; Stage 1 remains
**NOT PASSED**

## Scope

This checkpoint binds a verified SQLite ledger snapshot to a bounded, recursive,
read-only inventory of the filesystem content-addressed store. It answers three
separate questions without mutating storage:

1. Which artifact digests are reachable from every verified current run manifest?
2. Do those bytes and the typed relationships inside task/evaluation artifacts agree
   with their committed identities?
3. Which finalized blobs, temporary entries, malformed paths, links, or special files
   exist outside that trusted reachability graph?

The audit is deliberately not a garbage collector. Its `quarantine_allowed` result is
true only when the inventory completed, every reachable byte was verified, and no
owner-bearing integrity finding exists. A later checkpoint must still revalidate the
ledger snapshot and filesystem boundary immediately before any move.

## Implemented controls

- `EventStore.verify_integrity()` validates SQLite structural and foreign-key checks,
  complete canonical manifest history, replay/index/budget agreement, terminal state,
  and the event-chain head for every run in one consistent read transaction. It emits
  ordered `VerifiedRunRoot` commitments containing the current manifest revision/hash,
  event count, and head-event hash.
- `audit_artifact_store()` derives a deterministic snapshot hash from those roots,
  verifies every direct manifest reference, and recursively follows only the declared
  `TaskSpec` and `EvaluationResult` artifact roles.
- Typed cross-checks bind task ID, environment image, task content hash, evaluator
  version, run ID/status, patch hash, and nested evidence references back to the
  manifest and task artifact that authorize them.
- Structured artifacts must be canonical JSON. Duplicate/noncanonical encodings,
  `NaN`/infinity values, malformed typed objects, incorrect media roles, conflicting
  metadata, missing bytes, size mismatches, and digest mismatches are findings rather
  than trusted reachability.
- Bounded streaming limits cover root count, recursive-reference count, inventory
  entries, individual artifacts, structured artifacts, and total hashed bytes. Claimed
  oversize files stop before content reads; total hashing stops before exceeding the
  global budget.
- Inventory uses `lstat`, does not follow links, distinguishes valid and corrupt
  finalized orphans from temporary/noncanonical/symlink/special entries, and handles
  undecodable names as JSON-safe scan findings where the host filesystem permits them.
- Artifact-store operations capture directory device/inode identities, reject later
  root or ancestor replacement, reject configured links below an explicit trusted
  path boundary, and preserve standard operating-system aliases above that boundary.
- `FixtureRunner` captures and rechecks its state-directory identity before run and
  recovery entry, preventing deterministic post-construction replacement from
  redirecting SQLite or CAS writes.

## Adversarial review outcomes

Independent review reproduced and then closed false-safe cases involving:

- pre-existing and post-initialization root/ancestor replacement;
- macOS case-folded aliases and standard `/tmp`/`/var` path aliases;
- deserialized reports claiming unverified bytes were safe;
- oversized structured artifacts and cumulative hash exhaustion;
- noncanonical JSON and non-finite numeric values;
- invalid UTF-8 entry names;
- arbitrary reachable `storage_ref` values;
- same-digest references with conflicting metadata; and
- runner state replacement between construction and execution/recovery.

The final targeted review found no remaining deterministic P0/P1 within the stated
quiescent maintenance model. This is not a claim of safety against a hostile process
swapping path components concurrently with individual checks and opens.

## Reproduction

The checkpoint passed the repository gate on macOS:

```console
$ make check
81 files already formatted
All checks passed!
Success: no issues found in 57 source files
329 passed, 28 skipped
```

The 28 skips were the already-declared image/reference-host integration cases plus one
APFS platform skip where the filesystem itself rejected construction of an invalid
UTF-8 filename. A direct `/tmp` smoke test confirmed that a trusted macOS alias is
canonicalized to `/private/tmp` while the configured artifact-root component remains
no-follow.

## Evidence boundary and remaining work

The result is authoritative only while one exclusive-writer, quiescent maintenance
window spans ledger verification, CAS inventory, and any later action based on the
report. Descriptor-relative traversal or an equivalent locked maintenance protocol is
still required before claiming protection from concurrent out-of-band path swaps.

At this checkpoint the normal `EventStore` and `FileArtifactStore` constructors could
still create missing storage, recovery did not yet require an audit, and no orphan was
moved automatically. Therefore:

- a missing database must not be mistaken for a verified empty ledger;
- recovery must be gated on an existing, valid ledger and verified referenced bytes;
- the audited ledger snapshot must be checked again inside the recovery transaction;
- orphan quarantine needs a durable plan, same-filesystem no-replace moves, directory
  `fsync`, restart/resume semantics, and injected-crash tests; and
- CAS writer races and the 99% normal-fixture reliability campaign remain open.

These limits retain the **NOT PASSED** Stage 1 verdict and do not authorize external
repositories, arbitrary model-generated commands, or provider-backed experiments.

## 2026-08-03 no-create coordination follow-up

The next implementation slice adds the read-only coordination needed to consume this
audit safely without confusing absent storage with an empty ledger:

- `EventStore.open_existing_read_only()` rejects missing, linked, non-regular,
  zero-byte, wrong-schema, and replaced database paths without creating the main
  database or schema. Main-database and WAL/SHM hard links are rejected. It reads
  committed nonempty WAL state when an existing usable WAL/SHM pair survives a crash;
  an unmatched sidecar, non-WAL database header, or hot rollback journal fails closed.
- `EventStore.verified_snapshot()` retains the validated SQLite read transaction while
  the CAS graph is scanned. The shared `verified_run_roots_sha256()` function is the
  single commitment implementation used by the ledger, audit, and coordinator.
- `FileArtifactStore.open_existing_read_only()` requires the complete no-follow
  directory chain to exist and rejects publication, so a preflight/open disappearance
  cannot recreate the CAS.
- `audit_storage()` classifies uninitialized, database-missing-with-artifacts, invalid
  database, initialized-empty, initialized-empty-with-findings, audit-incomplete,
  referenced-evidence-invalid, healthy-with-findings, and healthy states. Its
  initialization/read/mutation/quarantine/clean booleans are derived observations;
  the serializable report is explicitly not an authorization capability.
- `EventStore.open_existing_writable()` uses SQLite `mode=rw`, requires an existing
  valid WAL Guildmind schema, and never creates or migrates it. Exact schema objects,
  DDL, columns, foreign keys, and indexes are rechecked inside every transaction on
  that existing-only handle, so a trigger or altered schema added after open cannot
  run a lifecycle mutation.
- The ordinary create-or-open constructor now reserves a genuinely new database with
  exclusive no-follow creation. Any pre-existing leaf is routed through the same
  strict opener, and existing SQLite sidecars without a main database fail before a
  new main file is created. Tracked transactions validate the full all-run ledger both
  before mutation and after staged writes.
- An optional `recover_run(expected_snapshot_sha256=...)` precondition recomputes the
  complete verified ledger commitment inside `BEGIN IMMEDIATE` before any lifecycle
  SQL. Recovery revalidates the complete ledger again after staging its changes and
  before commit, rolling back cross-run damage. At this intermediate follow-up, runtime
  integration still needed to supply the precondition and same-connection exception
  terminalization had not yet been routed through it.

The expanded repository gate passed with 439 tests and the same 28 declared skips. At
this no-create follow-up, external `FixtureRunner`/CLI recovery was not yet routed
through these controls, so the **NOT PASSED** verdict and the remaining-work list above
still applied.

Follow-up adversarial review reproduced and closed false-safe cases involving
hard-linked databases and sidecars, replacement of a controlled ancestor while the
same database inode remained open, a valid-looking schema with a cross-run trigger,
schema alteration after an existing-only writer was opened, default-constructor
reopening through a database-leaf link or unchecked schema, corruption in an unrelated
run, noncanonical event or budget JSON, mismatched SQL event identity columns, and a
true rollback-journal-mode database misreported through an immutable reader. The
remaining path check/open and precommit windows are not atomic against an actively
racing same-user process; the documented quiescent exclusive-writer requirement still
applies.

## 2026-08-03 guarded recovery follow-up

The next [guarded-recovery checkpoint](../../crash-recovery/2026-08-03-guarded-recovery/README.md)
now consumes this audit from explicit local-fixture recovery. It performs a fresh
existing-only coordinator audit, requires the requested run in the verified snapshot,
and then recomputes the complete ledger commitment and recursive CAS audit inside the
SQLite writer transaction before staging recovery. After staging, it validates the full
ledger and repeats the recursive CAS/path guard against the post-mutation roots at the
final pre-commit boundary. The recovered manifest and event stream are captured inside
that transaction; SQLite writer failures are normalized to a typed denial. The
`FixtureRunner` exception path now closes its ordinary writer and invokes this same
guarded existing-only recovery. Pre-dispatch budget refusal uses the same fresh audit and
dual writer-window guard; a budget error after dispatch receives conservative general
recovery. Normal evaluation completion and guarded terminalization capture their returned
manifest/event stream within their write transaction. `replay` and `report` use
existing-only read-only SQLite inspection and do not create missing state.

The expanded focused terminalization/inspection slice passed 51 cases with 131
deselected in 1.09s. The final full repository test run passed 487 cases with 28 declared
skips.

That integration does not move the orphans classified here and does not remove this
document's quiescent same-UID boundary. Resumable quarantine, CAS publication kill
points, concurrency stress, provider behavior, reference-host repetition, and the 99%
normal-fixture campaign remain open; Stage 1 is still **NOT PASSED**.

## 2026-08-03 resumable quarantine follow-up

The later [resumable quarantine checkpoint](../2026-08-03-resumable-quarantine/README.md)
now consumes the top-level `quarantine_allowed` observation correctly: the public entry
point accepts only a state path, acquires exclusive maintenance, and obtains a new report
itself. It denies the whole operation unless every finding is one of the three authorized
ownerless regular-file classes, then repeats the audit after hashing candidates. It binds
the full report and candidate identities into immutable content-addressed records before
publishing ACTIVE or moving a source.

Candidate moves use descriptor-relative same-filesystem atomic no-replace rename and
directory-sync ordering. Restart revalidates the original ledger/reachable commitment,
requires every planned file at exactly its source or destination, and can repair the
post-rename/pre-receipt window. The implementation includes deterministic interruption
regressions, not yet the complete spawned-process `SIGKILL`/concurrency matrix. The
same-UID, power-loss, remaining CAS, provider, reference-host, and 99% campaign limits
therefore remain, and Stage 1 is still **NOT PASSED**.
