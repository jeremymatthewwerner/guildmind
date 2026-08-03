# Guarded Existing-Only Recovery and Inspection — 2026-08-03

**Evidence level:** host-independent development tests<br>
**Scope:** guarded fixture terminalization plus read-only `replay`/`report` for existing
local fixture state<br>
**Stage 1 effect:** recovery gating is implemented for this path; the gate remains
**NOT PASSED**

## Claim established

`recover_existing_fixture_run()` and `guildmind recover` no longer treat an ordinary
create-or-open handle or a caller-supplied audit report as authority. Each attempt:

1. captures identities for the configured parent, state directory, database, artifact
   root, and SQLite sidecars; state/database/artifact links are rejected while explicit
   trusted-base handling preserves ordinary operating-system path aliases;
2. performs a fresh existing-only all-run SQLite and recursive CAS audit;
3. requires verified references and confirms that the requested run belongs to the
   audited ledger snapshot before opening a writer;
4. opens the database with SQLite `mode=rw` without creating or migrating storage;
5. recomputes the complete ledger commitment inside `BEGIN IMMEDIATE`, re-audits all
   reachable CAS bytes against those writer-locked roots, and checks the configured path
   identities around that audit;
6. stages conservative recovery only after those checks, validates the complete ledger,
   captures the recovered manifest and event stream inside the same transaction, and
   invokes the recursive-CAS/path guard a second time against the post-mutation roots at
   the final pre-commit boundary; and
7. returns that replay-valid terminal stream only after the final guard and commit
   succeed. Repeating recovery on the terminal run is an exact no-op.

No failed precondition commits a recovery mutation; a failure inside the writer window
rolls that SQLite transaction back. Missing or corrupt referenced bytes, an unknown run,
a changed all-run ledger, altered recursive bytes, or deterministic replacement of the
trusted parent/state/database/artifact path is denied before a recovery terminal is
committed. Existing unreferenced finalized blobs are not adopted, deleted, or moved.
Recovery does not construct a model, evaluator, or new runner and does not redispatch
external work; an already-started model request remains conservatively `ambiguous` and
consumes its full outstanding reservation.

The recovery boundary also normalizes SQLite open, lock, and integrity failures to the
stable `storage_changed` denial instead of leaking driver-specific exceptions. If a
`FixtureRunner` exception occurs after run creation, the runner closes its ordinary
writer and invokes this same guarded existing-only path with terminal reason
`runner_exception`. Intact evidence is terminalized; corrupt referenced evidence is left
nonterminal, and the denial is attached to the original exception.

Pre-dispatch budget refusal uses the same fresh audit, writer-locked snapshot check,
two-pass recursive-CAS/path guard, transactionally captured result, and stable denial
mapping before committing `budget_exhausted`. If a budget error occurs after dispatch has
started, the runner instead uses general recovery so an outstanding request is classified
and charged conservatively. The normal success path also captures evaluation completion's
terminal manifest/events inside its SQLite transaction rather than issuing a public
post-commit read that could fail after durable success.

The `replay` and `report` CLI paths now use an existing-only read-only database handle.
They reject a missing state directory/database, filesystem-root state, configured links,
an invalid Guildmind database, and detected path replacement with a stable
`guildmind.inspection-denial/v1` response. They hold a verified all-run SQLite snapshot
while reading the requested run and do not initialize storage. This inspection checkpoint
validates the ledger; it does not claim a recursive CAS audit for `replay` or `report`.

## Focused verification

The expanded focused command exercised guarded recovery and budget refusal, transactional
terminal-result capture, runner exception handling, the CLI boundary, existing-only
inspection, and the two real-process external-work kill cases:

```bash
uv run pytest -q \
  tests/integration/test_guarded_recovery.py \
  tests/integration/test_fixture_runner.py \
  tests/integration/test_fixture_runner_process_crash.py \
  tests/unit/test_event_store.py \
  tests/unit/test_cli.py \
  -k 'guarded_recovery or guarded_budget_refusal or recover_command or read_only_inspection or fixture_runner_recovers_real_process_crashes or runner_exception_recovery or budget_refusal_guard or late_budget_error or complete_evaluation_with_events or complete_budget_exhaustion_with_events'
```

Result on 2026-08-03:

```text
51 passed, 131 deselected in 1.09s
```

The 51 cases comprise 20 guarded recovery/budget-terminalization integration cases, 4
runner exception/budget cases, 2 `SIGKILL` cases at model- and evaluator-in-flight
boundaries, 6 EventStore transaction/guard cases, and 19 recovery/inspection CLI cases.
They cover successful idempotent recovery with an untouched orphan; absent/empty/root
storage; missing/corrupt referenced bytes; invalid terminal reasons; an unknown run; a
ledger commit after initial audit; event capture without post-commit observation;
normalized SQLite writer failure; recursive-byte mutation before staging and during
recovery or budget terminalization, where the second guard rolls the transaction back;
transactional evaluation completion; guarded runner exceptions and budget refusal;
conservative late-budget recovery; equal-content path replacement; stable CLI denials;
existing-only `replay`/`report`; and no-create behavior.

The final full repository test run reported:

```text
487 passed, 28 skipped
```

These are focused and repository-wide development results, not the predeclared
normal-fixture reliability campaign.

## Quiescence and same-UID boundary

Authoritative use still requires a quiescent exclusive-writer maintenance window.
`BEGIN IMMEDIATE` excludes cooperating SQLite writers, and identity/hard-link checks
close the deterministic replacement cases tested here. They do not make separate
pathname observation, open, CAS traversal, and precommit checks one atomic operating-
system operation. A concurrently hostile process running as the same OS user can mutate
or swap files between individual checks and may race this protocol. Guildmind therefore
does not claim protection from an actively racing same-UID process or a hostile local
co-tenant. Descriptor-relative traversal or an equivalent maintenance lock remains the
stronger future boundary.

## Remaining Stage 1 gates

- implement resumable, crash-safe quarantine; this checkpoint deliberately preserves
  all unreferenced blobs;
- inject kills throughout CAS temporary write, file `fsync`, no-replace publication,
  directory `fsync`, and quarantine moves;
- stress cooperative concurrent writers, open-process recovery, and CAS publication
  races;
- exercise real-provider idempotency, status polling, SDK retry suppression,
  duplicate-execution treatment, and invoice/usage reconciliation;
- repeat the complete sandbox, evaluator, resource, containment, and recovery matrix on
  the dedicated rootless x86_64 reference host; and
- run the predeclared normal-fixture campaign with at least 99% free of infrastructure
  error.

The recovery command is explicit; Guildmind does not yet sweep and recover abandoned
runs automatically at process startup. Nothing in this checkpoint authorizes external
repositories, arbitrary model-generated commands, or provider-backed campaigns.
