# Real-Process SIGKILL Recovery — 2026-08-02

**Evidence level:** development process test<br>
**Host:** Apple Silicon macOS<br>
**Mechanism:** spawned child process, pipe-synchronized phase or pre-`COMMIT`
barrier, parent-issued POSIX `SIGKILL`<br>
**Stage 1 effect:** partial recovery evidence; the gate remains **NOT PASSED**

## Claim established

Eleven integration cases kill a real Guildmind process only after the child has
announced an exact lifecycle or pre-`COMMIT` boundary over a multiprocessing pipe.
The parent does not use a sleep to guess whether the boundary has been reached. After
the child exits from `SIGKILL`, a new `EventStore` or `FixtureRunner` instance opens
the same SQLite WAL database and recovers the run.

The cases establish the following bounded claims:

- each of five externally visible, post-`COMMIT` EventStore prefixes survives with
  its exact event sequence;
- each of four selected pre-`COMMIT` transactions rolls back to its exact preceding
  phase after all transaction-body SQL has executed;
- killing the real fixture runner while its model or evaluator dependency is in
  flight preserves the preceding committed prefix;
- SQLite integrity and foreign-key checks pass after each kill;
- recovery creates one replay-valid terminal state and a second recovery leaves the
  event list and manifest unchanged;
- an outstanding model request becomes `ambiguous`, its entire reservation is
  conservatively charged, and no reservation remains;
- a committed model response keeps its actual usage and patch, while missing
  evaluation outputs are recorded explicitly as `artifact.not_produced`;
- a run already terminal after committed evaluation stays successful and receives
  no second terminal event;
- CAS bytes referenced by the recovered manifests and events verify against their
  recorded digest and size. The runner cases also verify the task's transitively
  referenced problem, repository snapshot, and visible-test bytes; and
- a rolled-back model response leaves exactly one finalized, unreferenced patch
  blob, while a rolled-back evaluation leaves exactly three finalized, unreferenced
  evaluation blobs. The two rolled-back recovery transactions leave no new orphan.

The pre-`COMMIT` matrix uses an integration-only wrapper around the already-open
SQLite connection; it adds no production hook. The orphan inventory is a test
observation, not a product startup audit or cleanup facility. The cases do **not**
interrupt a CAS temporary write, file/directory `fsync`, or rename, nor do they
exercise a race between concurrent writers.

## Tested boundary matrix

| Boundary | Durable prefix at kill | Recovery result |
|---|---|---|
| `run_created` | `run.created` | Infrastructure-error terminal; every expected output is explicitly interrupted; no budget is charged. |
| `task_bound` | Run started and `task_spec` recorded | Infrastructure-error terminal; `task_spec` remains bound; later outputs are explicitly interrupted; no budget is charged. |
| `model_request_outstanding` | Request intent and maximum reservation committed | Request becomes ambiguous; full maximum reservation moves to used; missing patch/evaluation outputs are explicit; one infrastructure-error terminal is committed. |
| `model_response_completed` | Response, actual usage, and patch committed | Actual usage and patch survive; evaluation outputs are explicit interruptions; one infrastructure-error terminal is committed. |
| `evaluation_completed` | Evaluation artifacts, budget snapshot, and successful terminal committed | Recovery is an idempotent no-op; the one existing successful terminal remains authoritative. |
| Pre-`COMMIT` model response | Patch is finalized in CAS; response/artifact/budget SQL has executed but is uncommitted | SQLite rolls back to the outstanding request. Recovery classifies it ambiguous and charges the full reservation. The patch remains as exactly one finalized, unreferenced blob and is not inferred into the manifest. |
| Pre-`COMMIT` evaluation | Three evaluation blobs are finalized in CAS; artifact/evaluation/budget/terminal SQL has executed but is uncommitted | SQLite rolls back to the completed response. Recovery retains actual model usage and patch, marks evaluation outputs interrupted, and ignores exactly three finalized, unreferenced evaluation blobs. |
| Pre-`COMMIT` recovery from outstanding request | Ambiguity, budget, absence, and terminal SQL has executed but is uncommitted | SQLite restores the outstanding-request prefix. A fresh recovery produces the one ambiguous/full-charge terminal; no new CAS orphan exists. |
| Pre-`COMMIT` recovery from completed response | Absence, budget, and terminal SQL has executed but is uncommitted | SQLite restores the completed-response prefix. A fresh recovery retains actual usage and patch and produces one interrupted terminal; no new CAS orphan exists. |
| FixtureRunner `model_entered` | Real runner is inside `ModelClient.propose_patch` after request/reservation commit | Full reservation is charged as ambiguous; task evidence survives; patch and evaluation outputs are explicit interruptions. |
| FixtureRunner `evaluator_entered` | Real runner is inside `Evaluator.evaluate` after response/patch commit | Actual model usage and exact patch survive; evaluation outputs are explicit interruptions. |

The direct EventStore matrix uses deterministic clocks and exact expected event-type
tuples. The FixtureRunner matrix uses a blocking model or evaluator test double only
to expose the synchronization point; the production runner and persistence paths are
otherwise exercised. No provider or Docker boundary is claimed by this suite.

## Focused verification record

Command:

```bash
uv run pytest -q \
  tests/integration/test_process_crash_recovery.py \
  tests/integration/test_fixture_runner_process_crash.py
```

Result on 2026-08-02:

```text
...........                                                              [100%]
11 passed in 1.11s
```

A separate development stability run invoked the same focused command ten
consecutive times. All ten invocations passed: 110/110 case executions, with each
invocation completing in 1.10–1.16 seconds. This repeat exercises the same narrow
crash matrix; it is not the predeclared 99% normal-fixture reliability campaign.

Test sources at the time of this focused run:

| File | SHA-256 |
|---|---|
| `tests/integration/test_process_crash_recovery.py` | `2722e7f475ea2ddca6de6ceb04d12712889bafbb3d23cde37cc5d83a3714ad03` |
| `tests/integration/test_fixture_runner_process_crash.py` | `5c5997897fb9309221f91feb76c1ce3727efa1bb48cee490805e9501e96ccfa6` |

The recorded duration is observational, not a performance gate. The tests are
skipped where POSIX `SIGKILL` is unavailable.

## Evidence boundary and remaining work

This checkpoint does not satisfy the full kill-boundary acceptance check in
[ADR 0002](../../../decisions/0002-sqlite-and-content-addressed-artifacts.md).
The following remain open:

- kills during CAS temporary write, file `fsync`, atomic rename, or directory
  `fsync`;
- startup verification of every referenced CAS byte and reporting/quarantine of
  orphan temporary and finalized blobs;
- commit races, cross-process writer exclusion, and open-process/concurrency stress;
- real-provider idempotency, polling, SDK retry suppression, duplicate-execution
  treatment, and invoice/usage reconciliation;
- repetition on the dedicated rootless x86_64 reference host; and
- the predeclared normal-fixture campaign with at least 99% free of infrastructure
  error.

No result here authorizes external repositories, hostile code, arbitrary
model-generated commands, or provider-backed campaigns.
