# ADR 0005: Resumable Orphan Quarantine

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-08-03

## Context

Guildmind can verify the complete SQLite ledger, recursively verify every reachable
content-addressed artifact, and classify unreferenced filesystem entries. Supported
runtime mutation also participates in one cooperative state-wide maintenance lease.
Those controls deliberately do not move an orphan: a read-only audit is not an
authorization capability, and a process can die between any filesystem mutation and
the record intended to explain it.

Quarantine must preserve ambiguous bytes for inspection without ever moving reachable
evidence, overwriting a collision, or reopening supported writers halfway through a
transaction. A restart must be able to distinguish a pending move from a completed move
whose receipt was not yet written.

## Decision

Implement quarantine as a forward-only, same-filesystem, no-replace rename protocol.
The public entry point accepts only a state-directory path. It never accepts a
caller-supplied audit, report, candidate list, plan, or receipt as authority.

### Authorization and scope

The transaction first acquires the state-wide maintenance lease in **exclusive** mode.
While holding that lease it performs a fresh top-level `audit_storage()` and requires
the returned `StorageIntegrityReport.quarantine_allowed` gate. The lower-level
`ArtifactAudit.quarantine_allowed` value is insufficient: without a valid authoritative
database, a zero-root CAS scan cannot establish that its bytes are unreferenced.

A transaction may contain only these ownerless finding kinds:

- `valid_finalized_orphan`;
- `corrupt_finalized_orphan`; and
- `temp_orphan`.

This is a whole-transaction allowlist. Any other finding—including an owner-bearing
finding, hard link, symbolic link, special file, noncanonical entry, scan error, or
limit exhaustion—denies the transaction before `ACTIVE` or any move is created. A clean
authorized report with no findings is a no-op and does not create a quarantine
transaction; a non-clean report with no actionable findings is denied.

The exclusive lease spans the fresh audit, plan construction, fence publication,
candidate revalidation, every move and receipt, restart reconciliation, final audit,
completion publication, and fence removal. A resumer repeats the fresh top-level audit
and verifies the planned ledger commitment; on-disk transaction records are evidence,
not authority.

### Durable layout and records

The versioned namespace is:

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

Every controlled component has its exact on-disk spelling and is traversed without
following links. Controlled directories are revalidated by type, device, and inode;
records and payload files must also be single-link regular files. Records are bounded
canonical JSON, immutable, mode `0600`, and published with an atomic no-replace rename.
Each newly created or inherited controlled directory entry is parent-`fsync`ed before a
later durable claim depends on it.

`BEFORE.json` is a canonical typed envelope containing the complete initial
`StorageIntegrityReport`. It is the durable BEFORE evidence, not a projection or summary
of that report.

`PLAN.json` binds the exact BEFORE hash (and therefore the complete artifact audit),
verified all-run ledger commitment, ordered reachable-artifact commitment, captured
state, database, and artifact-root identities, and ordered candidate inventory. For
every candidate it binds the finding kind, exact canonical source path, deterministic
payload destination, expected digest when one exists, observed SHA-256, byte length,
regular-file/single-link requirement, and captured source identity. Every candidate
device must match the state/quarantine filesystem before `ACTIVE` is created; nested
mounts and other cross-device layouts are denied before fencing.

Both `transaction_id` and `candidate_id` are deterministic full lowercase 64-character
SHA-256 values derived from their declared canonical identity material. They are never
random, truncated, or caller-selected. Each candidate identity binds its source,
classification, byte identity, and captured file identity. The transaction identity
binds the schema, exact BEFORE hash, state/ledger/reachable/artifact identities, and
ordered candidate IDs. The destination is then derived without ambiguity as
`transactions/<transaction_id>/payload/<candidate_id>`. `PLAN.json` includes both IDs
and all non-circular identity material required to recompute them.

`ACTIVE` binds the transaction ID and exact BEFORE and PLAN hashes. The maintenance
layer need not parse it to block a shared lease, but quarantine must parse and verify
its canonical contents before resuming.

A deterministic receipt is the per-candidate move evidence. It directly binds the same
transaction, plan, candidate, source, destination, observed digest, size, and
rename-stable file-identity fields; the plan and candidate commitments transitively bind
the complete pre-rename identity, including `ctime_ns`, and the finding kind. A receipt
is published only after source absence and exact destination presence have been freshly
verified. It does not claim an unrecoverable process ID or move timestamp.

`AFTER.json` is a canonical typed envelope containing the complete final
`StorageIntegrityReport` after all candidates and receipts reconcile. It is the durable
AFTER evidence. `COMPLETE.json` binds the transaction ID, exact BEFORE, PLAN, and AFTER
hashes, and the ordered receipt hashes.

An exact prepared `BEFORE.json` and `PLAN.json` may be reused only while holding a newly
acquired exclusive lease, after a fresh top-level audit produces the exact same canonical
BEFORE bytes and all planned state, artifact, reachable, ledger, and candidate identities
are freshly revalidated. A nonmatching prepared transaction remains immutable evidence
and cannot authorize a move.

Existing exact records may be accepted only after complete byte and identity
verification. A conflicting record or destination is never overwritten or treated as
deduplication.

### Durable ordering

For a new transaction, the maintainer performs these steps in order:

1. Acquire the exclusive lease and obtain a fresh authorized coordinator audit.
2. Open each candidate without following links, hash it under the declared bounds, and
   verify stable path/file identity, regular-file type, and link count of one.
3. Atomically publish and parent-`fsync` canonical `BEFORE.json`, then deterministic
   `PLAN.json`. An exact prepared pair is reusable only after the fresh matching audit
   and identity checks described above.
4. Atomically publish `ACTIVE`, then `fsync` `quarantine/v1`. No source may move before
   this sync succeeds.
5. Reconcile already moved destinations and repair their directory durability and
   missing receipts, then perform one fresh authoritative audit. Require the exact
   ledger/reachable commitments and the exact remaining candidate inventory. The
   exclusive lease prevents supported publishers from changing that view during the
   pending move loop.
6. Immediately revalidate each pending source file, file-`fsync` it, and atomically
   rename it to its absent deterministic destination without replacement, and `fsync`
   both destination and source directories. There is no copy/unlink or overwrite-
   capable fallback.
7. Verify the destination and source absence, atomically publish the deterministic
   receipt, and `fsync` the receipt directory.
8. Repeat steps 6–7 in canonical candidate order.
9. Rerun the authoritative ledger/reachable-CAS audit and require a clean report.
10. Atomically publish the complete canonical final report as `AFTER.json` and `fsync`
    its directory.
11. Verify every expected receipt, then atomically publish `COMPLETE.json`, binding
    BEFORE, PLAN, AFTER, and every ordered receipt hash.
12. Exact-check the payload, receipt, and transaction namespaces against the durable
    plan and `fsync` the transaction directory.
13. Unlink `ACTIVE`, `fsync` `quarantine/v1`, and only then release the exclusive lease.

No already moved payload is moved back. A failure after `ACTIVE` publication and before
its final unlink leaves the fence in place for an exclusive resumer. Cleanup code must
not remove the fence in a `finally` block. If the unlink succeeds but its parent `fsync`
or later lease cleanup fails, COMPLETE and the carried result remain authoritative but
durable unfencing is not claimed; the caller receives a finalization failure and must
re-invoke or inspect. Failure before durable `ACTIVE` is safe only because no move is
then permitted.

### Restart reconciliation

For every planned candidate, a resumer verifies the exact source and destination without
following links and applies this table:

| Source | Destination | Interpretation | Action |
|---|---|---|---|
| exact planned file present | absent | pending | Revalidate authority and candidate, then perform the move. |
| absent | exact planned file present | moved; receipt may have been interrupted | Re-`fsync` both directories, verify bytes and identity, then verify or publish the deterministic receipt. |
| present | present | collision, tamper, or an impossible protocol state | Preserve both, retain `ACTIVE`, and fail closed even when the bytes match. |
| absent | absent | ambiguous data loss | Retain `ACTIVE` and fail closed. |

A destination with the wrong bytes, size, type, link count, name, or identity is not the
planned destination. BEFORE, PLAN, ACTIVE, and required controlled ancestors must be
present and exact. A malformed, replaced, linked, or conflicting existing record is an
integrity denial. An order-consistent missing receipt, AFTER, or COMPLETE is a normal
resumable crash prefix and is deterministically reconstructed only after the preceding
evidence is revalidated; a missing earlier record beside later durable evidence is
repairable only when every preceding commitment still verifies exactly. A changed ledger
commitment, newly reachable candidate, damaged reachable evidence, or unexpected CAS
entry also retains `ACTIVE` and denies completion.

Once valid `COMPLETE.json` evidence exists, a resumer may verify the complete BEFORE →
PLAN → receipts → AFTER → COMPLETE chain and finish removing a remaining `ACTIVE` fence.
Once `ACTIVE` is durably absent, the completed transaction is an idempotent no-op;
immutable reports, plans, payloads, receipts, and completion records remain as evidence.

## Consequences

The protocol can recover the otherwise ambiguous window in which a rename succeeded
but its receipt did not. It preserves suspect bytes outside the live CAS while keeping
their original classification and verified identity. Disk use does not fall: quarantine
is evidence preservation, not garbage collection.

A damaged or externally altered transaction can intentionally leave supported mutation
blocked until an operator repairs or adjudicates it. This is preferable to reopening
the runtime with unexplained missing evidence. Cross-device layouts and filesystems
without the required atomic no-replace primitive fail closed.

This decision does not add automatic startup quarantine. Invocation remains explicit.
It does not delete quarantined bytes, perform garbage collection or secure erasure,
revoke already-open file descriptors, or authorize moving anything from an invalid or
missing ledger. It is a cooperative local protocol, not protection from a hostile
same-UID process that ignores `flock` or races pathnames. It makes no sudden-power-loss,
storage-controller, network-filesystem, Linux reference-host, or x86_64 reference-host
claim until those environments receive separate evidence.

## Alternatives considered

- **Delete audited orphans:** rejected because it destroys crash and integrity evidence.
- **Move first, then discover what moved:** rejected because death before discovery
  leaves an unprovable missing source.
- **Write a receipt before moving:** rejected because death after the receipt can leave
  a false claim.
- **Copy then unlink:** rejected because partial copies, cross-device behavior, and two
  independently durable names make reconciliation ambiguous.
- **Treat destination existence as idempotency:** rejected because a collision may be
  unrelated or corrupt even when its apparent bytes match.
- **Quarantine every ownerless finding:** rejected because links, special files, and
  noncanonical entries do not satisfy the regular single-link move invariant.

## Predeclared acceptance checks (not yet satisfied)

The implementation and deterministic unit faults exercise the protocol, but the complete
spawned-process, fault, concurrency, and reference-host proof below remains open.

- Fresh top-level coordinator authorization is obtained only after exclusive lease
  acquisition; missing/invalid databases and every non-allowlisted finding cause zero
  source moves.
- BEFORE/PLAN/ACTIVE/receipt/AFTER/COMPLETE publication is atomic and no-replace, and
  fault tests prove the required file and parent-directory `fsync` order.
- A pipe-synchronized spawned-process `SIGKILL` matrix covers: BEFORE publication before
  directory sync; PLAN publication before directory sync; ACTIVE publication before
  directory sync; immediately before the first move; immediately after successful
  rename; between the two directory syncs;
  after both syncs but before receipt; receipt publication before directory sync; one
  durable receipt before a second candidate; all receipts before AFTER; AFTER
  publication before directory sync; durable AFTER before COMPLETE; COMPLETE publication
  before directory sync; durable COMPLETE before ACTIVE removal; ACTIVE unlink before
  parent sync; and parent sync before exclusive-lease release.
- Every kill prefix has the expected `ACTIVE`, source, destination, receipt, and
  BEFORE/PLAN/AFTER/COMPLETE state. A fresh process either resumes to one exact completed
  transaction or fails closed without overwriting or deleting ambiguous bytes. A second
  resume changes no payload or record identity.
- At least one interrupted move/reconciliation is exercised for each of valid
  finalized, corrupt finalized, and temporary orphans.
- Live publishers blocked with a temporary and with a finalized-but-unbound artifact
  keep quarantine lease-busy. After their synchronized death, exclusive quarantine
  classifies and moves only the resulting allowlisted orphan.
- While exclusive quarantine is live, shared runtime/recovery and a second maintainer
  are nonblockingly denied. After a kill with durable ACTIVE, shared mutation remains
  fence-blocked while one exclusive resumer succeeds. Two simultaneous resumers produce
  one winner, one busy denial, and no duplicate move or receipt.
- After durable ACTIVE removal but before maintainer exit, shared mutation remains
  lease-busy; process death releases the kernel lease and later shared mutation can
  proceed.
- Source, destination, record, ancestor, snapshot, reachability, link, case-alias,
  replacement, collision, `EXDEV`, unsupported-syscall, and `fsync` fault cases preserve
  all suspect/outside bytes and, once `ACTIVE` exists, retain the fence whenever
  completion is not proven.
- Development `SIGKILL` results are labeled process-crash evidence only. Power loss,
  hostile same-UID races, open-descriptor revocation, deletion/GC, automatic startup,
  and reference-host validation remain explicit non-claims.
