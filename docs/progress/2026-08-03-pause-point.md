# Guildmind Pause-Point Handoff — 2026-08-03

**Natural pause:** after the local Stage 1 storage, recovery, quarantine, same-digest CAS
contention, campaign harness, four development-container-qualified fixture-breadth
batches, and a fifth batch through trusted-local qualification; fixtures 001–017 also
have cumulative local campaign evidence<br>
**Checkpoint:** the commit containing this handoff on `main`; the Batch 004 container
report is bound to clean tested revision
`7c9eebaf293ea088db07fdaa9daf8441c21a0b00`, while the Batch 004 cumulative campaign
report is bound to clean tested revision
`a856db8f152e2c37426bde6301a96b34c805e1aa`<br>
**Repository gates:** current default 701 passed / 45 declared skips; 204 files
formatted; Ruff clean and strict mypy clean across 83 source files. The latest accepted
complete dual-image gate before Batch 005 is 730 passed / 13 declared skips, using the
preserved Batch 001–003 image and the rebuilt Batch 004 image. Batch 005 container
qualification has not run<br>
**Overall verdict:** useful local development substrate; authoritative Stage 1 gate
still **NOT PASSED**

## Executive summary

Guildmind began as a research brief about improving collective AI capability by
changing the institution around fixed models rather than changing their weights. It is
now a tested local measurement substrate: repository-owned coding fixtures can run
through a scripted fake model, constrained patch application, evaluation, immutable
artifact storage, a transactional event ledger, replay, reporting, guarded recovery,
integrity audit, and explicit resumable quarantine. Twenty repository-owned fixtures
now exist. Fixture 001 anchors that complete campaign path. Fixtures 002–013 add twelve
distinct semantic families in three reviewed batches; all three batches pass
three-repeat trusted-local and two-phase development-container pristine/gold gates. A
separately frozen local campaign exercised and reconciled fixtures 001–005 as expected
with complete clean evidence. A separately frozen cumulative campaign then exercised and
reconciled all fixtures 001–009 as expected. A third frozen cumulative campaign has now
exercised and reconciled all fixtures 001–013 as expected.

Fixtures 014–017 add four more frozen families and pass both their three-repeat
trusted-local and their three-repeat, two-phase development-container pristine/gold
gates. The reproducibly rebuilt image closes the earlier unit-only boundary for finite
JSON numbers: the nonintegral backoff fixture passed all six sealed cases in a disjoint
candidate/scorer container flow. The separately frozen 17-fixture campaign subsequently
completed 17/17 attempts as
expected with 238 replay-valid events, zero retries, zero recovery, zero infrastructure
errors, and zero provider cost. Fresh reconciliation reproduced its complete report and
canonical bytes exactly.

Fixtures 018–020 complete the frozen family matrix with SemVer record selection,
recursive shape-preserving key redaction, and recursive Boolean-rule evaluation. Their
three pristine controls produced stable expected failures and their three gold patches
produced stable passes in three trusted-local repetitions apiece: 18 local evaluations
plus three direct visible-failure checks. Their development-container and campaign gates
remain pending.

The repository also contains a two-phase container evaluator and active resource and
containment probes. Those have strong development evidence on Docker Desktop, but they
have not passed the required rootless x86_64 Linux reference-host gate. External or
hostile repositories, arbitrary model-generated commands, paid model-provider pilots,
institution search, and later research stages therefore remain intentionally blocked.

This is a clean stopping point. The reliability-campaign contract is executable through
a narrow development-only CLI, directly tested across success and negative evidence
paths, and exercised by five immutable evidence manifests. The one-fixture smoke
accepted the harness; Batch 001 adds 24 stable local results, 24 stable container results,
and a 5/5 expected local campaign. Batch 002 adds another 24 stable local and 24 stable
container results, followed by a 9/9 expected cumulative local campaign with 126 events,
zero retries, and zero infrastructure errors. Batch 003 adds another 24 stable local and
24 stable container results, followed by a 13/13 expected cumulative local campaign with
182 events, zero retries, and zero infrastructure errors. Batch 004 adds another 24
stable trusted-local results and 24 stable development-container results with 48 cleaned
containers, followed by a 17/17 expected cumulative local campaign with 238 events,
zero retries, and zero infrastructure errors. Batch 005 adds 18 stable trusted-local
results across the final three fixtures; its container and cumulative campaign gates,
the final 20-fixture × 5-round campaign, and reference-host repetition remain undone.
None of this changes the **NOT PASSED** Stage 1 verdict.

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

The CAS contention checkpoint adds eight persistent spawned publishers over 20 unique
digest/shard rounds: 160 contested low-level puts. Each round proves eight verified
temporaries, exactly one real syscall winner, seven unchanged losers before cleanup, a
canonical file retaining the winning inode, eight identical returned references, no
residual temporary, and an exact final audit containing 20
`valid_finalized_orphan` findings. Ten additional consecutive runs passed, and the full
46-case CAS selection passed.

See the [atomic-publication evidence](../evidence/crash-recovery/2026-08-03-atomic-cas-publication/README.md),
[temporary-write matrix](../evidence/crash-recovery/2026-08-03-cas-temporary-write/README.md),
and [publisher-contention matrix](../evidence/crash-recovery/2026-08-03-cas-publisher-contention/README.md).

### 7. Reliability-campaign harness and one-fixture smoke

The campaign layer now preserves and executes the main contract decisions:

- strict immutable manifest, attempt, terminal-evidence, and hash-bound report models;
- a complete round-major schedule with unique fixture and attempt identities, zero
  retries, exact seeds, budgets, evaluator/environment identity, and code identity;
- derived completeness, expected-result, infrastructure-error, threshold, and campaign
  verdict fields that cannot be independently asserted in a report;
- two newly exported public JSON Schemas for the campaign manifest and report;
- an internal strict JSON/fixture loader with content-bound code, fixture-tree, task, and
  gold-patch checks; and
- a development-only executor/reconciler that isolates attempt state, audits ledger/CAS
  evidence twice around readback, verifies replay and terminal identity, and publishes
  canonical evidence with no-replace semantics; and
- a narrow `guildmind campaign run` command with pre-dispatch output refusal, exit 0 for
  a passed harness, exit 2 for a valid failed gate, and exit 1 for configuration or
  evidence failure.

The scaffold is in
[the campaign domain module](../../src/guildmind/domain/campaign.py) and
[the campaign runtime](../../src/guildmind/runtime/campaign.py). The checked-in
[one-fixture manifest](../../campaigns/stage1-local-smoke-v1.json) binds the current code,
fixture tree, gold patch, evaluator/environment, fake model, budget, seed, single attempt,
and zero retries. Twenty-four focused tests cover malformed contracts, content drift,
report tampering, execution recovery, budget refusal, missing/nonterminal/undeclared and
corrupt evidence, and CLI publication semantics.

The first [development smoke](../evidence/reliability-campaigns/2026-08-03-one-fixture-smoke/README.md)
passed 1/1 intended attempt with an `expected` disposition, a 14-event replay-valid
terminal stream, clean verified storage, and zero provider cost. This is a working
one-fixture harness. It is deliberately not described as the 100-attempt normal-fixture
campaign or as evidence for a population-level 99% claim.

### 8. First fixture-breadth batch

The [normal-fixture reliability corpus](../fixture-reliability-corpus.md) now freezes 20
materially different task families before the final denominator is built. Four new
repository-owned `python-call-v1` fixtures implement the first breadth batch:

- Unicode-aware slug normalization;
- closed-integer interval sorting and coalescing;
- escaped JSON Pointer traversal across objects and arrays; and
- stable, first-wins deduplication by structural JSON identity.

Each fixture has one deliberately faulty pristine implementation, one visible test, five
evaluator-owned hidden tests, six sealed oracle cases, a one-file patch allowlist, and an
exact gold patch. The parameterized integration gate proves that its visible test and
semantically pristine control fail, its gold patch passes, its challenge excludes
expected values, its oracle retains them, and three consecutive local evaluations on
each side are byte-for-byte stable. That produced 24 trusted-local evaluations
across the new batch. No provider or hosted runtime was used.

The exact same controls and gold patches then ran three times apiece through the rebuilt
digest-pinned two-phase development image: 12 stable pristine failures, 12 stable gold
passes, all six cases scored per result, zero infrastructure errors, and 48 cleaned
containers. The self-bound
[container report](../evidence/fixture-qualification/2026-08-03-batch-001-development-container/README.md)
records the rootful ARM Docker Desktop boundary and cannot be promoted to reference
evidence.

Finally, the separately committed
[`stage1-local-batch-001-v1`](../../campaigns/stage1-local-batch-001-v1.json) manifest
ran fixtures 001–005 from a clean detached revision. Its
[canonical report](../evidence/reliability-campaigns/2026-08-03-batch-001-local-calibration/README.md)
contains 5/5 expected terminal/reconciled attempts, 70 replay-valid events, clean verified
storage, zero retries, and zero infrastructure errors. Five observations remain a
calibration, not the final 100-attempt denominator or a population-level 99% claim.

### 9. Second fixture-breadth batch

Fixtures 006–009 add another four materially distinct families:

- escaped run-length decoding with multi-digit counts and a defined malformed policy;
- exact integer Hamilton apportionment with stable ties and values beyond binary64;
- lexicographically minimal topological ordering with dynamic readiness and cycles; and
- ordered nested JSON set, delete, and list-insert operations against current state.

Every fixture has a visibly failing pristine implementation, a six-case sealed oracle,
an exact semantics-preserving pristine control, an exact one-file gold patch, and the
same bounded `python-call-v1` boundary. The trusted-local gate produced 12 stable
pristine failures and 12 stable gold passes. The exact same bytes then produced the same
classifications in three repetitions through the pinned two-phase development image:
24 more results, 48 cleaned candidate/scorer containers, and zero infrastructure errors.

The self-bound
[Batch 002 container report](../evidence/fixture-qualification/2026-08-03-batch-002-development-container/README.md)
records the exact source revision, image, fixture, patch, response, scorer completion,
and evaluation-binding hashes. Its static verifier passed, its opt-in live verifier
reproduced all four fixture cells, and the complete development-image repository gate
passed. This is rootful ARM Docker Desktop development evidence, not native rootless
x86_64 reference evidence.

The separately committed
[`stage1-local-batch-002-v1`](../../campaigns/stage1-local-batch-002-v1.json) manifest
then froze all fixtures 001–009 into one round with seeds 2001–2009 and zero retries. It
ran from its exact detached, tracked-clean revision before any report documentation was
added. All 9/9 attempts were terminal, expected, replay-valid, and storage-clean: 126
events, nine model calls, zero recovery, zero infrastructure errors, and zero provider
cost. A fresh reconciliation of the preserved raw state reproduced the complete report
model exactly. The
[canonical campaign evidence](../evidence/reliability-campaigns/2026-08-03-batch-002-local-calibration/README.md)
binds the manifest, code, fixture trees, gold patches, schedule, budgets, terminal
streams, and storage commitments. Nine attempts are calibration evidence, not the final
100-attempt denominator or a population-level reliability claim.

### 10. Third fixture-breadth batch

Fixtures 010–013 add four more materially distinct families:

- greedy word wrapping with exact-fit and oversized-word rules;
- half-open business-day counting across weekends, holidays, and leap dates;
- canonical Roman-numeral parsing with illegal-form rejection; and
- clockwise rotation of rectangular and one-dimensional JSON grids.

Every fixture has a visibly failing pristine implementation, six sealed cases, a
semantics-preserving pristine control, an exact one-file gold patch, and the same bounded
`python-call-v1` boundary. The trusted-local gate produced 12 stable pristine failures
and 12 stable gold passes. The exact same bytes then reproduced those classifications in
three repetitions through the pinned two-phase development image: 24 more results, 48
cleaned candidate/scorer containers, and zero infrastructure errors.

The self-bound
[Batch 003 container report](../evidence/fixture-qualification/2026-08-03-batch-003-development-container/README.md)
records the exact source revision, image, fixture, patch, response, scorer completion,
and evaluation-binding hashes. Its static verifier and focused live verifier passed.
This is rootful ARM Docker Desktop development evidence, not native rootless x86_64
reference evidence.

The separately committed
[`stage1-local-batch-003-v1`](../../campaigns/stage1-local-batch-003-v1.json) manifest
then froze fixtures 001–013 into one round with seeds 3001–3013 and zero retries. It ran
from its exact detached, tracked-clean revision before any report documentation was
added. All 13/13 attempts were terminal, expected, replay-valid, and storage-clean: 182
events, 13 model calls, zero recovery, zero infrastructure errors, and zero provider
cost. A fresh reconciliation of the preserved raw state reproduced the complete report
model exactly. The
[canonical campaign evidence](../evidence/reliability-campaigns/2026-08-03-batch-003-local-calibration/README.md)
binds the manifest, code, fixture trees, gold patches, schedule, budgets, terminal
streams, and storage commitments. Thirteen attempts are calibration evidence, not the
final 100-attempt denominator or a population-level 99% claim.

### 11. Fourth fixture-breadth batch — development-container checkpoint

Fixtures 014–017 add stable signed transaction aggregation, raw-segment route matching,
capped nonintegral backoff recurrence, and stable multiset inventory deltas. Each fixture
has one visible and five hidden tests, six sealed cases, a semantics-preserving pristine
control, and an exact one-file gold patch. The trusted-local gate produced 12 stable
pristine failures and 12 stable gold passes plus four direct visible-failure checks.

The nonintegral factor exposed an integer-only restriction in `python-call-v1`. The host
loader, candidate adapter, and trusted scorer now accept finite JSON floats and reject
nonfinite, overflowed, or non-JSON values. Focused tests cover canonical host bytes,
candidate return validation, trusted/hostile scorer validation, and exact finite-result
comparison. Because the evaluator image source changed, the previous digest is historical
evidence for Batches 001–003 only; it was not relabelled or overwritten.

Two consecutive `linux/amd64` builds from the tracked-clean fixture checkpoint produced
the same digest-pinned image. The exact Batch 004 pristine controls and gold patches then
ran three times apiece through the two-phase evaluator: 12 stable expected failures, 12
stable passes, all six cases scored in every result, 48 cleaned containers, and no
infrastructure errors. The self-bound
[Batch 004 container evidence](../evidence/fixture-qualification/2026-08-03-batch-004-development-container/README.md)
records the exact build, source, patch, response, completion, and evaluation identities.

The separately committed
[`stage1-local-batch-004-v1`](../../campaigns/stage1-local-batch-004-v1.json) manifest
then froze fixtures 001–017 into one round with seeds 4001–4017 and zero retries. It ran
from its exact detached, tracked-clean revision before any report documentation was
added. All 17/17 attempts were terminal, expected, replay-valid, and storage-clean: 238
events, 17 model calls, zero recovery, zero infrastructure errors, and zero provider
cost. Fresh read-only reconciliation reproduced the complete report model and canonical
bytes exactly. The
[canonical campaign evidence](../evidence/reliability-campaigns/2026-08-03-batch-004-local-calibration/README.md)
binds the manifest, code, fixture trees, gold patches, schedule, budgets, terminal
streams, and storage commitments. Seventeen attempts are calibration evidence, not the
final 100-attempt denominator or a population-level 99% claim.

### 12. Fifth fixture-breadth batch — local checkpoint

Fixtures 018–020 add full SemVer precedence with stable original-record selection,
recursive blocked-key removal through arbitrary JSON objects and arrays, and recursive
`fact`/`all`/`any`/`not` evaluation with explicit missing/falsy and empty-list semantics.
Each fixture has one visible and five hidden tests, six sealed cases, a
semantics-preserving pristine control, and an exact one-file gold patch.

The trusted-local gate produced nine stable pristine failures and nine stable gold
passes plus three direct visible-failure checks. Build metadata ties, numeric and textual
prerelease identifiers, nested objects under arrays, scalar roots/values, missing and
falsy facts, empty `all`/`any`, and nested operator order are all explicit sealed
discriminators. No evaluator program changed, but the exact new fixture bytes have not
yet run through the current digest-pinned image. The fifth batch is locally qualified
only; container and campaign claims remain pending.

## What works now

### Local prerequisites and tests

No hosted runtime or deployment service is needed. The implemented path uses the local
Mac, Git, Python 3.12, and `uv`. The scripted model makes no paid API calls.

```bash
uv sync
make check
uv run guildmind doctor
```

The final default and development-image gates at this pause point reported:

```text
ruff format --check: 204 files already formatted
ruff check: all checks passed
mypy: 83 source files, no issues found
pytest (current default): 701 passed, 45 skipped in 21.37s
pytest (latest accepted pre-Batch-005 dual-image gate): 730 passed, 13 skipped in 119.47s
```

With both development images configured, every development-container test ran. The 13
remaining skips are 11 reference-host-only cases and two APFS-invalid-name edge cases.
The larger default skip count is the same explicit opt-in boundary, not silent failure.

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
`ReliabilityCampaignReport`. The five accepted manifests are immutable evidence
contracts. Each remains parseable and independently verifiable, while dispatch requires
its exact bound code and fixture identities. To reproduce a campaign, first check out the
tested implementation revision recorded in its linked evidence README, then use new
state and report paths:

```bash
mkdir -p runs/reliability-campaigns
uv run guildmind campaign run campaigns/stage1-local-smoke-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-smoke-v1-state \
  --output runs/reliability-campaigns/stage1-local-smoke-v1-report.json

uv run guildmind campaign run campaigns/stage1-local-batch-001-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-batch-001-rerun-state \
  --output runs/reliability-campaigns/stage1-local-batch-001-rerun-report.json

uv run guildmind campaign run campaigns/stage1-local-batch-002-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-batch-002-rerun-state \
  --output runs/reliability-campaigns/stage1-local-batch-002-rerun-report.json

uv run guildmind campaign run campaigns/stage1-local-batch-003-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-batch-003-rerun-state \
  --output runs/reliability-campaigns/stage1-local-batch-003-rerun-report.json

uv run guildmind campaign run campaigns/stage1-local-batch-004-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-batch-004-rerun-state \
  --output runs/reliability-campaigns/stage1-local-batch-004-rerun-report.json
```

The scripted model has no paid provider dependency. Repeating a campaign requires its
exact recorded content identities plus new state and output paths; existing evidence is
never overwritten.

All five breadth batches can be rechecked locally without a model or campaign run:

```bash
uv run pytest -q tests/integration/test_fixture_reliability_corpus.py
uv run guildmind evaluate \
  fixtures/002-slug-normalization \
  fixtures/002-slug-normalization/solution.patch
```

The parameterized test covers fixtures 002–017 and all three pristine/gold repetitions;
the `evaluate` command is the direct single-fixture form.

With both digest-pinned development images present, the complete accepted two-phase
matrix can be repeated with:

```bash
export GUILDMIND_HISTORICAL_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'
export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e'
uv run pytest -q tests/integration/test_fixture_reliability_container.py
```

Docker is optional for the container evaluator and active probes. No cloud runtime,
Kubernetes cluster, hosted database, queue, or managed agent service is needed now.
Depending on the user's organization and use, Docker Desktop itself may require a paid
subscription under [Docker's license terms](https://docs.docker.com/subscription/desktop-license/);
that is a Docker licensing question, not a Guildmind deployment dependency.

## What does not work yet

- The repository has all 20 frozen fixtures through trusted-local qualification. Only
  fixtures 001–017 have development-container and cumulative local campaign
  qualification. None has passed the native rootless x86_64 reference host.
- The largest frozen campaign currently covers 17 fixtures, one round, the scripted
  patch model, and trusted local evaluation. It does not support the container evaluator,
  resume an interrupted aggregate campaign, or constitute the planned 100-attempt
  reliability denominator.
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

Commit the frozen fixtures 018–020 and their trusted-local gate before container
execution. Then run each exact pristine control and gold patch three times through the
current digest-pinned two-phase evaluator, preserving a new self-bound Batch 005 report
and the rootful ARM development-host boundary. The accepted Batch 001–004 reports and
image identities must remain immutable.

After that qualification evidence is accepted, freeze and run a separate cumulative
20-fixture one-round calibration. The final 20-fixture × 5-round campaign must then be
frozen separately; neither the 17-fixture calibration nor the future one-round
calibration may be relabelled as that denominator.

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

Then commit and container-qualify fixtures 018–020 as described above. Do not edit an
accepted manifest, relabel either historical image, start provider-backed experiments,
or rent a reference host before the 20-fixture campaign is frozen.
