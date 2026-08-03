# Cooperative State-Wide Maintenance Lease — 2026-08-03

**Evidence level:** local Darwin development tests<br>
**Scope:** cooperative exclusion between supported runtime mutation and future
state-wide maintenance<br>
**Stage 1 effect:** supplies the maintenance-window prerequisite; resumable quarantine
is still not implemented and the gate remains **NOT PASSED**

## Claim established

Guildmind now has one persistent, state-local coordination inode:
`.guildmind-maintenance.lock`. `MaintenanceLease` exposes two nonblocking modes over
that inode:

- shared publisher/mutator mode, held by the supported fixture runtime and guarded
  terminalization paths; and
- exclusive maintenance mode, reserved for operations such as the future resumable
  orphan quarantine.

The lock file is created with descriptor-relative `O_CREAT | O_EXCL | O_NOFOLLOW`, is
required to remain a single-link regular file, is file-`fsync`ed, and is followed by an
`fsync` of the already-open real state directory. Existing and newly created lock files
are compared by file type, device, inode, and link count before a kernel lease is
returned. The configured state leaf must be a real non-root directory and must continue
to name the opened directory. A state or lock symlink, hard-linked lock, deterministic
state/lock replacement, failed filesystem operation, or invalid quarantine-fence path
fails with a typed integrity denial.

The kernel lease uses `fcntl.flock` with `LOCK_NB`. Shared leases coexist; an exclusive
lease conflicts with either mode and reports a typed busy denial rather than waiting.
Nested shared use through the same normalized state path in one process reuses one
open-file description behind a locked reference-counted registry. This matters because
`FixtureRunner` exception handling can enter guarded recovery while the runner's outer
shared lease is still held. Case-folded aliases may use separate registry entries and
open-file descriptions, but they still coordinate safely through the same kernel inode.
A forked child does not unlock or adopt the parent's registry entry, and an inherited
lease object cannot enter a context in that child.

`FixtureRunner.run()` acquires shared mode before the first task/CAS publication and
holds it through the final SQLite evidence-binding transaction, including model and
evaluator work and exception terminalization. Explicit recovery and pre-dispatch budget
terminalization acquire shared mode before their fresh authoritative ledger/CAS audit
and hold it through the writer-locked precondition, mutation, final guard, and commit.
A valid legacy state may therefore gain only the persistent coordination lock even if
the requested run ID is unknown. A missing, non-directory, or symlinked state leaf is
denied without creating state, a database, an artifact root, or a lock. Any existing
real state directory with a usable lock path—including empty storage, a corrupt
database, an unknown run, or a valid ACTIVE fence—may gain and synchronize the empty
coordination inode before the later audit, classification, or denial.

Shared mode coordinates these mutators with exclusive maintenance; it does not make
recovery quiescent against another shared publisher. Guarded terminalization may audit
under shared mode only because it never acts on ownerless findings and revalidates the
complete ledger and recursively reachable CAS graph under SQLite's writer lock before
and after its narrow mutation. Any operation using `quarantine_allowed` as authority
must instead hold one exclusive lease across a fresh audit, candidate revalidation,
every move and receipt, reconciliation, and completion.

Direct `EventStore` and `FileArtifactStore` use remains a trusted low-level boundary.
Those primitives do not silently acquire this lease; callers that bypass the supported
runtime are responsible for coordinating with maintenance.

## Future quarantine fence

The canonical future fence is `quarantine/v1/ACTIVE`. This checkpoint only inspects it
and never creates the quarantine namespace. After a shared kernel lock is acquired:

- an absent namespace permits shared mutation;
- any single-link regular `ACTIVE` leaf blocks shared mutation without parsing its
  future canonical JSON content;
- an ancestor link/non-directory or a linked/non-regular/replaced marker is an integrity
  denial; and
- exclusive maintenance may acquire the lease while a valid marker exists so a future
  implementation can resume an interrupted operation.

Checking after kernel acquisition closes the cooperative race: a conforming exclusive
maintainer cannot create or remove the fence while a shared publisher holds the lock,
and a publisher cannot pass the fence while exclusive maintenance owns it.

## Adversarial matrix

`tests/integration/test_maintenance_lease_process.py` uses spawned and direct-forked
processes with pipe-synchronized acquisition acknowledgements. It uses no timing sleeps.

| Case | Observation |
|---|---|
| shared/shared | Two spawned processes simultaneously hold shared mode; exclusive mode is denied until both release |
| exclusive exclusion | A spawned exclusive holder causes both shared and exclusive nonblocking attempts to return typed busy denials |
| `SIGKILL` | The parent kills a spawned exclusive holder only after acquisition; the kernel releases the lease, and the next exclusive holder reuses the same persistent single-link inode |
| fork inheritance | At-fork hooks serialize registry handoff, discard the child's inherited registry, and close inherited descriptors without unlocking the parent's lease |
| abrupt parent exit | A child forked by the holder remains alive after the holder exits abruptly but cannot retain the holder's lease |
| case-folded state alias | On Darwin's default case-insensitive filesystem, `state` and `STATE` still coordinate through the same kernel inode |

A later complete-suite run exposed a harness race in the abrupt-parent case: the outer
process could probe the lock after the holder exited but before the fork child's
registered post-fork cleanup had finished. The case now waits on an explicit child-ready
pipe written only after that cleanup boundary, without adding a timing sleep. The exact
case then passed 20 consecutive focused repetitions. The lease implementation did not
change; the synchronization makes the observation match the boundary the test claims.

Unit cases additionally cover nested same-process reference counting; failed registry
handoff rollback; finalizer cleanup; released-lease and same-object double-entry
rejection; cleanup when first-entry path/fence validation fails; both-descriptor cleanup
after an injected close failure; file and parent-directory sync calls;
filesystem-root rejection; state/lock links, hard links, and replacement; ACTIVE
blocking; and malformed fence paths. Runtime cases observe exclusive exclusion inside
both model and evaluator work, prove ACTIVE stops a runner before CAS/SQLite creation,
prove exclusive maintenance prevents recovery and budget terminalization, and prove
ACTIVE prevents recovery without changing the ledger. API and CLI cases distinguish a
lease-release failure after durable runner/recovery success from a pre-commit denial and
return the already-committed result in stable versioned JSON.

## Focused verification

```bash
uv run ruff format --check \
  src/guildmind/storage/maintenance.py \
  src/guildmind/runtime/runner.py \
  src/guildmind/runtime/recovery.py \
  tests/unit/test_maintenance.py \
  tests/integration/test_maintenance_lease_process.py \
  tests/integration/test_runtime_maintenance_lease.py
uv run ruff check \
  src/guildmind/storage/maintenance.py \
  src/guildmind/runtime/runner.py \
  src/guildmind/runtime/recovery.py \
  tests/unit/test_maintenance.py \
  tests/integration/test_maintenance_lease_process.py \
  tests/integration/test_runtime_maintenance_lease.py
uv run mypy \
  src/guildmind/storage/maintenance.py \
  src/guildmind/storage/__init__.py \
  src/guildmind/runtime/runner.py \
  src/guildmind/runtime/recovery.py \
  src/guildmind/storage/integrity.py \
  src/guildmind/cli.py
uv run pytest -q \
  tests/unit/test_maintenance.py \
  tests/integration/test_maintenance_lease_process.py \
  tests/integration/test_runtime_maintenance_lease.py
```

Result on the Darwin development host:

```text
39 passed in 0.79s
```

The broader maintenance/recovery/CLI selection, including both post-commit JSON
regressions, reported:

```text
93 passed in 1.43s
```

The complete repository gate after this integration reported:

```text
ruff format --check: 95 files already formatted
ruff check: all checks passed
mypy: 66 source files, no issues
pytest: 548 passed, 28 skipped in 7.34s
```

## Evidence boundary and remaining work

This is a cooperative local protocol, not a security boundary against an actively
hostile process with the same OS identity. Such a process can ignore `flock`, unlink or
replace checked pathnames after an identity check, or mutate low-level storage directly.
The descriptor-relative lock and marker traversal rejects the deterministic link and
replacement cases exercised here but does not make the whole filesystem namespace
hostile-race-proof. The authoritative database must remain on a local filesystem, and
all supported publishers and maintainers must follow this protocol.

`SIGKILL` proves that the kernel drops a process-owned lease on process exit. It is not
a sudden-power-loss or media-durability test. File and directory `fsync` ordering is
exercised by code and unit tests; a storage-fault or reboot campaign remains a separate
reference-host requirement.

Most importantly, no orphan is moved by this checkpoint. The durable ACTIVE record,
quarantine plan/receipts/completion records, no-replace moves, restart reconciliation,
and quarantine kill matrix remain to be implemented. The complete CAS publication kill
matrix, concurrent/open-process stress, rootless x86_64 repetition, real-provider
behavior, and the 99% normal-fixture campaign also remain open.

## 2026-08-03 quarantine integration follow-up

The subsequent [resumable quarantine checkpoint](../2026-08-03-resumable-quarantine/README.md)
now consumes this lease in exclusive mode. `MaintenanceLease.verified_state_descriptor()`
lends a revalidated duplicate of the already-open state directory for descriptor-relative
maintenance while preventing lease release until that borrow ends. The duplicate is
tracked process-wide so the at-fork child reset closes it; context exit verifies that the
number still names the state directory before closing it, so caller misuse cannot close
an unrelated file that reused the same descriptor number. Shared-mode borrows repeat the
ACTIVE-fence check, while quarantine explicitly requires exclusive mode.

Adversarial testing of that bridge reproduced and closed four false-safe implementation
cases before quarantine integration:

- an oversized Python descriptor could truncate through `ctypes.c_int` and alias a real
  descriptor;
- a borrowed duplicate could survive a direct `fork()` and prolong access in the child;
- case-folded `QUARANTINE`, `V1`, or `active` aliases could pass a pathname-only marker
  check on a case-insensitive filesystem; and
- closing and reusing the yielded descriptor number could make context cleanup close an
  unrelated file or hide the caller's primary exception.

Descriptor integers are now bounded to the C ABI range, borrowed descriptors participate
in at-fork cleanup, every fence component is found by an exact descriptor-relative
bounded scan, and release preserves a primary body exception while reporting borrow
misuse. These are local cooperative controls. The quarantine process-kill/concurrency
matrix, hostile same-UID races, power loss, and reference-host validation remain open.
