<p align="center">
  <img src="assets/guildmind-logo.svg" alt="Guildmind logo: three agents connected through a shared spark inside a guild crest" width="220">
</p>

<h1 align="center">Guildmind</h1>

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
- later, independently calibrated judge societies and persistent institutional state.

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
8. **Judge societies:** calibrate independent evaluators against blinded human preferences.
9. **Transfer:** freeze successful institutions and test them on new distributions and domains.

PettingZoo is being considered as an optional environment adapter for inexpensive multi-agent testbeds and later transfer experiments. It will not define Guildmind's core scheduler or the coding-task lifecycle.

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

- validate and export versioned task, experiment, run, event, budget, artifact, and evaluation schemas;
- reserve aggregate budget before model work and reconcile reported usage afterward;
- store immutable artifacts by content hash and hash-link events in a single-writer SQLite ledger;
- run a scripted fake model against a repository-owned coding fixture;
- validate and apply a constrained patch to a copied workspace;
- execute trusted local tests or a two-phase black-box container evaluation;
- actively probe configured memory, PID, and writable-byte ceilings with versioned evidence; and
- verify the event chain, reconstruct terminal state, and compare normalized semantic digests across runs.

The current Stage 1 hardening checkpoint adds a restricted Docker invocation contract, a pinned image-owned two-phase evaluator, and crash recovery that closes an abandoned nonterminal run with explicit terminal evidence. These are development controls and contract tests, not a claim that hostile-code containment has passed.

The implemented path is:

```text
fixture task → fake model → validated patch → copied workspace
             → local evaluator, or candidate container → opaque response
                                  → isolated trusted scorer
             → artifacts + transactional event ledger → replay/recovery
```

This is engineering infrastructure for trusted repository-owned fixtures, not yet a hostile-code sandbox. Evaluator v2 closes the demonstrated same-interpreter false pass for the first fixture: candidate code receives only a patched workspace and expected-value-free JSON challenge, while a fresh scorer container receives the sealed oracle and bounded candidate response but no candidate workspace. Manifest, source, test, and oracle bytes are frozen before model dispatch; the trusted completion binds that frozen identity, and exact bounded candidate/scorer transcripts survive as content-addressed evidence. A strict [adversarial corpus](docs/adversarial-corpus.md) content-addresses nine functional, boundary-integrity, timeout, and output-exhaustion patches with exact predeclared outcomes. The complete matrix now passes under live development evaluation.

That boundary is deliberately narrow. It covers JSON-callable micro-fixtures, candidate code necessarily observes the call inputs, and it is not a general pytest or arbitrary-repository evaluator. The reproducibly built image ran successfully under the declared restrictions on Docker Desktop, including direct OOM, PID-ceiling, and exact writable-space probes, but that rootful ARM Linux virtual machine is not the required rootless x86_64 reference host. External tasks and real model-generated commands remain blocked until the checked-in gold/attack matrix and direct probes pass on that reference host and the remaining unsafe-patch corpus, process-kill recovery campaign, and 99% normal-fixture reliability campaign are complete. The [Stage 1 hardening gate report](docs/reviews/2026-08-01-stage-1-hardening-gate.md) records the exact evidence and retains a **NOT PASSED** verdict; [ADR 0004](docs/decisions/0004-two-phase-python-call-evaluator.md) records the v2 boundary and its limits.

Experiment 0001 also remains a draft until its worker model, spend ceilings, minimum relevant effect, and publication level are approved. Until both that Stage 0 approval and the Stage 1 boundary gate exist, the next work remains fixture hardening—not provider-backed pilots, baselines, genomes, or search.

## Quick start

Guildmind currently targets Python 3.12 and uses [uv](https://docs.astral.sh/uv/) for its locked environment.

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
```

Export the public JSON Schemas or run the 100-repetition semantic determinism check:

```bash
make schemas
make determinism
```

After building the evaluator image, run the active resource suite with its exact local
repository digest (never a mutable tag):

```bash
uv run guildmind probe-resources --development \
  --evaluator-image guildmind/evaluator@sha256:<digest>
```

The resulting `all_enforced` value describes that development host only.
`reference_passed` additionally requires the strict rootless x86_64 host policy and a
clean Git revision; development evidence cannot be promoted by relabeling it.
Use `--output <new-file.json>` to preserve canonical evidence; the command refuses to
overwrite an existing report.

`guildmind doctor` reports the trusted local fixture path separately from whether the configured Docker host/image passes the production sandbox probe. That probe is necessary but is not the Stage 1 gate: it does not certify evaluator discrimination, recovery campaigns, or fixture reliability. Generated run artifacts remain outside Git by default.

## Project documents

- [Starting brief](docs/starting-brief.md): the thesis, first experiment, evaluation strategy, risks, and research principles.
- [Staged build plan](docs/build-plan.md): the architecture, benchmark ladder, statistical design, stage gates, implementation roadmap, costs, and first-month backlog.
- [Experiment 0001 contract](docs/experiments/0001-institutional-search.md): the pilot protocol, claims, task partitions, budget semantics, lockbox rules, and open owner decisions.
- [Threat model](docs/threat-model.md): assets, trust boundaries, threats, controls, and release gates.
- [Adversarial evaluator corpus](docs/adversarial-corpus.md): manifest invariants, exact attack outcomes, evidence levels, and the resource-classification boundary.
- [Resource-probe evidence](docs/evidence/resource-probes/2026-08-02-docker-desktop/README.md): three canonical development reports for OOM, PID, writable-byte, and cleanup enforcement.
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
