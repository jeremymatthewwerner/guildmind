# Resumable Orphan Quarantine — 2026-08-03

**Evidence level:** local Darwin development tests and adversarial code review<br>
**Implementation checkpoint:** commit containing this document<br>
**Scope:** explicit local orphan-quarantine protocol, public CLI, and deterministic
in-process interruption/fault regressions<br>
**Stage 1 effect:** the protocol surface is implemented; its spawned-process kill and
concurrency matrix is still open, so Stage 1 remains **NOT PASSED**

## Claim established

`quarantine_orphans()` and `guildmind quarantine --state-dir PATH` provide an explicit
maintenance action for preserving authorized ownerless CAS files outside the live
artifact namespace. The entry point accepts only a state-directory path. It never accepts
a caller-supplied audit, plan, finding list, or receipt as authority.

The operational path:

1. acquires the cooperative maintenance lease in exclusive mode and borrows a revalidated
   descriptor for the already-open real state directory;
2. obtains a fresh top-level `audit_storage()` report and requires its derived
   `StorageIntegrityReport.quarantine_allowed` gate;
3. denies the whole operation before fencing if any finding is not an ownerless
   `valid_finalized_orphan`, `corrupt_finalized_orphan`, or `temp_orphan`;
4. hashes and captures every single-link regular candidate, repeats the authoritative
   audit, and denies nested-mount/cross-device candidates before creating transaction
   records;
5. publishes canonical immutable `BEFORE.json`, content-addressed `PLAN.json`, and
   `quarantine/v1/ACTIVE` before moving a source;
6. immediately revalidates and file-`fsync`s each pending candidate, moves it with a
   descriptor-relative atomic no-replace rename, then syncs the destination and source
   directories before publishing a deterministic receipt;
7. requires a fresh clean final ledger/reachable-CAS audit, publishes canonical
   `AFTER.json` and `COMPLETE.json`, and only then removes and parent-`fsync`s `ACTIVE`;
   and
8. retains all payloads and records as immutable evidence. It deletes no quarantined
   bytes and performs no garbage collection.

An initialized clean store returns an exact `no_op` result without creating the
quarantine namespace. A missing or invalid authoritative database, incomplete scan,
owner-bearing finding, noncanonical entry, link, special file, namespace collision,
candidate change, or unsupported platform primitive fails closed. A real existing state
directory may still gain the persistent maintenance-lock inode before a later denial, as
documented by the maintenance protocol.

The CLI emits stable JSON. A `guildmind.quarantine-denial/v1` response means this
invocation obtained no mutation authority and performed no CAS move; it does not claim
that another lease holder has not already fenced the state. After exclusive authority
and the verified state descriptor have been obtained, a
`guildmind.quarantine-incomplete/v1` response means an exact ACTIVE marker is present or
its absence cannot then be proved safely. Acquisition-time busy or fence-integrity
failure remains an invocation-local denial because this invocation never obtained move
authority. A
`guildmind.quarantine-finalization-failure/v1` response carries the authoritative result
but does not claim that ACTIVE removal, its parent sync, lease release, or later cleanup
all completed; it is phase-neutral so it also describes a clean no-op whose lease release
failed.

## Durable evidence and restart rule

The v1 layout is:

```text
quarantine/v1/
  ACTIVE
  transactions/<transaction_id>/
    BEFORE.json
    PLAN.json
    payload/<candidate_id>
    receipts/<candidate_id>.json
    AFTER.json
    COMPLETE.json
```

Candidate and transaction IDs are full canonical SHA-256 commitments rather than random
or caller-selected names. `ACTIVE` binds the transaction plus exact BEFORE and PLAN
hashes. The plan binds the complete initial report, verified all-run ledger snapshot,
reachable graph, state/database/artifact-root identities, ordered candidate findings,
source identities, sizes, and observed bytes. COMPLETE binds the exact BEFORE, PLAN,
AFTER, and ordered receipt hashes.

On resume, every planned candidate must satisfy exactly one row:

| Source | Destination | Result |
|---|---|---|
| Exact planned file present | Absent | Pending; revalidate after the fresh audit, then move. |
| Absent | Exact planned file present | Re-sync the payload and source directories, revalidate, and verify or synthesize the deterministic receipt. |
| Present | Present | Ambiguous collision; retain both and keep `ACTIVE`. |
| Absent | Absent | Ambiguous loss; keep `ACTIVE`. |

The post-rename/pre-receipt case therefore resumes forward without moving a payload back,
overwriting a destination, or inferring a new candidate. Prepared pre-ACTIVE records are
reusable only when a newly acquired exclusive lease observes the exact same fresh audit
and deterministic plan. A second invocation after completed unfencing is a clean no-op
when storage remains clean and no new orphan has appeared.

## Adversarial review outcomes

Independent review reproduced and closed prerequisite false-safe cases involving:

- C-ABI descriptor truncation aliasing an oversized Python integer to a real file
  descriptor;
- borrowed state descriptors surviving `fork()`;
- case-folded aliases for each `quarantine/v1/ACTIVE` component;
- caller-close/descriptor-number reuse causing cleanup to close an unrelated file or
  suppress a primary exception; and
- a candidate on a nested mount reaching `EXDEV` only after fencing instead of being
  denied before `ACTIVE`.

The protocol also has regressions for whole-operation denial, all three authorized
finding classes, recursive referenced-byte preservation, durable prepared-plan reuse,
post-move receipt synthesis, both/neither ambiguity, corrupted PLAN/ACTIVE records,
database replacement and links, unexplained namespace entries, bounded enumeration,
record-parent sync failures, and descriptor cleanup on identity-check failure.

The final code review found no remaining deterministic P0/P1 within the stated
cooperative local-filesystem model. This is not a claim against a hostile same-UID
process racing the individual checks.

## Verification

The focused quarantine module passed:

```text
28 passed
```

The combined quarantine, descriptor/filesystem, maintenance, CLI, and maintenance-process
selection passed:

```bash
uv run pytest -q \
  tests/unit/test_quarantine.py \
  tests/unit/test_fsops.py \
  tests/unit/test_maintenance.py \
  tests/unit/test_cli.py \
  tests/integration/test_maintenance_lease_process.py
```

```text
124 passed, 1 skipped in 1.34s
```

The focused skip is the platform case where Darwin/APFS rejected construction of an
invalid raw filename. The complete repository gate then reported:

```text
ruff format --check: clean
ruff check: all checks passed
mypy: 70 source files, no issues
pytest: 608 passed, 29 declared skips in 7.84s
```

The additional declared skips are 27 existing container-image/reference-host cases plus
one storage-integrity case where APFS rejected another invalid UTF-8 filename. These
development counts are not the predeclared 99% normal-fixture campaign.

## Evidence boundary and remaining work

This checkpoint establishes code paths, record invariants, deterministic in-process
interruption recovery, and public JSON result/denial surfaces. It does **not** yet
establish the full process-crash claim in [ADR 0005](../../../decisions/0005-resumable-orphan-quarantine.md).
The next checkpoint must use spawned processes, pipe-synchronized boundaries, and real
`SIGKILL` to cover record publication, every rename/directory-sync/receipt window,
multi-candidate progress, COMPLETE publication, ACTIVE removal, and lease release.

It must also exercise cooperating publishers and two simultaneous resumers. Remaining
separate nonclaims are:

- sudden power loss, storage-controller behavior, and network filesystems;
- an actively hostile same-UID process that ignores `flock` or races pathnames;
- revocation of descriptors that another process already opened;
- deletion, garbage collection, secure erasure, or automatic startup quarantine;
- the remaining CAS temporary-create/partial-write/file-`fsync` kill points; and
- native rootless x86_64 Linux reference-host validation.

No external repository, arbitrary model-generated command, provider-backed pilot,
baseline campaign, institutional search, or Stage 2 work is authorized by this result.
