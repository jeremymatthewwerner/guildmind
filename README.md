<p align="center">
  <img src="assets/guildmind-logo.svg" alt="Guildmind logo: three agents connected to a shared spark inside a guild crest" width="560">
</p>

<p align="center"><strong>Evolving institutions for artificial collective intelligence.</strong></p>

Guildmind is a research project about improving collective AI capability by evolving how agents organize, collaborate, evaluate one another, and preserve knowledge—not by changing the underlying model weights.

The project starts from a simple question:

> Can changing the institution around otherwise fixed agents produce reliable capability gains that generalize beyond the tasks used to discover it?

The idea is inspired by a feature of human intelligence: civilization advanced much faster than human biology. Language, specialization, education, peer review, standards, reputation, and governance allowed groups of broadly similar individuals to become much more capable collectively. Guildmind asks whether some of that leverage transfers to societies of AI agents.

This is a hypothesis to test, not a premise the project assumes.

## Core hypothesis

The initial claim is deliberately narrow:

> A machine-readable institution discovered on development tasks can outperform strong solo and multi-agent workflows on unseen software-engineering tasks, using the same model, tools, and aggregate resource budget.

An **institution** is more than a collection of role prompts. It includes persistent rules governing how work is delegated, reviewed, accepted, disputed, escalated, remembered, and rewarded. A planner–implementer–reviewer topology is an organization; requiring independent review before acceptance, with a bounded appeal process, is institutional.

Guildmind treats several questions as separate hypotheses:

- Can an institution improve capability at a fixed budget?
- Can search discover useful institutions more efficiently than random or manual design?
- Can reputation, certification, apprenticeship, or institutional memory improve future performance?
- Can evolution plus reward-trained, bounded institutional control adapt better than either mechanism alone?
- Can independent judge societies predict human preferences without drifting alongside workers?
- Do successful institutional rules transfer to new repositories, languages, or domains?

## What we are building

The project will provide a reproducible research platform with:

- a declarative **organizational genome** describing roles, communication, governance, memory, and budgets;
- an event-driven runtime that interprets genomes without hiding orchestration behavior inside an agent framework;
- isolated worker environments and a separate hidden-test evaluator;
- an authoritative budget ledger covering model calls, token classes, tool use, time, and estimated cost;
- complete candidate lineage, event traces, patches, failures, and replayable evidence;
- noise-aware search over valid institutional mutations; and
- later, persistent institutional state, bounded learned controllers, and independently calibrated judge societies.

The first implementation domain is software engineering. Coding tasks provide objective tests, isolated execution, fast iteration, and a strong basis for distinguishing actual improvement from persuasive-looking output.

## First research program

The first experiment compares a searched institution against four frozen, equal-budget baselines:

1. A solo agent.
2. A solo agent with an explicit reflection loop.
3. Equal-budget best-of-N attempts with a selector.
4. A hand-designed planner → implementer → reviewer team.

Every system receives the same underlying model snapshot, tools, task information, and aggregate cap. More agents do not receive more total inference merely because they can spend it in parallel.

Search occurs only on generated development tasks. One frozen champion is then evaluated in a sealed campaign of fresh, repository-disjoint tasks created after the selected model snapshot. Public benchmarks remain useful for compatibility, but the central claim cannot rest on a benchmark that may already be contaminated or heavily optimized against.

A positive result requires the champion to clear a preregistered improvement threshold against every required baseline. A development-only gain, a gain caused by additional inference, or a result dependent on evaluator leakage is negative or invalid—not success.

## Roadmap

The work is divided into gated stages:

1. **Experiment contract:** freeze hypotheses, eligibility rules, resource ceilings, analysis, and the leakage threat model.
2. **Measurement substrate:** build a deterministic runner, sandbox, evaluator, budget ledger, trace, and replay system.
3. **Strong baselines:** measure cost, variance, task difficulty, and infrastructure reliability.
4. **Institution language:** compile validated genomes into bounded executable state machines.
5. **Search machinery:** add typed mutations, lineage, archives, and multi-fidelity promotion.
6. **Exploratory search and sealed confirmation:** select one candidate and obtain a positive, equivocal, negative, or invalid result.
7. **Persistent institutions:** test reputation, certification, apprenticeship, and durable memory.
8. **Hybrid evolution and reinforcement learning:** evolve institutional constraints and executable policies while training compatible bounded controllers from objective reward.
9. **Judge societies:** calibrate independent evaluators against blinded human preferences.
10. **Transfer:** freeze successful institutions and test them on new distributions and domains.

PettingZoo is being considered as an optional environment adapter for inexpensive search and controller-learning testbeds and later transfer experiments. It will not define Guildmind's core scheduler or the coding-task lifecycle.

## Research principles

- **Evidence before scale.** Complexity must be earned by measured gains.
- **Institutions are executable.** Social metaphors become precise, versioned rules with observable effects.
- **Budgets are part of correctness.** Quality claims are meaningless without equal resource accounting.
- **Evaluation stays independent.** Workers cannot alter or inspect the standards by which they are selected.
- **Search and confirmation stay separate.** Development feedback never becomes confirmatory evidence.
- **Negative results are artifacts.** Failed institutions and discarded candidates remain part of the record.
- **Transfer is the real test.** Improvement on search tasks is discovery; improvement on unseen distributions is evidence.

## Current status

Guildmind now has its first deterministic local vertical slice. It can:

- validate and export versioned task, experiment, run, event, budget, artifact,
  evaluation, and reliability-campaign evidence schemas;
- reserve aggregate budget before model work and reconcile reported usage afterward;
- store immutable artifacts by content hash and hash-link events in a single-writer SQLite ledger;
- recover run state after a real process is killed at committed lifecycle, selected
  pre-commit, or model/evaluator in-flight boundaries, producing one replay-valid
  terminal state with conservative budget treatment;
- bind a verified all-run SQLite snapshot to a bounded recursive CAS audit that
  verifies committed bytes and typed relationships while classifying unreferenced,
  temporary, malformed, linked, and corrupt entries without mutating them;
- coordinate supported publishers and mutators with a persistent shared/exclusive
  maintenance lease and a fail-closed quarantine fence;
- explicitly quarantine only freshly authorized ownerless CAS files behind that
  exclusive lease, with immutable plans, no-replace moves, receipts, and forward-only
  restart reconciliation;
- publish CAS entries with an atomic platform-native no-replace rename rather than a
  crash-exposed hard-link/unlink pair;
- converge eight competing low-level CAS publishers on one immutable canonical blob,
  preserving the exact winning inode and cleaning every losing temporary;
- run a scripted fake model against a repository-owned coding fixture;
- validate and apply a constrained patch to a copied workspace;
- execute trusted local tests or a two-phase black-box container evaluation;
- actively probe configured memory, PID, writable-byte, planted-secret, mount,
  environment, credential, and network/socket boundaries with versioned evidence; and
- verify the event chain, reconstruct terminal state, and compare normalized semantic digests across runs.

The reliability-campaign path now defines a frozen full-factorial schedule,
content-bound fixture/code/evaluator identities, zero-retry attempts, derived aggregate
claims, and a hash-bound canonical report. A strict loader, development-only
executor/reconciler, and `guildmind campaign run` command have passed focused malformed,
failure, recovery, tampering, and no-overwrite cases. The first checked-in one-fixture
smoke reconciled one expected terminal result with zero infrastructure errors. This is
harness evidence, not the planned 100-attempt campaign and not proof of a 99% population
reliability claim; see the [campaign evidence](docs/evidence/reliability-campaigns/2026-08-03-one-fixture-smoke/README.md).

The [normal-fixture reliability corpus](docs/fixture-reliability-corpus.md) now freezes
20 materially different task families before the final denominator is assembled. Its
first breadth batch adds Unicode slug normalization, closed-interval coalescing, JSON
Pointer traversal, and structural stable deduplication. All four fixtures fail pristine
and pass their exact gold patch identically in three trusted-local repetitions. The same
exact controls and gold patches also matched in three two-phase development-container
repetitions apiece—24 evaluations and 48 disposable containers with zero infrastructure
errors. The [batch evidence](docs/evidence/fixture-qualification/2026-08-03-batch-001-development-container/README.md)
records the rootful ARM development-host boundary. The final 100-attempt campaign and
native rootless x86_64 reference repetition are still pending, so this is not yet part of
the final Stage 1 reliability claim.

A separately frozen
[`stage1-local-batch-001-v1`](campaigns/stage1-local-batch-001-v1.json) manifest then ran
fixtures 001–005 through the transactional local campaign harness. All 5/5 attempts were
terminal, expected, replay-valid, and storage-clean, with 70 total events, zero retries,
zero infrastructure errors, and zero provider cost. The
[canonical batch-calibration report](docs/evidence/reliability-campaigns/2026-08-03-batch-001-local-calibration/README.md)
is useful breadth evidence, but five attempts are not the final 100-attempt denominator
and do not prove a 99% population reliability claim.

The second breadth batch now adds an escaped run-length parser, exact integer
apportionment, stable topological ordering, and ordered nested JSON changes. For each of
fixtures 006–009, the exact pristine control failed and the gold patch passed identically
in three trusted-local repetitions. Those 24 results are local construction evidence;
the two-phase development-container matrix and a separately frozen cumulative Batch 002
campaign are the next gates.

The current Stage 1 hardening checkpoint adds a restricted Docker invocation contract, a pinned image-owned two-phase evaluator, crash recovery that closes an abandoned nonterminal run with explicit terminal evidence, and a read-only ledger/CAS integrity audit. Eleven real-process tests use pipe-synchronized boundaries and `SIGKILL` to cover five post-commit EventStore prefixes, four selected pre-commit rollback points, and the model-in-flight and evaluator-in-flight FixtureRunner boundaries. CAS publication now uses atomic platform-native no-replace rename; six synchronized Darwin kills cover the root, `sha256`, and shard `mkdir`-before-parent-`fsync` boundaries plus immediately before rename, immediately after rename, and after rename before directory `fsync`. Three further kills cover temporary creation, a flushed proper-prefix write, and the full write immediately before file `fsync`; exact stranded temporaries remain auditable across two idempotent retries. Explicit recovery acquires a cooperative shared maintenance lease before its fresh existing-only audit, checks the complete ledger and recursively reachable CAS bytes under the SQLite writer lock before staging, checks them again at the final pre-commit boundary, and returns the terminal event stream captured inside that transaction. Fixture publication holds the same shared lease through its final SQLite binding, while exclusive mode is reserved for state-wide maintenance and a present `quarantine/v1/ACTIVE` fence blocks mutation. Shared recovery is not quiescent against another shared publisher; it is safe for its narrower terminalization action because it never acts on ownerless findings and revalidates the complete ledger/reachable-CAS graph under the writer lock. Explicit quarantine instead holds exclusive mode, obtains its own fresh top-level audit, accepts only the complete allowlisted set of ownerless valid-finalized, corrupt-finalized, or temporary regular files, and revalidates each pending source before a descriptor-relative no-replace move. A canonical BEFORE/PLAN/ACTIVE chain precedes moves; deterministic receipts and AFTER/COMPLETE evidence precede fence removal. Restart can repair the ambiguous post-rename/pre-receipt window but fails closed if both or neither planned name exists. FixtureRunner exceptions after run creation use the guarded recovery path; pre-dispatch budget refusal uses the same dual guard, while a later budget error receives conservative general recovery. Successful evaluation completion also returns its terminal manifest/events from inside its write transaction, and SQLite writer failures become stable recovery denials. `replay` and `report` open existing state read-only and create nothing when it is absent. A missing, non-directory, or symlinked state leaf remains no-create. Any existing real state directory with a usable lock path—including empty or damaged storage, an unknown run, or an ACTIVE fence—may gain and synchronize the persistent coordination lock before later classification or denial. Six cooperating-process concurrency cases now cover publishers, an exclusive maintainer, a durable ACTIVE fence, overlapping resumers, and the post-unfence/pre-release lease window. Sixteen additional pipe-synchronized `SIGKILL` cases cover the quarantine record, move, receipt, completion, fence-removal, and lease-release prefixes, followed by fresh-process completion and a second identity-preserving no-op. A deterministic same-digest matrix additionally gates eight persistent spawned publishers before and after the real no-replace syscall across 20 unique digest/shard rounds: all 160 low-level puts converge on one winner per round, seven identity-exact losers before cleanup, eight identical references afterward, no residual temporary, and an exact 20-finding final audit. These remain bounded development controls: hostile same-UID concurrency, power-loss durability, reference-host repetition, and general hostile-code containment have not passed.

The implemented path is:

```text
fixture task → fake model → validated patch → copied workspace
             → local evaluator, or candidate container → opaque response
                                  → isolated trusted scorer
             → artifacts + transactional event ledger → replay/recovery
```

This is engineering infrastructure for trusted repository-owned fixtures, not yet a hostile-code sandbox. Evaluator v2 closes the demonstrated same-interpreter false pass for the first fixture: candidate code receives only a patched workspace and expected-value-free JSON challenge, while a fresh scorer container receives the sealed oracle and bounded candidate response but no candidate workspace. Manifest, source, test, and oracle bytes are frozen before model dispatch; the trusted completion binds that frozen identity, and exact bounded candidate/scorer transcripts survive as content-addressed evidence. A strict [adversarial corpus](docs/adversarial-corpus.md) content-addresses 19 patch-intake, functional, boundary-integrity, timeout, output-exhaustion, and OOM cases with exact predeclared outcomes. Nine unsafe shapes now fail before Git application or sandbox dispatch; the other ten cases pass under live development evaluation.

That boundary is deliberately narrow. It covers JSON-callable micro-fixtures, candidate code necessarily observes the call inputs, and it is not a general pytest or arbitrary-repository evaluator. The reproducibly built image ran successfully under the declared restrictions on Docker Desktop, including direct OOM, PID-ceiling, exact writable-space, high-entropy planted-secret, mount/environment, credential, DNS/TCP, host-route, and Unix-socket probes. Both evaluator phases were contained in three repeated reports with verified cleanup. That rootful ARM Linux virtual machine is not the required rootless x86_64 reference host, however, and the probe covers evaluator candidate/scorer requests rather than a not-yet-built general worker dispatcher. The process-kill slice proves committed-prefix recovery, rollback at four mutation-rich pre-commit points, the two external-work boundaries, and nine exact CAS creation/write/publication/durability boundaries on Darwin. It also observes the exact finalized orphan blobs left when response or evaluation references roll back. A subsequent [recursive storage-integrity audit](docs/evidence/storage-integrity/2026-08-02-recursive-audit/README.md) re-verifies referenced bytes and classifies those filesystem entries, and a no-create coordinator distinguishes missing, invalid, empty, damaged, orphaned, and healthy ledger/CAS pairs without initializing them. The [guarded-recovery checkpoint](docs/evidence/crash-recovery/2026-08-03-guarded-recovery/README.md) makes external local-fixture, runner-exception, and budget-refusal terminalization consume that evidence freshly, validate it before mutation and again at final pre-commit, and capture the returned stream transactionally. The [CAS temporary-write checkpoint](docs/evidence/crash-recovery/2026-08-03-cas-temporary-write/README.md) closes the three userspace process-kill points left by the original atomic-publication matrix. The subsequent [CAS publisher-contention checkpoint](docs/evidence/crash-recovery/2026-08-03-cas-publisher-contention/README.md) closes the bounded cooperative low-level same-digest race with 8 persistent processes, 20 unique rounds, and 160 contested puts. The [cooperative maintenance lease](docs/evidence/storage-integrity/2026-08-03-maintenance-lease/README.md) excludes supported publication/recovery from exclusive maintenance and fails closed on a durable ACTIVE fence, but direct low-level storage use remains a trusted caller boundary. The explicit [resumable quarantine checkpoint](docs/evidence/storage-integrity/2026-08-03-resumable-quarantine/README.md) now preserves authorized ownerless bytes through immutable plans, no-replace moves, receipts, and restart reconciliation. The follow-up [quarantine process-crash and concurrency matrix](docs/evidence/storage-integrity/2026-08-03-quarantine-process-crash/README.md) exercises that protocol across real process death and cooperating-process exclusion. Hostile same-UID and broader open-process races, runtime-level contention beyond the trusted low-level primitive, and real-provider idempotency remain open. External tasks and real model-generated commands remain blocked until the checked-in gold/attack matrix and direct probes pass on the reference host and the remaining reference-host, containment, durability, and 99% normal-fixture reliability work is complete. The [Stage 1 hardening gate report](docs/reviews/2026-08-01-stage-1-hardening-gate.md) records the exact evidence and retains a **NOT PASSED** verdict; [ADR 0004](docs/decisions/0004-two-phase-python-call-evaluator.md) records the v2 boundary and its limits.

Experiment 0001 also remains a draft until its worker model, spend ceilings, minimum relevant effect, and publication level are approved. Until both that Stage 0 approval and the Stage 1 boundary gate exist, the next work remains fixture hardening—not provider-backed pilots, baselines, genomes, or search.

## Quick start

Guildmind currently targets Python 3.12 and uses [uv](https://docs.astral.sh/uv/) for its locked environment.
Everything implemented today runs locally with Git, Python, and `uv`; the scripted
fixture model makes no paid API calls. Docker Desktop is optional for the container
evaluator and containment/resource probes. No runtime hosting or deployment service is
needed at this stage. Later provider-backed campaigns will incur model usage, and the
authoritative hostile-code gate must eventually be repeated on a clean rootless x86_64
Linux host, which may be an owned machine or a short-lived rental—there is no reason to
provision one yet.

```bash
uv sync
make check
uv run guildmind doctor
```

Run and replay the deterministic fixture:

```bash
uv run guildmind run fixtures/001-python-addition \
  --state-dir .guildmind \
  --run-id demo-run

uv run guildmind replay demo-run --state-dir .guildmind
uv run guildmind report demo-run --state-dir .guildmind

# If a previous process died mid-run, close that attempt without redispatching it:
uv run guildmind recover interrupted-run --state-dir .guildmind

# Explicitly preserve freshly authorized ownerless CAS files outside the live store:
uv run guildmind quarantine --state-dir .guildmind
```

Run the frozen one-fixture development campaign into new state and report paths:

```bash
mkdir -p runs/reliability-campaigns
uv run guildmind campaign run campaigns/stage1-local-smoke-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-smoke-v1-state \
  --output runs/reliability-campaigns/stage1-local-smoke-v1-report.json

# Or run the frozen five-fixture calibration with different new paths:
uv run guildmind campaign run campaigns/stage1-local-batch-001-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/stage1-local-batch-001-rerun-state \
  --output runs/reliability-campaigns/stage1-local-batch-001-rerun-report.json
```

The command refuses an existing state or output path and uses no paid model provider.
Exit 0 means the complete frozen harness verdict passed; exit 2 means a valid canonical
report was produced but its gate failed; exit 1 is a configuration/evidence error.

Export the public JSON Schemas or run the 100-repetition semantic determinism check:

```bash
make schemas
make determinism
```

After building the evaluator image, run the active resource and two-phase containment
suites with its exact local repository digest (never a mutable tag):

```bash
uv run guildmind probe-resources --development \
  --evaluator-image guildmind/evaluator@sha256:<digest>

uv run guildmind probe-containment --development \
  --evaluator-image guildmind/evaluator@sha256:<digest>
```

The resulting `all_enforced` and `all_contained` values describe that development host only.
`reference_passed` additionally requires the strict rootless x86_64 host policy and a
clean Git revision; development evidence cannot be promoted by relabeling it.
Use `--output <new-file.json>` to preserve canonical evidence; the command refuses to
overwrite an existing report.

`guildmind doctor` reports the trusted local fixture path separately from whether the configured Docker host/image passes the production sandbox probe. That probe is necessary but is not the Stage 1 gate: it does not certify evaluator discrimination, complete transaction/CAS recovery, or fixture reliability. Generated run artifacts remain outside Git by default.

## Project documents

- [Current pause-point handoff](docs/progress/2026-08-03-pause-point.md): a complete
  summary of what has been built, what runs locally now, the remaining evidence limits,
  expected future costs, and the exact next checkpoint.
- [Starting brief](docs/starting-brief.md): the thesis, first experiment, evaluation strategy, risks, and research principles.
- [Staged build plan](docs/build-plan.md): the architecture, benchmark ladder, statistical design, stage gates, implementation roadmap, costs, and first-month backlog.
- [Hybrid evolution and reinforcement learning](docs/hybrid-evolution-rl.md): the future Stage 8 constraint/policy boundary, RL contract, inheritance semantics, evidence identity, and matched comparison arms.
- [Experiment 0001 contract](docs/experiments/0001-institutional-search.md): the pilot protocol, claims, task partitions, budget semantics, lockbox rules, and open owner decisions.
- [Threat model](docs/threat-model.md): assets, trust boundaries, threats, controls, and release gates.
- [Adversarial evaluator corpus](docs/adversarial-corpus.md): manifest invariants, exact attack outcomes, evidence levels, and the resource-classification boundary.
- [Normal-fixture reliability corpus](docs/fixture-reliability-corpus.md): the frozen 20-family matrix, anti-duplication rules, first development-qualified breadth batch, evidence boundary, and batch protocol.
- [Fixture Batch 001 development-container evidence](docs/evidence/fixture-qualification/2026-08-03-batch-001-development-container/README.md): three repeated pristine failures and gold passes for each of four fixtures, with a self-bound report and explicit non-reference host boundary.
- [Fixture Batch 001 local campaign calibration](docs/evidence/reliability-campaigns/2026-08-03-batch-001-local-calibration/README.md): five content-bound, zero-retry attempts with complete terminal ledger/CAS evidence and an explicit non-statistical boundary.
- [Unsafe-patch intake evidence](docs/evidence/patch-intake/2026-08-02-development/README.md): the nine-case pre-application matrix, parser hardening, hashes, and evidence limits.
- [Resource-probe evidence](docs/evidence/resource-probes/2026-08-02-docker-desktop/README.md): three canonical development reports for OOM, PID, writable-byte, and cleanup enforcement.
- [Containment-probe evidence](docs/evidence/containment-probes/2026-08-02-docker-desktop/README.md): three canonical evaluator candidate/scorer reports for planted-secret, mount/environment, credential, network/socket, privilege, and cleanup boundaries.
- [One-fixture reliability-campaign smoke](docs/evidence/reliability-campaigns/2026-08-03-one-fixture-smoke/README.md): the first content-bound, zero-retry CLI campaign and canonical report, with explicit one-attempt/statistical/reference-host limits.
- [Real-process crash-recovery evidence](docs/evidence/crash-recovery/2026-08-02-process-sigkill/README.md): eleven synchronized `SIGKILL` cases across committed lifecycle, selected pre-commit, and runner external-work boundaries, with exact rollback/orphan observations and remaining gaps.
- [Atomic no-replace CAS publication](docs/evidence/crash-recovery/2026-08-03-atomic-cas-publication/README.md): Darwin no-replace publication, six exact synchronized `SIGKILL` boundaries, a narrow Linux/arm64 syscall smoke, and explicit power-loss/reference-host limits.
- [CAS temporary-write process-crash evidence](docs/evidence/crash-recovery/2026-08-03-cas-temporary-write/README.md): three exact pre-publication `SIGKILL` boundaries, typed stranded temporaries, two-retry identity preservation, and explicit process-crash/power-loss limits.
- [Same-digest CAS publisher-contention evidence](docs/evidence/crash-recovery/2026-08-03-cas-publisher-contention/README.md): eight persistent spawned publishers, 20 unique digest/shard rounds, 160 contested low-level puts, exact winner/loser inode proofs, and a 20-finding final audit.
- [Guarded existing-only recovery evidence](docs/evidence/crash-recovery/2026-08-03-guarded-recovery/README.md): fresh ledger/CAS gating, a second final pre-commit guard, transactionally captured results, guarded runner-exception and budget-refusal cleanup, no-create inspection, and the residual quiescence boundary.
- [Recursive storage-integrity audit](docs/evidence/storage-integrity/2026-08-02-recursive-audit/README.md): verified ledger-root commitments, typed recursive CAS reachability, bounded orphan inventory, adversarial review outcomes, and the remaining mutation boundary.
- [Cooperative state-wide maintenance lease](docs/evidence/storage-integrity/2026-08-03-maintenance-lease/README.md): persistent no-follow flock coordination, shared runtime/exclusive maintenance modes, ACTIVE-fence behavior, real-process exclusion and `SIGKILL` release, and the trusted low-level boundary.
- [Resumable orphan quarantine](docs/evidence/storage-integrity/2026-08-03-resumable-quarantine/README.md): fresh exclusive authorization, immutable plan/receipt/completion records, descriptor-relative no-replace moves, forward restart reconciliation, and explicit crash-evidence limits.
- [Quarantine process-crash and concurrency evidence](docs/evidence/storage-integrity/2026-08-03-quarantine-process-crash/README.md): sixteen exact `SIGKILL` prefixes, six cooperating-process exclusion/resumption cases, fresh-process completion, identity-preserving idempotency, and explicit power-loss/hostility limits.
- [Architecture decisions](docs/decisions/): the Python environment, evidence storage, and sandbox/evaluator boundary.
- [Plan review ledger](docs/reviews/2026-07-31-plan-audit.md): durable dispositions for the benchmark, runtime, and search/evaluation review findings.
- [Stage 1 hardening gate](docs/reviews/2026-08-01-stage-1-hardening-gate.md): implemented development controls, smoke evidence, remaining adversarial matrix, reference-host procedure, and current verdict.

## Initial non-goals

- Training or fine-tuning foundation-model weights.
- Building an open-ended autonomous civilization.
- Treating prompt search alone as institutional improvement.
- Allowing worker and judge populations to co-evolve against an unvalidated score.
- Replacing objective tests with model judgment.
- Optimizing a public leaderboard without cost, leakage, and transfer controls.

The long-term ambition is an artificial collective capable of improving how intelligence is organized, evaluated, and transmitted. The immediate obligation is much smaller: run one clean experiment that could prove the idea wrong.
