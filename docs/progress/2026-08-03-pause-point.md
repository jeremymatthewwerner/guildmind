# Guildmind Pause-Point Handoff — 2026-08-03

**Natural pause:** after the local Stage 1 storage, recovery, quarantine, and
same-digest CAS contention checkpoints, plus a statically verified reliability-campaign
evidence scaffold<br>
**Functional checkpoint:** `c09e098` (`Stress concurrent CAS publishers`) on `main`<br>
**Saved scaffold checkpoint:** `b668456` (`Scaffold reliability campaign evidence
contract`) on `main`<br>
**Repository gate:** 634 passed, 29 declared skips; 110 files formatted; Ruff and strict
mypy clean across 75 source files<br>
**Overall verdict:** useful local development substrate; authoritative Stage 1 gate
still **NOT PASSED**

## Executive summary

Guildmind began as a research brief about improving collective AI capability by
changing the institution around fixed models rather than changing their weights. It is
now a tested local measurement substrate: one repository-owned coding fixture can run
through a scripted fake model, constrained patch application, evaluation, immutable
artifact storage, a transactional event ledger, replay, reporting, guarded recovery,
integrity audit, and explicit resumable quarantine.

The repository also contains a two-phase container evaluator and active resource and
containment probes. Those have strong development evidence on Docker Desktop, but they
have not passed the required rootless x86_64 Linux reference-host gate. External or
hostile repositories, arbitrary model-generated commands, paid model-provider pilots,
institution search, and later research stages therefore remain intentionally blocked.

This is a clean stopping point. The storage-contention checkpoint is implemented,
documented, independently reviewed, committed, and pushed. A first campaign evidence
contract and internal runtime scaffold are also saved and statically clean, but they are
not CLI-wired or directly tested and have not executed a campaign. They are explicitly a
resume point, not new reliability evidence.

## What has been built

### 1. Research and experiment contract

- The [starting brief](../starting-brief.md) states the central hypothesis, intended
  comparisons, research principles, risks, and negative-result policy.
- The [staged build plan](../build-plan.md) turns that brief into gated architecture,
  benchmark, runtime, search, statistical, and cost work.
- [Experiment 0001](../experiments/0001-institutional-search.md) defines the draft first
  institutional-search protocol, equal-budget comparisons, lockbox rules, and unresolved
  owner decisions.
- The [threat model](../threat-model.md), architecture decisions, and
  [plan audit](../reviews/2026-07-31-plan-audit.md) preserve the security, evidence, and
  review reasoning rather than leaving it only in chat history.
- The [hybrid evolution/RL design](../hybrid-evolution-rl.md) records the much later
  constraint-versus-policy boundary without prematurely building it.
- PettingZoo remains an optional future environment adapter, not Guildmind's scheduler.
  Karpathy's `autoresearch` was considered but not adopted: its tight single-program
  optimization loop does not currently justify a dependency or architectural role in
  Guildmind's institution-level, equal-budget, evidence-heavy research design.
- The README now carries the selected Guildmind mark and a substantially expanded project
  explanation.

### 2. Deterministic local vertical slice

The current local path is:

```text
fixture task → scripted fake model → constrained patch → copied workspace
             → trusted local evaluator
             → immutable CAS artifacts + transactional SQLite event ledger
             → verified replay / report / guarded recovery / quarantine
```

Implemented capabilities include:

- strict immutable Pydantic domain records and exported JSON Schemas;
- aggregate model-budget reservation, refusal, and reconciliation;
- content-addressed artifacts and hash-linked, single-writer event history;
- a deterministic clock and normalized semantic replay digest;
- one scripted model request per current fixture run;
- explicit artifact absence and conservative ambiguous-request treatment after failure;
- terminal run reconstruction from committed events; and
- existing-only replay and reporting that do not silently initialize missing state.

The core implementation is in the
[fixture runner](../../src/guildmind/runtime/runner.py),
[event store](../../src/guildmind/storage/events.py), and
[artifact store](../../src/guildmind/storage/artifacts.py).

### 3. Evaluator and sandbox hardening

The first fixture is a bounded `python-call-v1` micro-fixture with five known cases. Its
container evaluator separates untrusted candidate invocation from trusted scoring:

- the candidate receives the patched workspace and expected-value-free challenge;
- a separate scorer receives the bounded candidate response and sealed oracle;
- the scorer never imports the candidate workspace; and
- exact bounded candidate/scorer transcripts survive as content-addressed evidence.

The checked-in [adversarial corpus](../adversarial-corpus.md) contains 19 predeclared
patch-intake, functional, boundary-integrity, timeout, output-exhaustion, and OOM cases.
Development runs matched all declared outcomes. Active Docker probes also exercised
memory, PID, writable-byte, planted-secret, mount/environment, credential, route,
DNS/TCP, Unix-socket, privilege, source-integrity, and cleanup boundaries.

Durable evidence is stored under:

- [patch-intake evidence](../evidence/patch-intake/2026-08-02-development/README.md);
- [resource-probe evidence](../evidence/resource-probes/2026-08-02-docker-desktop/README.md);
  and
- [containment-probe evidence](../evidence/containment-probes/2026-08-02-docker-desktop/README.md).

This remains development evidence. Docker Desktop on the current Apple Silicon Mac is a
rootful ARM Linux VM, not the clean native rootless x86_64 Linux environment required by
the [Stage 1 gate](../reviews/2026-08-01-stage-1-hardening-gate.md).

### 4. Crash recovery and evidence integrity

The repository now has synchronized real-process evidence across the major local
persistence boundaries:

- eleven `SIGKILL` cases cover five committed lifecycle prefixes, four selected
  pre-commit rollback points, and model/evaluator in-flight work;
- a bounded recursive audit verifies every reachable artifact byte and typed relationship
  while classifying ownerless, temporary, malformed, linked, and corrupt entries;
- guarded recovery takes a fresh audit, rechecks the complete ledger and reachable CAS
  under the SQLite writer lock, and repeats the guard at the final pre-commit boundary;
- replay, report, and recovery open existing state without creating absent evidence; and
- SQLite failures and integrity drift produce stable denials rather than optimistic
  repair.

See the [process-crash matrix](../evidence/crash-recovery/2026-08-02-process-sigkill/README.md),
[recursive audit](../evidence/storage-integrity/2026-08-02-recursive-audit/README.md),
and [guarded-recovery evidence](../evidence/crash-recovery/2026-08-03-guarded-recovery/README.md).

### 5. Cooperative maintenance and resumable quarantine

Supported runtime mutation now uses a persistent state-local shared/exclusive maintenance
lease. Normal publication and guarded recovery hold shared mode; explicit state-wide
maintenance holds exclusive mode. A durable `quarantine/v1/ACTIVE` marker blocks
supported mutation while a quarantine transaction is active.

Explicit quarantine:

- authorizes only a fresh, complete allowlist of ownerless regular-file findings;
- writes immutable BEFORE/PLAN/ACTIVE evidence before moving bytes;
- uses descriptor-relative, same-filesystem, no-replace moves;
- writes deterministic receipts and AFTER/COMPLETE commitments;
- repairs the ambiguous post-rename/pre-receipt window on restart; and
- fails closed if both or neither planned source/destination exists.

Six cooperating-process cases and all 16 predeclared quarantine `SIGKILL` prefixes pass,
including fresh-process completion and an identity-preserving second no-op. See the
[maintenance lease](../evidence/storage-integrity/2026-08-03-maintenance-lease/README.md),
[resumable quarantine](../evidence/storage-integrity/2026-08-03-resumable-quarantine/README.md),
and [quarantine process matrix](../evidence/storage-integrity/2026-08-03-quarantine-process-crash/README.md).

### 6. Atomic CAS publication and process contention

CAS publication now uses fail-closed platform-native no-replace rename:

- Darwin `renamex_np(..., RENAME_EXCL)`;
- Linux `renameat2(..., RENAME_NOREPLACE)`; and
- no overwrite-capable fallback.

Nine synchronized Darwin process-kill cases cover directory creation, temporary creation,
partial write, the full write before file `fsync`, pre-rename, post-rename, and final
directory-sync boundaries. Retries preserve exact stranded-temporary evidence and one
stable canonical inode.

The latest checkpoint adds eight persistent spawned publishers over 20 unique
digest/shard rounds: 160 contested low-level puts. Each round proves eight verified
temporaries, exactly one real syscall winner, seven unchanged losers before cleanup, a
canonical file retaining the winning inode, eight identical returned references, no
residual temporary, and an exact final audit containing 20
`valid_finalized_orphan` findings. Ten additional consecutive runs passed, and the full
46-case CAS selection passed.

See the [atomic-publication evidence](../evidence/crash-recovery/2026-08-03-atomic-cas-publication/README.md),
[temporary-write matrix](../evidence/crash-recovery/2026-08-03-cas-temporary-write/README.md),
and [publisher-contention matrix](../evidence/crash-recovery/2026-08-03-cas-publisher-contention/README.md).

### 7. Reliability-campaign evidence scaffold

The pause-point scaffold now preserves the main contract decisions in code instead of
only in planning notes:

- strict immutable manifest, attempt, terminal-evidence, and hash-bound report models;
- a complete round-major schedule with unique fixture and attempt identities, zero
  retries, exact seeds, budgets, evaluator/environment identity, and code identity;
- derived completeness, expected-result, infrastructure-error, threshold, and campaign
  verdict fields that cannot be independently asserted in a report;
- two newly exported public JSON Schemas for the campaign manifest and report;
- an internal strict JSON/fixture loader with content-bound code, fixture-tree, task, and
  gold-patch checks; and
- an internal development-only executor/reconciler draft that isolates attempt state,
  audits ledger/CAS evidence, verifies replay and terminal identity, and publishes
  canonical evidence with no-replace semantics.

The scaffold is in
[the campaign domain module](../../src/guildmind/domain/campaign.py) and
[the internal campaign runtime](../../src/guildmind/runtime/campaign.py). The full
repository gate passes, and the two identity hash functions were smoke-invoked locally.
There is deliberately no sample campaign manifest, CLI command, targeted campaign test
suite, or recorded campaign result yet. Until those exist, this code must not be described
as a working campaign runner or as evidence for the 99% gate.

## What works now

### Local prerequisites and tests

No hosted runtime or deployment service is needed. The implemented path uses the local
Mac, Git, Python 3.12, and `uv`. The scripted model makes no paid API calls.

```bash
uv sync
make check
uv run guildmind doctor
```

The last full gate reported:

```text
ruff format --check: 110 files already formatted
ruff check: all checks passed
mypy: 75 source files, no issues found
pytest: 634 passed, 29 skipped in 14.28s
```

The 29 skips are declared Docker-image/reference-host and two local-filesystem edge cases;
they are not silent failures inside the new CAS contention test.

### End-to-end local run

This runs the repository-owned fixture with its scripted solution patch, writes the CAS
and ledger, and then verifies the stored result:

```bash
uv run guildmind run fixtures/001-python-addition \
  --state-dir .guildmind \
  --run-id demo-run

uv run guildmind replay demo-run --state-dir .guildmind
uv run guildmind report demo-run --state-dir .guildmind
```

A fresh end-to-end check at this pause point succeeded with status `succeeded`, a valid
14-event ledger, replay/report agreement, and zero provider cost.

If a process dies after creating a run, explicit recovery can close the existing attempt
without redispatching model or evaluator work:

```bash
uv run guildmind recover interrupted-run --state-dir .guildmind
```

Freshly authorized ownerless CAS files can be preserved outside the live store with:

```bash
uv run guildmind quarantine --state-dir .guildmind
```

Public schemas and the current semantic determinism check also work:

```bash
make schemas
make determinism
```

The schema export now includes `ReliabilityCampaignManifest` and
`ReliabilityCampaignReport`. That means consumers can validate the frozen data shapes;
it does not mean a campaign can yet be launched from the CLI.

Docker is optional for the container evaluator and active probes. No cloud runtime,
Kubernetes cluster, hosted database, queue, or managed agent service is needed now.
Depending on the user's organization and use, Docker Desktop itself may require a paid
subscription under [Docker's license terms](https://docs.docker.com/subscription/desktop-license/);
that is a Docker licensing question, not a Guildmind deployment dependency.

## What does not work yet

- The repository has one fixture, one gold patch, and five known cases—not the planned
  20-fixture reliability corpus.
- The reliability-campaign data contract and an internal runtime scaffold now exist, but
  there is no checked-in campaign manifest, CLI integration, targeted unit/integration
  suite, executed smoke campaign, or durable campaign evidence artifact. The internal
  executor has not been accepted as operational.
- The 100-run determinism command is not a fixture-reliability campaign and must not be
  relabeled as one.
- The rootless x86_64 Linux reference-host gate has not run. `guildmind doctor` correctly
  reports the local fixture path ready and the production sandbox not ready on this Mac.
- The low-level CAS contention test is cooperative local filesystem evidence. It is not
  runtime-level contention, hostile same-UID protection, power-loss durability, network
  filesystem evidence, fairness, performance, or scalability evidence.
- Real provider idempotency, polling, duplicate suppression, retry disabling, usage/bill
  reconciliation, and frozen model identity are not implemented.
- General worker dispatch, the organizational-genome compiler, multi-agent scheduling,
  institution search, persistent institutional memory, hybrid RL controllers, judge
  societies, and PettingZoo adapters have not been built.
- External repositories, arbitrary model-generated commands, provider-backed pilots,
  baseline campaigns, institutional search, and Stage 2 work are not authorized by the
  current evidence.
- Experiment 0001 still needs owner choices for the worker model, spend ceilings,
  minimum relevant effect (`δ`), and evidence/publication level.

## Exact next step

Finish and validate the saved campaign scaffold before adding more fixtures or making a
99% claim:

1. Add focused model/loader tests for unknown or duplicate fields, duplicate fixture
   paths and IDs, invalid or incomplete schedules, unsafe paths, changed code/fixture
   bytes, retry attempts, and reference-tier/local-evaluator mismatches.
2. Exercise and review the internal executor/reconciler, including ordinary execution
   exceptions, budget refusal, missing/duplicate/nonterminal runs, replay corruption,
   identity mismatch, and write-once publication.
3. Add a checked-in one-fixture development manifest whose code, fixture tree, gold
   patch, evaluator, budget, seed, and single attempt are content-bound.
4. Expose a narrow `guildmind campaign run` command that checks output availability
   before dispatch and produces one canonical report, with a nonzero exit for a valid
   failed gate and no hidden retry.
5. Run and preserve the one-attempt smoke with targeted tests and a full repository gate.
   Document explicitly that it is harness evidence, not the 100-attempt campaign and not
   proof of an underlying 99% reliability rate.

After that checkpoint:

1. add 19 distinct known-outcome micro-fixtures in reviewed batches;
2. prove each pristine fixture fails and its gold patch passes locally and in the
   two-phase container evaluator;
3. freeze a 20-fixture × 5-round schedule (100 declared attempts) for an empirical
   one-percentage-point infrastructure-error denominator;
4. run that exact manifest in development without changing its denominator or retry rule;
5. rerun the exact campaign and all containment/recovery matrices on the dedicated
   rootless x86_64 reference host; and
6. preserve the resulting canonical evidence and update the Stage 1 verdict.

A 100-attempt result is an empirical gate, not proof that an underlying population
reliability exceeds 99%. Any stronger statistical claim needs its own preregistered
confidence rule and larger sample.

## Expected future costs

There is no reason to buy deployment infrastructure at this pause point. Likely later
costs, only when their gates are ready, are:

- model API usage for provider-backed reliability and baseline campaigns;
- a short-lived clean rootless x86_64 Linux machine, owned or rented, for authoritative
  sandbox/recovery evidence; and
- eventually, measured storage or parallel compute if local SQLite/CAS throughput becomes
  an actual constraint.

PostgreSQL, object storage, distributed queues, Kubernetes, and always-on hosted workers
remain deliberate non-requirements until measurements justify them.

All Codex subagents were stopped at this pause point. No Guildmind process, cloud worker,
paid model call, or hosted service was left running.

## Resume checklist

```bash
git pull --ff-only
uv sync
make check
uv run guildmind doctor
```

Then continue only the campaign test/CLI/one-fixture smoke checkpoint described above.
Do not start provider-backed experiments, generate 19 fixtures before the scaffold is
validated end to end, or rent a reference host before the attempt/evidence format is
frozen by an accepted smoke.
