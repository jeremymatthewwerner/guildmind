# Experiment 0001: Institutional Search Pilot and Evidence Contract

**Status:** Draft Stage 0 pilot contract; not yet approved<br>
**Date:** 2026-07-31<br>
**Scope:** Stages 0–6 of the first software-engineering research program<br>
**Supersedes:** Nothing

This document freezes a pilot and the rules that turn pilot evidence into a final protocol. It does **not** claim that an unknown model, effect size, sample size, or spend ceiling has already been selected.

## 1. Change control

Every normative rule and number in this document is a **frozen default** unless it is explicitly an **owner decision required** or an output of the deterministic procedure in Section 8. Frozen defaults become binding when the Stage 0 checklist is approved; owner decisions must be completed before that approval.

- Before pilot outcomes exist, a material change requires a reviewed revision with a new content hash.
- After pilot outcomes exist, only the deterministic rules in Section 8 may fill the final protocol. Any other material change starts a new numbered pilot revision and reruns affected pilot work.
- After `development_selection` is opened, changing the genome, finalist slate, selection rule, model, cap, or analysis starts a new experiment. The opened split is retired.
- After `confirmation_lockbox` is opened, no treatment change is permitted. An integrity failure retires the lockbox and makes the affected comparison invalid.

Stage 0 approval freezes the process. A reviewed, content-hashed Stage 2 revision freezes the exact campaign.

## 2. Claims and current status

### H1 — institutional effect

> Under the same model, tools, initial task information, and aggregate per-attempt resource cap, one institution selected on development data has a higher repository-balanced probability of resolving a fresh software-engineering task than each required frozen baseline.

**Status:** Planned confirmatory claim. It becomes confirmatory only when the Stage 2 protocol revision freezes the model snapshot, minimum relevant effect `δ`, comparator set, task/repository/attempt counts, confidence procedure, analysis code, and spend.

Required comparators are:

1. solo;
2. solo with reflection;
3. equal-budget best-of-N with its selector; and
4. a fixed planner → implementer → reviewer team.

The primary comparator is the required baseline with the highest repository-balanced pilot success estimate. Exact ties break by the stable order shown above. It is selected in Stage 2 and never from lockbox results. The headline H1 rule nevertheless applies to all four comparators.

### H2 — search effect

> At equal total search cost, archive/evolutionary search discovers stronger institutions more reliably than random search or a frozen manual candidate slate.

**Status: Exploratory by frozen default.** One search campaign cannot support a reliability claim. H2 may become confirmatory only in the Stage 2 revision, before search outcomes exist, if funding covers a preregistered number of complete campaigns and a power analysis over campaign-level best-at-budget and area-under-best-so-far results. Otherwise every H2 result is labeled exploratory.

H1 can be positive while H2 is unsupported, and vice versa.

## 3. Owner decisions required before Stage 0 exit

No silent fallback applies to these decisions.

| ID | Decision | Required record |
|---|---|---|
| O-01 | Worker model | Provider, immutable model/snapshot identifier, release date, requested and returned model fields, sampling parameters, API/adapter versions, available revision/fingerprint, and mismatch rule |
| O-02 | Spend | Separate ceilings for pilot/baseline tuning, Stage 4 search-fidelity calibration, each search arm and complete-search seed, confirmation, and any unconditional replication; include a contingency reserve |
| O-03 | Minimum relevant effect | `δ`, an absolute percentage-point improvement in repository-balanced single-attempt success that is worth the coordination and search cost |
| O-04 | Evidence/publication level | Internal learning, public technical report, or academic submission; name artifact-release, independent review, and power obligations |

**Recommended but nonbinding choices:** use one immutable model snapshot; fund H1 first and leave H2 exploratory; choose a public technical report; and choose `δ` before seeing baseline outcomes.

If the selected model has no immutable revision or reliable mismatch signal, the owner must either choose another model or explicitly downgrade the evidence level. A provider alias alone is not an immutable model.

## 4. Frozen experimental defaults

- One worker model snapshot and one coding task type are used throughout an H1 campaign.
- Structural genome mutations and prompt mutations are separate treatments. The first H1 champion comes from structural search unless the Stage 2 revision explicitly labels a factorial prompt × structure experiment.
- The objective evaluator is primary. No model judge contributes to Experiment 0001 fitness or confirmation.
- All required baselines are tuned only on adaptive development data, then versioned and frozen.
- Each search arm starts from the same initial archive and shares the genome grammar, mutation inventory, task/attempt blocks, evaluator, and racing machinery. Only the search component under test differs.
- Every proposed candidate, invalid mutation, failure, retry, lineage edge, and cost remains in the evidence record.
- Post-selection ablations are explanatory. They cannot replace the champion or suppress a valid confirmation run.
- Public benchmark results are compatibility evidence, not the primary H1 result.

## 5. Estimand, endpoint, and decision classes

For system `s`, the confirmatory estimand is:

```text
theta_s = mean over target repositories(
            mean over sampled tasks in repository(
              probability one organization attempt resolves the task under cap B
            )
          )
```

Repositories receive equal headline weight; tasks receive equal weight within repository. Repeated attempts estimate execution randomness and are not independent tasks. Every method uses the same task and attempt blocks, with execution order randomized within those blocks.

For comparator `j`, `Δ_j = theta_champion - theta_j`. The primary endpoint is paired pass-at-budget difference. Secondary cost, latency, reliability, and coordination metrics are descriptive unless the Stage 2 revision names another hypothesis family and correction.

The Stage 2 revision freezes a repository-clustered or hierarchical paired analysis and a simultaneous one-sided confidence procedure across the four required comparisons.

Outcomes are classified before exploratory slicing:

- **Positive:** every simultaneous lower bound for `Δ_j` exceeds `δ`, every cap is respected, and no integrity violation occurred.
- **Negative:** the upper bound against the primary comparator is at or below `δ`, or an upper bound at or below zero rules out superiority to any required comparator.
- **Equivocal:** a valid result meets neither the positive nor negative rule. More evidence requires the same frozen comparison on new data; the champion is not tuned.
- **Invalid:** leakage, an undeclared intervention, model mismatch, evaluator fault, unaccounted cap violation, or other integrity failure compromises the comparison. Repair requires a fresh lockbox.

Transfer is a later H6 question. A positive H1 result does not become negative merely because a later transfer test fails.

## 6. Task partitions and permitted use

Stage 0 freezes eligibility, construction, QA, exclusion, fingerprinting, and escrow—not hundreds of future task IDs.

| Partition | Default size/source | Permitted use | Evidentiary role |
|---|---|---|---|
| `fixture` | 20–50 repository-owned deterministic faults | Constantly visible; harness, replay, security, and evaluator tests | None |
| `compatibility` | One fixed, hashed 10–20-task official public benchmark slice | Adapter comparison only | Legacy reference |
| `search_train` | 120 newly generated, verified tasks across at least 12 repositories | Pilot, factor screen, mutation, racing, promotion, and diagnostics | Adaptive/exploratory |
| `development_selection` | 48 tasks across at least six repositories from a disjoint repository group | Each search arm submits its frozen finalist slate; opened once | Champion selection only |
| `confirmation_lockbox` | Fresh real tasks from the deterministic design in Section 8, normally 10–15+ repositories | One frozen paired campaign | Primary H1 evidence |
| `transfer` | Separately sourced repository/time/domain shift | Opened only under a later protocol | H6 evidence |

Repository and time groups are disjoint. Near duplicates and faults in the same code region remain in one partition. Search scores, including validation-like checkpoint scores, are search data. `development_selection` supplies no confidence claim.

Before inclusion, every generated or mined task must have:

1. three consecutive base failures on the intended hidden test;
2. three consecutive gold-patch passes on hidden and regression suites;
3. prompt/test alignment review blinded to Guildmind outputs;
4. frozen exclusions before the relevant sealed opening;
5. no solution-bearing IDs, URLs, remotes, metadata, or future repository history in the worker bundle; and
6. task, source revision, worker image, grader image, evaluator, worker bundle, and grader bundle hashes.

Both issue and solution must postdate the chosen model snapshot for primary freshness evidence. Otherwise choose an older model, wait for new tasks, or downgrade the result before outcomes are seen.

## 7. Aggregate-budget and failure semantics

One organization/task attempt receives one aggregate cap `B`, shared by every role, subattempt, selector, retry, and tool invocation. Best-of-N does not receive `N × B`.

The Stage 2 revision gives hard limits for model calls; uncached, cached, output, and exposed reasoning tokens; aggregate tool CPU; organization wall time; writable bytes; processes; output bytes; and retry count. It also records provider-reported billable units and a cost estimate using a frozen price-table hash. Invoice reconciliation is separate from the equality constraint.

Before concurrent work starts, the ledger reserves a conservative maximum debit. It refuses work that the remaining cap cannot cover, reconciles the reservation from returned usage, cancels in flight work only on a best-effort basis, and starts no new work after exhaustion. Unknown final usage receives the frozen conservative debit. Hidden SDK retries are disabled.

- Budget exhaustion, malformed output, unsafe patch, worker exception, candidate-caused timeout, and test failure count as unresolved outcomes.
- Every started attempt remains recorded, including spend before failure.
- A provider request accepted without a committed response is `ambiguous`; it may be retried only by the frozen counted-retry rule, and possible duplicate spend remains visible.
- A machine-classified infrastructure fault independent of treatment may trigger at most one complete paired-block retry. The original block remains in reliability and spend reports. No selective method-only rerun is allowed.
- If more than 5% of intended confirmation blocks remain missing, an infrastructure fault is materially differential, or failure classification cannot be reconstructed, the comparison is invalid rather than silently imputed.

Search has its own cap. It includes factor-screen evaluations, proposer calls, invalid proposals, all candidate-task attempts, declared retries, and a separately reported cap on human time used for the manual slate. Search budgets are stated per arm and per complete-search seed; no search method receives unreported free proposal effort.

## 8. Deterministic pilot-to-protocol rule

### 8.1 Stage 2 baseline pilot

After O-01 through O-04 are approved, run the four required baselines on a fixed repository-stratified prefix of 40 `search_train` tasks from at least five repositories, with three paired attempts per method. The task order is derived from `SHA-256(protocol_hash || task_fingerprint)`; attempt IDs and execution order are generated from the same committed seed schedule.

The pilot estimates infrastructure-failure rate, provider cost distribution, baseline success, paired discordance, within-task execution noise, repository heterogeneity, and success-versus-budget curves. If every required baseline is below 10% or above 90% success, or normal infrastructure failure exceeds 1%, the model/task/harness combination fails the pilot. Revise before search; do not manufacture a sample size from an uninformative pilot.

### 8.2 Confirmation size

The Stage 2 analysis program must, using a seed derived from the pilot protocol hash:

1. fit the declared paired hierarchical data-generating model to pilot outcomes;
2. construct a nuisance grid from the point estimate and endpoints of the pilot 95% intervals for baseline rate, paired discordance, within-task noise, and repository heterogeneity;
3. evaluate every feasible design with repositories in `{10, 12, 15, 18, 20}`, tasks per repository in `{8, 10, 12, 15, 20}`, and paired attempts in `{3, 4, 5}`, subject to the confirmation spend ceiling and no more than 20% of tasks from one repository;
4. run at least 10,000 simulated campaigns at every nuisance-grid point using the exact planned analysis; and
5. retain only designs that reach the publication-level power target both when every true `Δ_j = 2δ` and when the primary true difference is zero.

The frozen power targets are 80% for internal/public-technical evidence and 90% for academic evidence. Choose the retained design with the fewest organization-task attempts; ties prefer more repositories, then fewer tasks per repository, then fewer repeats. If no design qualifies within spend, the Stage 2 gate fails: reopen O-02, O-03, or O-04 through a reviewed pre-search revision, or stop. Do not reduce the standard after observing candidate results.

The exact task IDs may be created later, but their eligibility rule, repository allocation, attempt schedule, and escrow procedure are fixed by the resulting revision.

### 8.3 Search fidelity and rungs

The default campaign shape is 48 candidates → 12 → 4, followed by two frozen finalists per search arm on `development_selection`. Candidate counts may not be silently reduced to fit spend. Stage 2 sets provisional task panels of 16 → 40 → 120.

On a Stage 4 calibration slate frozen before scoring and containing at least 16 structurally varied valid genomes, evaluate nested repository-stratified panels from `{16, 24, 32, 40, 60, 80, 120}` `search_train` tasks. A cheap rung qualifies only if, against the 120-task panel, it has:

- Spearman rank correlation at least 0.60;
- at least 80% recall of full-panel top-quartile candidates; and
- at most 10% false elimination of candidates whose full-panel gain over the frozen primary baseline exceeds `δ`.

Set rung 1 to the smallest qualifying value in `{16, 24, 32, 40, 60, 80}`, or 120 if none qualifies. Set rung 2 to the smallest qualifying value in `{40, 60, 80}` that exceeds rung 1, or 120 if none qualifies. Rung 3 is 120. Each rung is a separately seeded block of the stated size; earlier results remain evidence but are not silently counted as later-rung attempts. Thus the authorized attempt count is `48 × rung_1 + 12 × rung_2 + 4 × 120` per arm and complete-search seed, before finalist selection. The Stage 4 revision may only retain or enlarge the Stage 2 provisional panels according to this rule. If the resulting cost exceeds O-02, do not run search.

### 8.4 Champion selection

Each arm nominates exactly two finalists before `development_selection` opens. Evaluate every finalist and required baseline on all 48 tasks with three paired attempts in one sealed opening. Exclude only candidates with a preregistered capability, integrity, or aggregate-cap violation. A finalist clears the development gate only if its repository-balanced point-estimate gain over the frozen primary comparator is at least `δ`. If none clears, Experiment 0001 stops before confirmation. Rank qualifying finalists by repository-balanced success; exact ties break by lower total tokens, then lower organization wall time, then lexicographically by genome hash. Freeze the winner, runtime, prompts, model, cap, and analysis before lockbox access. No mutation, replacement, or fallback follows the opening.

## 9. Lockbox procedure

The corpus custodian must not be able to mutate candidates or unilaterally authorize an opening. The search operator cannot list lockbox task IDs, repositories, prompts, tests, gold patches, or per-task metadata.

Before opening, two named reviewers verify and content-hash:

- champion and required baseline artifacts;
- model and runtime/environment identities;
- aggregate cap, task/attempt blocks, retry rule, and execution randomization;
- the escrowed task-manifest commitment;
- analysis code, confidence and classification rules, exclusions, and report template; and
- reserved confirmation and unconditional-replication spend.

The custodian records the time, people, purpose, manifest hash, and files disclosed in an append-only access log. Worker bundles go only to the scheduler; grader bundles go only to the separate evaluator. A pre-freeze access, partial trial run, unlogged disclosure, or use of lockbox feedback to change treatment is an integrity failure.

Once opened, the campaign is completed and reported regardless of direction. The lockbox is then published as allowed by O-04 or retired permanently. It never becomes development data for Experiment 0001.

## 10. Required evidence package

The final package contains the approved contract and revisions; task and split commitments; candidate/baseline genomes and prompts; model, API, environment, image, evaluator, and price-table identities; all manifests, terminal states, events, budgets, artifacts, patches, evaluations, retries, failures, and lineage; analysis code and outputs; exploratory slices clearly labeled; and the lockbox access log. Restricted artifacts receive hashes, provenance, and a documented access route rather than disappearing from the manifest.

## 11. Stage 0 exit checklist

Stage 0 passes only when every box is checked:

- [ ] O-01 through O-04 are completed and signed by the research and systems owners.
- [ ] H1 is the sole planned confirmatory claim; H2 is explicitly exploratory or separately funded and powered.
- [ ] Task eligibility, split roles, QA, freshness, repository separation, fingerprinting, and corpus-custodian responsibilities are assigned.
- [ ] Aggregate caps, reservation, exhaustion, retry, ambiguity, missing-block, and infrastructure-failure rules are accepted.
- [ ] The pilot-to-confirmation and pilot-to-rung programs can be implemented without discretionary branches.
- [ ] Champion nomination and one-shot development-selection rules are accepted.
- [ ] Lockbox storage, two-person opening, access log, retirement, and invalidation procedures are assigned and testable.
- [ ] [`docs/threat-model.md`](../threat-model.md) has owners and planned tests for every critical control.
- [ ] [ADR 0001](../decisions/0001-python-and-uv.md), [ADR 0002](../decisions/0002-sqlite-and-content-addressed-artifacts.md), and [ADR 0003](../decisions/0003-sandbox-and-evaluator-boundary.md) are accepted.
- [ ] A 10–20-call cost/latency probe demonstrates that the approved pilot can fit O-02.
- [ ] This file and referenced manifests have content hashes recorded in a signed Stage 0 approval record.

Passing this checklist authorizes the deterministic measurement substrate. It does not authorize organizational search or claim that final sample sizes are already known.
