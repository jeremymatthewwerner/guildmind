# Hybrid evolution and reinforcement learning

**Status:** Future Stage 8 design note<br>
**Depends on:** A positive institutional effect (H1); either a positive persistence effect (H3) or the reviewed stateless negative path below; and machine-gradeable sequential environments<br>
**Does not change:** Experiment 0001, Genome v0, or the requirement that the first worker model snapshot remains fixed

## 1. Purpose

Guildmind should eventually test two distinct improvement mechanisms together:

1. **Evolution searches broadly** across populations of institutional structures and restricted executable policies.
2. **Reinforcement learning corrects locally** by updating a sequential controller from experience inside one compatible structure.

Selection alone is not reinforcement learning. Stage 8 counts as proper RL only when a parameterized policy takes sequential actions, receives a versioned reward stream, estimates return or advantage, updates from trajectories, and is later evaluated as a frozen checkpoint.

The evolutionary population is a population of candidate institutions. Each candidate instantiates a society of worker roles for an episode, so agent populations are part of the phenotype being evaluated, but the initial unit of selection is the whole institution rather than an independently breeding worker model. The executable-program component is the institution's bounded coordination algorithm; the RL component learns how that algorithm should act as institutional state changes.

The intended claim is deliberately narrower than “agents rewrite themselves”:

> At an equal total training and execution budget, a hybrid of institutional evolution and RL-trained control adapts faster and produces a better frozen policy on unseen task streams than evolution alone or RL alone.

The default H4 campaign begins only after H3 passes the Stage 7 gate. If H3 is validly negative, a reviewed protocol may still test H4 with `s = ∅`, provided it removes every persistence/inherited-state claim, retains a matched no-persistence control, and explains why the sequential control surface remains scientifically useful. An equivocal or invalid H3 result is not a negative-path authorization. Stage 8A may be built earlier as an engineering testbed, but it supplies no H4 evidence by itself.

## 2. What is inherited

The future phenotype is `(g, p, θ, s)`:

| Component | Meaning | Changed by evolution | Changed by RL |
|---|---|---:|---:|
| `g` | Organizational constraint genome: role slots, capabilities, communication/memory access, hard budgets/lifecycle caps, governance invariants, and state-dependent legal-action set `A_g` | Yes | No |
| `p` | Restricted executable policy program that chooses within `A_g`: typed decision table, finite-state policy, behavior tree, or typed expression graph | Yes | No |
| `θ` | Controller parameter checkpoint; `p` and the compatibility signature identify its architecture and recurrent-state schema | Compatible checkpoint inheritance only | Yes |
| `s` | Provenance-backed reputation, certification, and institutional memory | Policy governing it may evolve | Updated only through declared environment transitions, not gradients |

Task-specific solution code is not inherited. The worker foundation model remains fixed in the first hybrid campaign. Evolving arbitrary Python, shell programs, evaluator logic, model providers, or search code remains out of scope.

Genome v0 deliberately keeps constraints and deterministic workflow rules in one artifact. Stage 8 introduces a versioned migration that factors those semantics into `g` and `p`; conformance fixtures must show that the migrated pair reproduces the v0 event trace before either component may mutate. In the factored representation, `g` never chooses among simultaneously legal actions. Program `p` is the real compiled algorithm for that choice, but its grammar contains only safe, bounded operations exposed by the Guildmind runtime.

### Evidence identity

The future `Candidate` identity binds all four phenotype components, the `PolicyCompatibilitySignature`, the content-hashed `RewardSpec`, declared learning hyperparameters, and two distinct lineage edge sets:

1. structure/program parentage for mutations to `g` or `p`; and
2. controller-checkpoint parentage for initialization, gradient updates, exploit/copy, migration, or distillation of `θ`.

Every Stage 8 `RunManifest` binds that candidate identity plus the exact `g`, `p`, `θ`, and institutional-state snapshot `s` used for the attempt, the reward and compatibility identities, both lineage heads, the training campaign/partition, and the usual task/model/environment/seed identities. Any change creates a new content identity; mutable aliases such as “current policy” or “best checkpoint” are not evidence. Reward components must reconstruct under the bound `RewardSpec`, and the two lineage DAGs must remain independently traversable.

## 3. The RL problem

For each constraint genome `g` and compiled policy program `p`, the controller implements:

```text
π_{p,θ}(action_t | observation_t, genome_g, state_s_t)
where action_t ∈ A_g(observation_t, state_s_t)
```

### Observation

The first observation schema should contain only legitimate institutional state:

- active, waiting, completed, and failed roles;
- queued proposals, reviews, disputes, and handoffs;
- scoped message summaries or embeddings with provenance;
- public test outcomes and tool results available to the workers;
- remaining shared and per-role budgets;
- uncertainty-aware reputation and certification state;
- allowed memory-retrieval summaries;
- previous institutional action and elapsed rounds/time; and
- the legal-action mask compiled from `g`.

It must exclude hidden tests, gold solutions, future repository history, confirmation identities, evaluator internals, and judge-only information.

### Action

Program `p` and its controller may choose only declared institutional operations:

- activate, suspend, or revisit a role;
- route a message or proposal along an allowed edge;
- allocate or reclaim a bounded budget slice;
- request independent review;
- retrieve an allowed memory item;
- escalate or resolve a dispute;
- accept or reject a proposal; and
- terminate and submit the current artifact.

The runtime independently masks and rejects any action outside `A_g`; a bug or adversarial output in `p` is not authority. Workers still create the task artifact. The controller coordinates them; it cannot directly open an undeclared shell, add a tool, alter the evaluator, or invoke a different model.

### Transition and episode

One action advances the authoritative Guildmind state machine by one logical step. Every step records the `g`/`p` and compatibility identities, observation/action schema hashes, legal-action mask, behavior-policy checkpoint, action probability or equivalent audit metadata, resulting events, `RewardSpec` and reward components, and termination or truncation reason.

One task attempt is one episode. Multiple tasks form an RL training stream. Training, population selection, frozen-policy holdout, and transfer use separate task/repository partitions.

### Reward

Start with a sparse, objective reward:

```text
terminal reward
  = task success
  - declared model/tool/latency cost
  - invalid-action and policy-violation penalties
```

Costs are real ledger debits, not inferred notions of “good coordination.” Dense shaping is allowed only for externally visible costs or invalid actions. It may not disclose hidden-test progress, solution similarity, or post-hoc judge preferences. The complete `RewardSpec`, coefficients, normalization, discount, and truncation semantics are content-hashed before training.

Judge reward is prohibited until the independent judge stage has cleared its human-calibration and optimizer-facing shadow gates.

### First learner

Use an action-masked recurrent PPO implementation as the first reference learner, not as a permanent architectural dependency. The initial institutional action space is discrete, and recurrent state makes partial observability explicit. PPO limits long-lived replay, but it still intentionally reuses one collected batch for multiple minibatch update epochs; “on-policy” does not make structural compatibility automatic. At collection, each trajectory is bound to the exact `g`, `p`, compatibility signature, `RewardSpec`, action masks, and behavior checkpoint. Immediately before every update, the learner rechecks those identities and rejects the batch if policy structure, action/observation meaning, reward semantics, or behavior provenance changed. The exact PPO implementation and update bounds must be frozen because those details materially affect results.

If expensive model-backed episodes make PPO too sample-inefficient, that is an experimental result. Offline warm-starting, actor–critic replay, or another learner must be a separately declared arm rather than a quiet methodology change.

## 4. Hybrid population loop

The hybrid follows population-based training inside the existing quality-diversity archive:

```text
initialize population of (g, p, θ, s)

for each frozen training epoch:
    verify and record collection-time compatibility
    collect capped trajectories on the shared task/seed block
    recheck compatibility and provenance at update time
    reject stale batches; otherwise update θ with the frozen RL algorithm
    evaluate frozen checkpoints on the selection block
    update Pareto / quality-diversity archives
    exploit: copy a qualified compatible checkpoint
    explore: apply one typed genome, policy-program, or learner mutation
    record structure/program lineage and controller-checkpoint lineage

freeze finalists
evaluate once on untouched hybrid holdout tasks
```

Evolution provides divergent exploration and preserves stepping stones. RL provides directed correction within differentiable controller parameters. Population-based exploit/explore permits successful controller state and hyperparameter schedules to propagate without collapsing every candidate into one lineage.

## 5. Compatibility and inheritance

Every compiled `(g, p)` pair has a `PolicyCompatibilitySignature` containing:

- constraint-genome and policy-program schema/semantic versions;
- observation schema and feature ordering;
- legal action IDs and masking semantics;
- maximum role slots and role encoding;
- recurrent-state shape;
- controller architecture and parameter layout; and
- reward and normalization version.

The signature is checked before trajectory collection, written into every transition and episode, and checked again immediately before each optimization update. Structural/program mutations happen only after the current update batch is consumed or discarded; a newly compatible-looking child may not inherit the parent's trajectories merely because tensor shapes match.

Compatible structural mutations may inherit `θ`. Incompatible mutations must do one of the following according to a rule frozen before the campaign:

1. reinitialize the controller;
2. inherit only a named compatible submodule; or
3. use a separately measured migration/distillation procedure.

Silently loading weights into a changed meaning is an integrity violation.

The experiment should compare two inheritance semantics:

- **Baldwinian:** learning helps candidate fitness, but offspring inherit structure with a fresh controller;
- **Lamarckian/PBT:** compatible offspring inherit the learned checkpoint as well as structure.

## 6. Development ladder

### 8A — Cheap verifiable environments

Use deterministic synthetic tasks plus one PettingZoo AEC and one Parallel environment. Establish that:

- trajectories replay exactly from seeds and checkpoints;
- illegal actions are masked and rejected fail-closed;
- termination and budget truncation are distinct;
- reward components reconstruct from the event ledger;
- RL improves a frozen policy over a non-learning controller; and
- population/checkpoint restore does not alter results.

No hosted worker-model calls are needed at this rung.

### 8B — Learned institutional control

Around a fixed worker model, train only high-level choices such as routing, review depth, dispute escalation, memory retrieval, and remaining-budget allocation. Keep task generation, evaluator, worker prompts, tools, and aggregate caps fixed across arms.

### 8C — Optional role adaptation

Only after 8B succeeds, test small trainable role adapters or a dedicated policy model. This changes the agent substrate and therefore requires a separate treatment, baseline, compute ledger, checkpoint policy, and transfer test. It must not be folded into the headline controller result.

## 7. Required experimental arms

At minimum compare:

1. expert-designed fixed institution and controller;
2. equal-budget random typed mutation;
3. evolution-only structure/program search with no policy updates;
4. RL-only controller training inside one fixed expert genome;
5. Baldwinian hybrid;
6. Lamarckian/PBT hybrid; and
7. removal ablations for structural mutation, gradient updates, checkpoint inheritance, reward-cost penalties, archive diversity, and—on the H3-positive path—persistent state.

All arms receive the same environment/task blocks, worker model, legal action surface, task information, reward function, seed schedule, and total maximum spend.

## 8. Fair accounting and estimands

The total budget must include:

- environment episodes and steps;
- worker model calls and token classes;
- controller inference;
- policy/value optimization FLOPs or measured accelerator time;
- population/search scheduling;
- replay and checkpoint storage;
- evaluation attempts and declared retries; and
- human intervention.

The primary estimands are:

1. frozen-policy held-out return or pass-at-budget after a fixed total training spend; and
2. area under the held-out adaptation curve versus total spend after a distribution shift.

Secondary metrics include regret, sample efficiency, archive quality/diversity, controller entropy, checkpoint churn, catastrophic forgetting, infrastructure failure, and zero-shot transfer of `g/p` separately from transfer of `θ`.

Use several independent complete campaign seeds. Candidate episodes inside one campaign do not substitute for independent search/training campaigns.

## 9. Data and reward integrity

- Create `rl_train`, `hybrid_selection`, `hybrid_holdout`, and `hybrid_transfer` partitions by repository and time.
- Never write holdout or confirmation trajectories into a replay buffer.
- Store exact `g`/`p`, compatibility, `RewardSpec`, behavior-policy, and task-partition provenance with every transition.
- Reject inherited memory or replay containing task solution text, patches, hidden-test details, or future history.
- Expose aggregate training diagnostics to mutation logic only according to the frozen campaign contract.
- Evaluate finalists with learning disabled and no optimizer state loaded into the worker runtime.
- Retire a holdout if a checkpoint was updated from it, directly or indirectly.

## 10. Failure tests

The stage is incomplete until it can detect:

- reward hacking that raises shaped return while objective success falls;
- illegal-action masking errors;
- off-policy or replay data from an incompatible `g`/`p` pair or `RewardSpec`;
- checkpoint/signature mismatch;
- double-counted hybrid compute;
- controller collapse to one action or one lineage;
- catastrophic forgetting after a task-stream shift;
- inherited task-answer content;
- evaluator or judge information in observations/rewards; and
- nondeterministic restore from the same environment seed and checkpoint.

## 11. Advancement gate

Advance only if the hybrid clears a predeclared minimum relevant margin over evolution-only and RL-only on both primary estimands, using simultaneous lower confidence bounds across independent campaigns. It must also survive an unseen task-stream shift, reward-component ablation, no-inherited-content audit, total-compute reconciliation, and exact checkpoint replay.

The report must name whether it followed the H3-positive or reviewed stateless path. A positive result on the stateless path cannot support a persistence or inherited-memory claim. If H4 fails, retain the better single mechanism. A negative result is evidence about where institutional adaptation does and does not benefit from gradients.

## 12. Decisions to freeze when Stage 8 begins

- Which controller surface has enough delayed consequence to justify RL rather than a contextual bandit?
- What typed policy-program grammar is allowed?
- What observation is sufficient without leaking solution/evaluator state?
- What controller architecture and reference learner are used?
- What reward and cost coefficients represent the actual objective?
- What makes checkpoints compatible across mutations?
- Are Baldwinian and Lamarckian inheritance both affordable?
- Which PettingZoo/synthetic tasks predict the model-backed institutional control problem?
- What total-compute unit permits fair evolution-only, RL-only, and hybrid comparison?
- Does Stage 8C train a controller-only model or any role-specific adapter?

## 13. Primary methodological anchors

- [Population Based Training of Neural Networks](https://arxiv.org/abs/1711.09846) jointly optimizes a population of models and training hyperparameters under a fixed computational budget through exploit/explore steps.
- [Evolution-Guided Policy Gradient in Reinforcement Learning](https://arxiv.org/abs/1805.07917) combines an evolutionary population with a gradient-trained policy and shared experience.
- [Policy Gradient Assisted MAP-Elites](https://doi.org/10.1145/3449639.3459304) pairs divergent genetic variation with directed policy-gradient variation in a quality-diversity archive.
- [Proximal Policy Optimization Algorithms](https://arxiv.org/abs/1707.06347) provides the initial reference on-policy update family.
- [PettingZoo](https://pettingzoo.farama.org/) supplies versioned multi-agent environment semantics for the cheap Stage 8A testbed; it does not become the Guildmind scheduler.

These are design precedents, not dependencies to reproduce wholesale. Guildmind's distinctive object of learning is the institution-level controller around otherwise fixed agents.
