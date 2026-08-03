# Quarantine Process-Crash and Concurrency Matrix — 2026-08-03

**Evidence level:** local Darwin development tests with spawned processes and real
POSIX `SIGKILL`<br>
**Scope:** every predeclared resumable-quarantine crash prefix plus cooperative
publisher, maintainer, fence, resumer, and lease-release concurrency<br>
**Stage 1 effect:** closes the local process-crash and cooperative-concurrency matrix
predeclared by [ADR 0005](../../../decisions/0005-resumable-orphan-quarantine.md);
Stage 1 remains **NOT PASSED**

## Claim established

The implemented quarantine protocol can be killed at each of its 16 declared durable
ordering boundaries and then inspected and resumed by a newly spawned process. For the
first 14 boundaries, the fresh process completes the one existing transaction. For the
final two boundaries, `ACTIVE` is already absent, so the fresh process returns a clean
no-op while preserving the already completed transaction. A second fresh invocation is
always a clean, identity-preserving no-op.

The matrix starts with three simultaneously present ownerless findings: one valid
finalized orphan, one corrupt finalized orphan, and one temporary orphan. The selected
post-rename reconciliation boundaries deliberately target a different finding class,
so all three allowed classes exercise the moved-without-receipt restart path. Resume
never creates a replacement transaction, moves a payload back, overwrites a destination,
or accepts an unverified receipt.

Six additional spawned-process cases establish the intended cooperative exclusion
rules. A conforming shared publisher prevents exclusive quarantine while publication is
in flight; a live exclusive quarantiner prevents shared mutation and another maintainer;
a durable `ACTIVE` fence keeps shared mutation blocked after the original process dies;
and overlapping resumers yield one winner and one typed busy denial. Even after durable
unfencing, the exclusive kernel lease remains held until maintainer exit.

Both test modules use `multiprocessing` with the `spawn` start method. Test-only wrappers
perform the real production rename, record publication, `fsync`, unlink, move, receipt,
and lease operations, announce the exact semantic boundary over a pipe, and block. The
parent acts only after receiving that announcement. There are no production crash hooks,
timing sleeps, or scheduler guesses.

## Exact 16-boundary process-crash matrix

The boundary names below are the literal parameter values in
[`test_quarantine_process_crash.py`](../../../../tests/integration/test_quarantine_process_crash.py).
"Observed prefix" means the filesystem state observed after the parent delivered
`SIGKILL`; it is not a claim that an unsynchronized entry would survive sudden power
loss.

| # | Exact kill boundary | Observed prefix after `SIGKILL` | Fresh-process result |
|---:|---|---|---|
| 1 | `before_published_before_transaction_sync` | Canonical `BEFORE.json` is present; PLAN and ACTIVE are absent; all three sources remain. | Reuses the prepared evidence and completes the same transaction; `resumed=false`. |
| 2 | `plan_published_before_transaction_sync` | Canonical BEFORE and PLAN are present; ACTIVE is absent; all sources remain. | Revalidates and reuses the exact prepared plan, then completes the same transaction; `resumed=false`. |
| 3 | `active_published_before_version_sync` | BEFORE, PLAN, and canonical ACTIVE are present; no candidate has moved. | Resumes the fenced transaction and completes it; `resumed=true`. |
| 4 | `immediately_before_first_move` | ACTIVE has reached the pre-move protocol point; payload and receipt directories are empty and all sources remain. | Resumes the same fenced transaction and moves all three candidates. |
| 5 | `immediately_after_selected_rename` | The selected valid-finalized orphan has changed from its source name to its exact payload name without its receipt; any earlier canonical candidates are already receipted. | Reconciles absent source plus exact destination, repairs the receipt, and completes. |
| 6 | `between_payload_and_source_parent_sync` | The selected corrupt-finalized orphan is under its exact payload name after the payload-directory sync returned but before the source-parent sync; its receipt is absent. | Re-synchronizes and verifies both sides, publishes the deterministic receipt, and completes. |
| 7 | `after_both_move_directory_syncs_before_receipt` | The selected temporary orphan has moved and both directory sync calls have returned; its receipt is absent. | Verifies the exact destination and source absence, publishes the receipt, and completes. |
| 8 | `receipt_published_before_receipts_sync` | The first candidate and its canonical receipt are present, but the receipt-directory sync has not returned. | Verifies or reconstructs the ordered receipt prefix and completes the remaining candidates. |
| 9 | `first_durable_receipt_before_second_candidate` | Exactly the first candidate is moved and receipted; the other two sources remain. | Preserves the first payload and receipt and completes candidates two and three. |
| 10 | `all_receipts_durable_before_after` | All three payloads and receipts are present; AFTER and COMPLETE are absent; ACTIVE remains. | Re-audits clean storage, publishes AFTER and COMPLETE, then unfences. |
| 11 | `after_published_before_transaction_sync` | Canonical clean `AFTER.json` is present; COMPLETE is absent; ACTIVE remains. | Verifies the preceding chain, publishes COMPLETE, and unfences. |
| 12 | `durable_after_before_complete` | AFTER publication and its transaction-directory sync have returned; COMPLETE is absent. | Publishes the exact completion commitment and unfences. |
| 13 | `complete_published_before_transaction_sync` | Canonical `COMPLETE.json` is present and binds the full chain; ACTIVE remains. | Verifies COMPLETE, finishes its durability sequence, and unfences. |
| 14 | `durable_complete_before_active_removal` | COMPLETE publication and transaction sync have returned; ACTIVE remains. | Verifies the completed transaction, removes and syncs ACTIVE, and returns the same completion. |
| 15 | `active_unlinked_before_version_sync` | The completed transaction is intact and ACTIVE is absent, but the version-directory sync has not returned. | The fresh audit is clean and the invocation returns `no_op`; existing completion evidence is unchanged. |
| 16 | `version_synced_before_lease_release` | ACTIVE is absent and the version-directory sync has returned, while the killed process still owns the exclusive lease. | Before the kill, a shared attempt is `lease_held`; death releases the kernel lease, and the fresh invocation returns `no_op`. |

For boundaries 1–14, the first restart returns `completed`, names the same transaction,
quarantines exactly three candidates, reports a clean final audit, and returns the hash
of the verified COMPLETE record. Boundaries 3–14 additionally report `resumed=true`
because ACTIVE was present; the two prepared-record prefixes before ACTIVE are validated
and reused without being labeled an ACTIVE resume. For boundaries 15–16, the first
restart returns `no_op`, no transaction ID, zero newly quarantined candidates, and no new
completion hash because the prior completed transaction already left live storage clean.

## Exact six-case concurrency matrix

[`test_quarantine_concurrency.py`](../../../../tests/integration/test_quarantine_concurrency.py)
contains six collected cases; the first test is parameterized into two distinct
publication states.

| # | Case | Synchronized observation |
|---:|---|---|
| 1 | Shared publisher, temporary name not yet bound to the final name | The publisher holds a shared lease and blocks immediately before its no-replace rename. Quarantine is denied with `maintenance_denied` caused by `lease_held`, creates no quarantine namespace, and after synchronized publisher death classifies and moves only the exact temporary orphan. |
| 2 | Shared publisher, finalized name bound but not yet returned to the caller | The real no-replace rename has succeeded while the publisher still holds its shared lease. Quarantine is denied with the same typed causes; after publisher death the audit finds exactly one valid-finalized orphan and quarantine preserves its exact bytes. |
| 3 | Live exclusive quarantine versus shared mutation and a second maintainer | The quarantiner holds exclusive mode with durable ACTIVE and blocks before its first move. A direct shared acquisition, a cooperating `FileArtifactStore` mutation guarded by shared mode, and a second quarantine attempt all receive `lease_held`; the mutation target is absent. Releasing the first process yields one completed transaction. |
| 4 | Original maintainer killed after durable ACTIVE | The candidate source is still present and ACTIVE names the original transaction. Shared acquisition is denied as `quarantine_active`; a later exclusive invocation reports `resumed=true`, uses the same transaction and candidate IDs, moves the source once, and completes. |
| 5 | Two overlapping resumers | A prepared durable-ACTIVE transaction is the only transaction directory. The gated winner acquires exclusive mode and blocks before its move; the contender receives `maintenance_denied` caused by `lease_held`. The winner completes the original transaction, and the transaction-directory set, payload count, and receipt count prove no duplicate transaction, move, or receipt. |
| 6 | ACTIVE durably removed while exclusive lease is still live | The real unfencing operation has returned: ACTIVE is absent, the source has moved, and completion evidence exists, but shared acquisition still receives `lease_held`. `SIGKILL` releases the kernel lease; shared mode then succeeds, a later quarantine is `no_op`, and exactly one payload and one receipt remain. |

The publication cases are intentionally about a cooperating low-level publisher holding
the documented shared lease around `FileArtifactStore` publication. The mutation case
similarly exercises the shared mutation guard before the low-level store call. These are
cross-process protocol tests, not new end-to-end `FixtureRunner` publication cases and
not evidence that an uncooperative low-level caller is automatically fenced.

## Hash-chain, namespace, and identity assertions

The process-crash test checks the interrupted prefix before any resume and the completed
state after each restart. In particular, it requires:

- the version directory to contain exactly `transactions` plus ACTIVE only for the
  declared fenced prefixes, and the one transaction directory to contain exactly the
  records and subdirectories allowed at that prefix;
- every present record to be a single-link regular file with mode `0600`, strict model
  validation, and bytes exactly equal to its canonical JSON serialization;
- BEFORE to equal the canonical independent parent-process audit captured before the
  crash child starts, PLAN to contain exactly the three expected source paths and
  candidates, and
  `PLAN.body.before_sha256 == canonical_sha256(BEFORE)`;
- ACTIVE to name the same transaction and bind the exact canonical PLAN and BEFORE
  hashes;
- every moved payload to retain the planned device, inode, mode, byte length, and
  `mtime_ns`, remain single-link and regular, and hash to the planned content SHA-256;
- every present receipt to equal a freshly reconstructed deterministic receipt binding
  transaction, plan, candidate, source, destination, digest, size, device, inode, mode,
  and `mtime_ns`;
- AFTER to bind the same transaction and equal a fresh independent final audit plus
  both fresh-process result reports;
- COMPLETE to bind the exact transaction, PLAN, BEFORE, and AFTER hashes and the ordered
  candidate-to-receipt hash commitments; and
- a completed transaction to have exactly the three planned payload names and receipt
  names, no remaining planned source, no ACTIVE marker, and a clean independent
  `audit_storage()` result.

Before the first restart, the test snapshots every already present evidence file's
device, inode, mode, link count, size, `mtime_ns`, `ctime_ns`, and SHA-256. Every such
file must have the identical snapshot after completion. The test then snapshots the
whole completed transaction, invokes quarantine in a second fresh process, requires an
exact no-op, and requires the full snapshot to remain identical. This is the explicit
identity-preserving idempotence check.

The concurrency completion helper additionally parses BEFORE, PLAN, AFTER, the one
receipt, and COMPLETE in strict mode and asserts:

```text
COMPLETE.plan_sha256              == SHA256(canonical PLAN)
COMPLETE.before_sha256            == SHA256(canonical BEFORE)
COMPLETE.after_sha256             == SHA256(canonical AFTER)
COMPLETE.receipts[0].receipt_sha256 == SHA256(canonical receipt)
result.completion_sha256          == SHA256(canonical COMPLETE)
```

It also requires exactly one payload and receipt, exact payload bytes, a single payload
link, and matching transaction, candidate, source, and destination identities. The
two-resumer case separately requires that the transaction-directory name set remains
exactly the singleton original transaction before and after the race.

## Verification

Focused process-crash run on the Darwin development host:

```bash
uv run pytest -q tests/integration/test_quarantine_process_crash.py
```

```text
16 passed in 5.13s
```

Focused cooperative-concurrency run:

```bash
uv run pytest -q tests/integration/test_quarantine_concurrency.py
```

```text
6 passed in 0.84s
```

After the final independent-audit and commitment assertions were in place, the combined
checkpoint was rerun:

```bash
uv run pytest -q \
  tests/integration/test_quarantine_concurrency.py \
  tests/integration/test_quarantine_process_crash.py
```

```text
22 passed in 5.98s
```

The complete repository gate was then run:

```bash
make check
```

`make check` expands to formatting and lint checks, static type checking, and the full
Pytest suite. Formatting, lint, and typing passed; Pytest reported:

```text
630 passed, 29 skipped in 13.82s
```

Both focused modules declare skips unless POSIX `SIGKILL` and a supported atomic
no-replace host are available. Neither module was skipped in the recorded Darwin runs.
The 29 repository skips are existing opt-in platform/image cases, not skips inside this
22-case quarantine checkpoint.

## Evidence boundary and remaining Stage 1 work

This is **process-crash** evidence. The child performs a real operation, announces the
named prefix, and is killed; the tests then reason from the namespace the running kernel
left visible. They do not remove power, reboot a host, emulate lost writeback, inspect
device caches, or prove filesystem/controller persistence. In particular, a record or
directory entry observed after death before its parent `fsync` may disappear after a
real power cut. Sudden-power-loss and storage-fault testing remains a separate claim.

This is also a **cooperative** local concurrency protocol. It covers supported actors
that take the shared or exclusive `flock` and honor ACTIVE. It is not a security boundary
against a same-UID process that ignores the lease, directly mutates low-level storage,
races or replaces checked pathnames, or retains an already-open descriptor. The protocol
does not revoke descriptors, secure-delete bytes, garbage-collect payloads, or run
quarantine automatically at startup. Network filesystems and filesystems without the
required atomic no-replace primitive remain outside the claim.

The subsequent [CAS temporary-write checkpoint](../../crash-recovery/2026-08-03-cas-temporary-write/README.md)
closes the local temporary-create, partial-write, and file-`fsync` kill points that were
open when this matrix was first recorded. The remaining Stage 1 gaps include:

- running this complete matrix, with repetition, on the dedicated native rootless
  x86_64 Linux reference host and preserving host/filesystem evidence;
- real-process concurrent CAS publisher stress;
- separate credible sudden-power-loss/storage-fault evidence if that stronger
  durability claim is required;
- hostile same-UID and already-open-descriptor races, which require a stronger isolation
  boundary rather than more cooperative lease cases;
- real-provider idempotency, status polling, duplicate suppression, and billing
  reconciliation; and
- the planned fixture breadth, general hostile-code containment on the reference host,
  and predeclared 99% normal-fixture reliability campaign.

This checkpoint does not authorize external repositories, arbitrary model-generated
commands, provider-backed pilots, institutional search, PettingZoo, persistent memory,
or Stage 2 work. It closes the bounded local matrix that remained open after the
[resumable quarantine checkpoint](../2026-08-03-resumable-quarantine/README.md); it does
not change the overall Stage 1 **NOT PASSED** verdict.
