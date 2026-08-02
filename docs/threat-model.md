# Guildmind Threat Model

**Status:** Stage 0 design; controls are requirements, not claims of implementation<br>
**Date:** 2026-07-31<br>
**Last implementation review:** 2026-08-01<br>
**Applies to:** Experiment 0001 worker execution, search, evidence storage, and sealed evaluation

## Current implementation evidence

The [2026-08-01 Stage 1 hardening checkpoint](reviews/2026-08-01-stage-1-hardening-gate.md) partially implements the container contract, an image-owned two-phase evaluator, and crash-to-terminal recovery. A development smoke on rootful ARM Docker Desktop exercised the declared restrictions and produced the expected gold-fixture result. That smoke is not authoritative hostile-code containment evidence.

The Stage 1 security gate is **not passed**. Evaluator v2 closes the known shared-interpreter defect for the first JSON-callable fixture: the candidate request mounts only the patched workspace and an expected-value-free challenge; the separate scorer mounts only the challenge, sealed oracle, and exact bounded candidate response. Fixture input bytes are frozen before model dispatch. Only scorer output can become a verdict, and the completion binds the frozen task/source, patch, challenge, response, oracle, image, evaluator, protocol, expected count, and limits while the content-hashed result retains validated observed counts. Exact candidate/scorer transcripts are named content-addressed evidence. A strict [19-case adversarial corpus](adversarial-corpus.md) now content-addresses nine pre-application unsafe shapes plus functional controls, the former `unittest` tampering case, grader access, completion forgery, empty response, timeout, output exhaustion, and OOM. Every predeclared outcome passes in development; the execution cases still require reference-host repetition.

T-03 through T-06, T-10, T-11, and T-13 still lack the required reference-host adversarial and recovery matrix on dedicated rootless x86_64 Linux. The v2 protocol is not a generic pytest boundary, candidate code necessarily sees call inputs, repeated evaluation can leak oracle information, containers share a kernel, and a bounded writable filesystem still needs a demonstrated hard byte quota. A configured Docker option is not proof of enforcement.

External repositories, arbitrary model-generated commands, and provider-backed campaigns remain blocked. The detailed matrix and reference-host procedure live in the checkpoint report; the normative controls below are unchanged.

## 1. Security objectives

Guildmind must make these properties testable:

1. Workers cannot obtain hidden tests, gold patches, future source history, task-specific grader/oracle material, credentials, or sealed-task metadata. Generic evaluator adapter source need not be confidential, but workers cannot modify the trusted copy.
2. Candidate code cannot alter the grader, test command, evidence record, budget authority, or another attempt.
3. Every started action and its known or conservatively estimated spend survives failure.
4. Adaptive search cannot observe `development_selection` before finalist freeze or any `confirmation_lockbox` material before champion freeze.
5. A reported result identifies the exact task, model request/response identity, runtime, image, evaluator, patch, budget, and analysis that produced it.
6. Integrity failures produce an invalid result, not an optimistic score or a selective retry.

This design reduces risk; it does not claim that containers defeat every kernel exploit or that a hosted model is bit-reproducible.

## 2. Actors and trust assumptions

| Actor | Treatment |
|---|---|
| Worker model output, candidate shell commands, repository code, and submitted patch | Untrusted and potentially adversarial |
| Institution/search policy | Adaptive and incentive-driven; not trusted with sealed data |
| Guildmind control plane and evaluator definitions | Trusted code, versioned and reviewed |
| Research operator | Honest but fallible and vulnerable to confirmation bias |
| Corpus custodian | Trusted for escrow, but cannot mutate candidates or unilaterally authorize opening |
| Hosted model provider | Trusted to serve its API and usage report; stochasticity, drift, retention, and outages remain threats |
| Reference Linux host administrator | Trusted. Host compromise is outside the initial containment claim and must invalidate affected campaigns |

No one person both changes candidates and unilaterally controls the confirmation lockbox.

## 3. Trust zones and data flow

| Zone | May contain | Must not contain |
|---|---|---|
| Lockbox escrow | Task manifest, worker/grader bundles, hidden tests, gold/QA data | Candidate outputs, search database, model credentials |
| Research control plane | Approved genomes, visible task bundle, model credentials, scheduler, budget ledger, artifact references | Grader bundle before an authorized evaluation |
| Worker sandbox | Randomized task alias, prompt, sanitized repository snapshot, visible tests, bounded workspace | Network, credentials, Docker socket, hidden tests, evaluator code, gold data, future history, search/lockbox indexes |
| Candidate invoke sandbox | Fresh patched repository and sanitized call challenge without expected outcomes | Grader/oracle, expected values, model credentials, search state, evidence store, network, Docker socket |
| Trusted scorer sandbox | Immutable oracle, sanitized challenge, exact bounded candidate response | Candidate workspace or code, model credentials, search state, writable grader definition, network, Docker socket |
| Evidence store | Immutable artifacts, transactional events/ledger, hashes, access log | Undeclared mutable replacements for committed evidence |

Model requests are brokered by the trusted control plane. The worker sandbox never receives provider credentials. A worker yields a patch as untrusted data; the evaluator validates and applies it to a fresh repository before running a fixed test command.

## 4. Assets

Critical assets are lockbox membership and metadata; hidden tests and gold patches; model and infrastructure credentials; corpus source history; evaluator definitions; candidate and baseline identities; aggregate caps and usage; task/split fingerprints; append-only events and terminal manifests; submitted patches; analysis code; and the lockbox access log.

## 5. Threat and control register

“Required test” names the latest gate by which the control must be demonstrated.

| ID | Threat | Required control | Required test / response |
|---|---|---|---|
| T-01 | Hidden tests, gold data, or sealed metadata reach a worker | Physically separate worker/grader bundles; allowlist bundle contents; never mount escrow in the control or worker path | Stage 1 planted-secret fixture; any disclosure invalidates affected evidence |
| T-02 | Future solution survives in Git history | Prefer export without `.git`; otherwise reconstruct only allowed objects and remove remotes | Search for planted solution in refs, packed refs, tags, reflogs, alternates, and unreachable objects before external tasks |
| T-03 | Worker retrieves solutions or exfiltrates data over a network | Docker `none` network, offline prebuilt dependencies, no proxy variables or host service route | Worker DNS/TCP/Unix-socket escape suite fails closed |
| T-04 | Worker steals credentials or controls Docker/host | No secrets or Docker socket in sandbox; non-root UID; rootless daemon; read-only base; dropped capabilities; `no-new-privileges`; seccomp | Probe host paths, environment, metadata endpoints, daemon sockets, and privilege escalation |
| T-05 | Candidate escapes resource limits or multiplies them with more agents | cgroup v2 CPU/memory/PID limits, real writable quota, output/wall caps applied to the whole organization attempt | `doctor` refuses authoritative runs when enforcement is absent; fork/disk/output bombs terminate within caps |
| T-06 | Candidate alters or evades the evaluator | Freeze task/source/grader bytes before dispatch; fresh candidate and scorer sandboxes; immutable out-of-tree grader and fixed commands; disjoint mount allowlists; candidate output treated only as hostile observations; scorer never imports candidate code; exact transcript evidence; no network/credentials; resource limits equivalent to worker containment | No-op, visible-test-only, same-interpreter tampering, grader-read, marker-forgery, malformed-response, unsafe, timeout, and gold patches must produce expected discrimination. Initial v2 development attacks pass, but the complete reference-host matrix is open. |
| T-07 | Malicious patch writes outside the repository or changes grader inputs | Parse before apply; reject traversal, absolute/unexpected paths, symlink and submodule mode changes, oversize/binary payloads, and grader/test paths | The nine-case [patch-intake matrix](evidence/patch-intake/2026-08-02-development/README.md) covers absolute/container/grader targets, traversal, symlink, submodule, binary/decompression claims, oversize, and file-count attacks before Git or sandbox dispatch |
| T-08 | Candidate injects a forged score or artifact reference | Evaluator creates results from committed task/patch/image/evaluator hashes; workers cannot write evidence DB; one control-plane writer validates state transitions | Cross-record hash/state tests; mismatched or missing references fail closed |
| T-09 | More agents spend more than the control | Reserve worst-case debit before dispatch; aggregate caps; disable hidden retries; conservative unknown-usage debit | Concurrent exhaustion and crash tests prove no new work starts beyond cap |
| T-10 | Crash creates a free or duplicated provider call | Persist `request_started` with idempotency key before dispatch; classify accepted/uncommitted calls `ambiguous`; allow no duplicate committed result | Kill at every dispatch/commit boundary; retain possible duplicate spend |
| T-11 | Selective retries or discarded failures inflate results | Frozen retry policy; terminal manifest for every start; complete paired-block rerun only for machine-classified infrastructure faults | Reconcile planned blocks to terminal states; unexplained absence invalidates comparison |
| T-12 | Provider alias or behavior drifts during campaign | Record requested/returned IDs, provider revision/fingerprint, adapter/API versions, sampling settings, and frozen canary outputs; predeclare mismatch rule | Canary before and during campaign; mismatch aborts or invalidates as contracted |
| T-13 | Dependency, base image, or framework drift changes treatment | Lock dependencies; preserve exact OCI digests; record kernel/architecture/runtime; capture raw model request/response | Operational rerun uses preserved digest; rebuilding is reported separately as supply-chain validation |
| T-14 | Task duplication, public contamination, or identifying strings leak answers | Repository/time-disjoint splits, near-duplicate grouping, freshness rule, randomized aliases, solution-bearing metadata removal | Similarity and provenance audit before split freeze; failures retire affected tasks before opening |
| T-15 | Search memorizes task-specific content in genomes or memory | Typed bounded genome; prompt/memory size caps; task-token and identifier audit; no lockbox access | Audit every finalist before `development_selection` and champion before confirmation |
| T-16 | Repeated validation/lockbox inspection becomes training feedback | All promotion on `search_train`; one-shot finalist evaluation; encrypted/separate escrow; two-person opening and append-only access log | Search process cannot enumerate sealed tasks; unauthorized access retires split |
| T-17 | Post-hoc exclusions, analyses, or comparator choice manufacture a result | Content-hash champion, comparators, manifests, exclusions, analysis code, report template, and decision rules before opening | Reproduce report from frozen inputs; undeclared branch is reported and confirmatory result invalid |
| T-18 | Artifact or event evidence is overwritten | Content-addressed immutable blobs; atomic write-before-reference; transactional event/ledger state; backups and read verification | Corruption, orphan, partial-write, backup/restore, and reference-reconciliation tests |
| T-19 | Provider retains private tasks or uses them for training | Select an appropriate data-retention contract/setting; send no gold/hidden material; record provider policy with campaign | Owner accepts residual risk before corpus use; policy mismatch blocks confirmation |
| T-20 | Worker and future judge populations share selection state or collude on a proxy | No judges in Experiment 0001; later judge data, lineages, providers, and epochs remain separate; objective tests retain veto | A later judge-specific protocol is required before any subjective score affects selection |

Content hashes establish identity and reveal changes; they are not signatures. Authorization depends on the trusted approval/access system and named reviewers.

Unkeyed commitments to a low-entropy oracle can also enable offline guessing. Before adaptive search, optimizer-facing feedback must exclude raw oracle commitments or replace them with a keyed/access-controlled commitment while preserving trusted audit evidence.

## 6. Lockbox-specific controls

The corpus custodian stores the lockbox outside developer search paths and creates a commitment to its manifest. Search tooling receives neither the manifest nor an enumerable index. Before opening, two reviewers verify the frozen champion, baselines, model, runtime, caps, task/attempt schedule, analysis, exclusions, spend, and manifest commitment.

Every access record includes actor, time, purpose, disclosed objects, and before/after hashes. An early or partial opening retires the split. After an authorized opening, the frozen campaign is completed and reported or the lockbox is retired; its result may not be used to tune Experiment 0001. The detailed procedure is in [Experiment 0001](experiments/0001-institutional-search.md#9-lockbox-procedure).

## 7. Incident and integrity response

On a suspected integrity event:

1. stop new dispatches without deleting or rewriting evidence;
2. preserve host, access, event, budget, provider, and artifact records;
3. identify the earliest affected action and every downstream result;
4. classify worker-caused failures as outcomes and control/evaluator/leakage failures as integrity incidents; and
5. publish the classification and retire any opened sealed data.

An incident is **invalidating** when it could change treatment, task exposure, budget equality, outcome measurement, inclusion, or analysis. The affected comparison is rerun only after repair on fresh sealed data. “The direction probably would not change” is not a recovery rule.

## 8. Gate ownership

Default control ownership is: corpus custodian for T-01, T-02, T-14, and T-16; systems owner for T-03 through T-10, T-13, and T-18; and research/evaluation owner for T-11, T-12, T-15, T-17, T-19, and T-20. T-12, T-16, and T-17 also require the second owner's review. Stage 0 replaces role labels with named people.

- **Stage 0:** assign an owner and test stage for every threat; resolve provider retention and lockbox custody.
- **Stage 1:** pass fixture, sandbox, evaluator, budget, crash, and evidence-integrity attacks.
- **Stage 2:** pass provider identity/canary, cost reconciliation, and normal-failure thresholds.
- **Before Stage 5:** freeze and audit search/development bundles and finalist-content checks.
- **Before Stage 6:** rerun the full boundary suite on the exact campaign images and complete a two-person lockbox-opening rehearsal using dummy data.

No failed critical control may be waived after seeing capability results.
