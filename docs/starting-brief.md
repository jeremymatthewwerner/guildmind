# Guildmind: Starting Brief

**Status:** Initial project brief<br>
**Date:** 2026-07-31<br>
**Working domain:** Agentic software engineering

## 1. Thesis

Most work on recursive AI improvement treats the individual model as the unit that improves. Guildmind investigates a different unit: the institution in which multiple agents work.

Human collective intelligence advanced through language, specialization, education, peer review, standards, reputation, and governance—not only through changes to individual brains. The analogous opportunity for AI may be to improve the structures that coordinate, evaluate, and transmit the work of otherwise fixed models.

Guildmind's central hypothesis is:

> A population of agent organizations can discover institutional designs that outperform hand-designed workflows under equal model, tool, time, and token budgets—and some of those gains will transfer to held-out tasks.

This is a hypothesis to test, not a premise to assume.

## 2. Project objective

Build a reproducible research platform that can:

1. Represent an agent organization as a versioned, mutable specification.
2. Run that organization on a controlled set of tasks.
3. Measure correctness, reliability, cost, and generalization.
4. Propose variations to organizational and institutional rules.
5. Select candidates without contaminating held-out evaluation.
6. Preserve the lineage and evidence behind every claimed improvement.

The near-term outcome is not a general artificial civilization. It is credible evidence for or against institutional search as a source of agent capability.

## 3. Key terms

- **Agent:** One model instance acting with a role, context, tools, and budget.
- **Society:** A group of agents and the communication topology connecting them.
- **Institution:** Persistent rules governing how work is assigned, reviewed, accepted, escalated, remembered, and rewarded.
- **Organizational genome:** A machine-readable specification of a society and its institutional rules.
- **Worker society:** The system that attempts a task.
- **Judge society:** An independent system that assesses work where objective verification is incomplete.
- **Evolution:** Any generate–evaluate–select–inherit loop over organizational genomes. The first implementation need not imitate biological evolution closely.

The distinction between a society and an institution matters. A planner–implementer–reviewer topology is a society design. A rule requiring independent review before acceptance, with an escalation path after disagreement, is institutional.

## 4. Research questions

The project is organized around five questions:

1. **Capability:** Can institutional search beat a single-agent baseline and strong hand-designed multi-agent baselines?
2. **Efficiency:** Do gains remain after controlling for tokens, wall-clock time, tool calls, and model invocations?
3. **Generalization:** Do discovered institutions improve performance on tasks and repositories not used during selection?
4. **Causality:** Which institutional components produce the gain, and do ablations reproduce that conclusion?
5. **Evaluation integrity:** Can independently calibrated judges add useful signal without enabling workers and judges to co-adapt toward a misleading proxy?

## 5. First falsifiable experiment

### Claim under test

Searching over a constrained organizational genome will produce a workflow that achieves a higher cost-adjusted success rate on held-out coding tasks than both a solo agent and a fixed expert-designed workflow.

### Controlled inputs

All candidates use the same:

- underlying model or model pool;
- tools and execution environment;
- per-task token and time budget;
- task information;
- benchmark split; and
- objective test harness.

### Initial baselines

1. **Solo:** one agent plans, implements, and verifies.
2. **Fixed team:** a hand-designed planner → implementer → reviewer workflow.
3. **Best-of-N:** multiple independent solo attempts with a fixed selection procedure.
4. **Searched institution:** a candidate selected by the Guildmind search loop.

Best-of-N is essential: a multi-agent system must demonstrate more than the benefit of spending additional inference.

### Initial genome

Keep the first search space deliberately small:

- available roles;
- number of agents per role;
- communication topology;
- delegation and handoff rules;
- number and type of review rounds;
- acceptance, veto, and escalation rules;
- allocation of a fixed token/tool budget; and
- shared-memory read/write policy.

Model weights, tools, and benchmark tests remain fixed. Prompts may be versioned parameters, but results must distinguish prompt optimization from institutional change.

### Task progression

1. A small local corpus with deterministic tests, used to debug the harness.
2. A frozen development set, used for organizational search.
3. A sealed held-out set, used only for final comparisons.
4. At least one recognized external benchmark once the harness is stable.

Exact benchmark selection is an early design decision. The benchmark must support isolated execution, repeatable scoring, and a realistic enough task distribution to make transfer meaningful.

### Primary metrics

- percentage of tasks resolved;
- resolved tasks per million tokens;
- total model and tool cost per resolved task;
- wall-clock time;
- variance across repeated runs; and
- improvement retained on held-out tasks.

Secondary diagnostics include invalid edits, regressions introduced, test-selection errors, reviewer reversals, communication overhead, and failure category.

### Success criterion

The first experiment succeeds if the searched institution:

- improves held-out task success over every baseline;
- stays within the same declared resource budget;
- reproduces across multiple runs and task subsets; and
- retains a meaningful portion of its gain after removing any single nonessential role or rule.

A result that improves only the development set, uses materially more inference, or depends on evaluator leakage is a negative result.

## 6. System shape

The minimal platform has six separable components:

```text
Task corpus -> Experiment runner -> Worker society -> Sandbox/tests
                       |                  |
                       v                  v
                 Event/artifact log <- Objective results
                       |
                       v
              Search and selection loop
```

1. **Task corpus:** immutable task inputs, tests, metadata, and split membership.
2. **Genome registry:** versioned organizational specifications and parent/child lineage.
3. **Experiment runner:** deterministic orchestration, budget enforcement, and isolation.
4. **Agent runtime:** role execution, messaging, memory, and tool access.
5. **Evaluator:** objective test results first; judge and human signals later.
6. **Evidence store:** prompts, messages, tool traces, patches, scores, costs, seeds, and environment versions.

The core abstractions should remain framework-independent. An agent framework may provide plumbing, but Guildmind's research artifact is the institutional representation, search process, and evidence—not a wrapper around a particular framework.

## 7. Evaluation strategy

### Objective anchors first

Coding tests provide the initial source of ground truth. A candidate cannot negotiate with or rewrite its evaluator. Tests and hidden evaluation artifacts must be inaccessible to workers.

### Human judgment later

Tests do not fully capture maintainability, elegance, risk, or usefulness. Later phases will collect blinded human pairwise preferences and use them to calibrate judge societies. Humans evaluate the judges' agreement and failure modes rather than rating every worker output.

### Separate worker and judge populations

Worker and judge selection must use separate state, lineages, and data partitions. Static tests, periodic human audits, adversarial evaluation, and multiple judge families provide external anchors against co-evolution or tacit collusion.

### Research hygiene

- Predeclare the primary metric and stopping rule for major experiments.
- Freeze and fingerprint task splits.
- Record all failed and discarded runs.
- Repeat stochastic runs with declared seeds and temperatures.
- Compare under equal budgets.
- Test on sealed data only after a candidate is selected.
- Run component ablations before attributing gains to an institution.

## 8. Phased roadmap

### Phase 0 — Experiment contract

- Choose the first task corpus and baseline model.
- Define the genome schema and resource accounting.
- Write the threat model for leakage and score gaming.
- Specify metrics, splits, and the first success threshold.

**Exit condition:** another researcher could implement the first experiment without guessing what counts as success.

### Phase 1 — Reproducible runner

- Execute one agent on one task in an isolated environment.
- Persist a complete, replayable trace.
- Enforce token, cost, tool, and time limits.
- Run solo, fixed-team, and best-of-N baselines.

**Exit condition:** repeated baseline runs produce explainable results and complete evidence.

### Phase 2 — Search over societies

- Implement mutation and selection over the constrained genome.
- Search only on the development split.
- Compare selected candidates on the held-out split.
- Perform ablations and budget-normalized analysis.

**Exit condition:** a clear positive or negative answer to the first experiment.

### Phase 3 — Institutional memory and reputation

- Add rules for certification, promotion, apprenticeship, and reputation.
- Test whether durable state improves future performance rather than merely accumulating context.
- Measure the cost of institutional overhead.

**Exit condition:** persistent rules outperform equivalent stateless workflows on longitudinal tasks.

### Phase 4 — Judge societies

- Collect a small, blinded human preference dataset.
- Compare independent judge organizations on calibration and robustness.
- Keep judge evolution isolated from worker evolution.
- Red-team reward hacking and preference drift.

**Exit condition:** judge signal predicts held-out human preference and adds value beyond objective tests.

### Phase 5 — Transfer

- Freeze successful institutional designs.
- Transfer them to a new coding distribution, then a non-coding domain.
- Separate domain knowledge transfer from organizational transfer.

**Exit condition:** an institution retains measurable benefit without being re-optimized on the target domain.

## 9. Non-goals for the first release

- Training or fine-tuning foundation-model weights.
- Building an open-ended autonomous system.
- Claiming recursive self-improvement from prompt changes alone.
- Evolving workers and judges together.
- Replacing objective tests with model-based judgment.
- Simulating every feature of a human economy or government.
- Optimizing headline benchmark scores without cost and leakage controls.

## 10. Principal risks

| Risk | Why it matters | Initial mitigation |
|---|---|---|
| Benchmark overfitting | Search can memorize a task distribution | Sealed splits, external benchmark, transfer test |
| Resource confounding | More agents can look smarter simply by spending more | Strict equal budgets and best-of-N baseline |
| Evaluator leakage | Workers may infer or access hidden tests | Isolation, access controls, artifact fingerprints |
| Proxy gaming | Selection may exploit weaknesses in a score | Multiple metrics, adversarial tests, human audits |
| Co-adaptation | Workers and judges may reinforce shared errors | Separate populations, data, and selection loops |
| Irreproducibility | Model and tool stochasticity can hide weak effects | Full traces, repeated trials, environment pinning |
| Institutional overhead | Coordination can cost more than it contributes | Measure messages, latency, tokens, and ablations |
| Anthropomorphic framing | Social metaphors may obscure simpler mechanisms | Define every institution operationally in code |

## 11. Design principles

1. **Evidence before scale.** Earn complexity through measured gains.
2. **Institutions are executable.** Every social metaphor must become a precise rule with observable effects.
3. **Budgets are part of correctness.** Capability claims are meaningless without resource accounting.
4. **Evaluation stays independent.** Workers do not modify the standards by which they are selected.
5. **Negative results are artifacts.** Failed institutions and failure analyses remain part of the research record.
6. **Lineage is explicit.** Every candidate records its parents, mutations, environment, and evidence.
7. **Transfer is the real test.** Development-set improvement is discovery; held-out improvement is evidence.

## 12. Immediate work package

The first implementation cycle should produce four artifacts:

1. **Experiment specification:** task corpus, splits, budgets, metrics, and stopping rules.
2. **Genome schema:** a small declarative format with validation and versioning.
3. **Baseline runner:** solo, fixed-team, and best-of-N execution with complete traces.
4. **Analysis notebook or report:** budget-normalized results and failure taxonomy.

Recommended order:

1. Select 20–50 inexpensive, deterministic tasks for harness development.
2. Define a single-task run manifest and result schema.
3. Implement the solo baseline end to end.
4. Add fixed-team and best-of-N baselines without changing the evaluator.
5. Freeze the experiment contract before implementing search.

## 13. Decisions still to make

- Which coding benchmark and model provide the first credible but affordable experiment?
- Is the initial unit of selection a complete genome or one institutional rule at a time?
- Which mutation operators can produce valid, interpretable candidates?
- How should resource budgets account for parallel execution and cached context?
- What minimum effect size justifies advancing from society search to persistent institutions?
- What evidence is required before calling a change an institutional improvement rather than prompt optimization?

## 14. Long-term direction

If the initial hypothesis survives controlled testing, Guildmind can expand from agent societies to evolving systems of review, accreditation, apprenticeship, reputation, governance, and institutional memory. The long-term research question is whether an artificial collective can improve not just the solutions it produces, but the durable processes by which it produces, evaluates, and teaches those solutions.

That ambition remains downstream of the first obligation: run one clean experiment that could prove the idea wrong.
