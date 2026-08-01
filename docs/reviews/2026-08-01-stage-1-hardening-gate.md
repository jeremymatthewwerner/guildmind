# 2026-08-01 Stage 1 Hardening Gate

**Verdict:** **NOT PASSED**<br>
**Scope:** Development checkpoint for the sandbox contract, trusted-fixture container evaluator, evidence state machine, and crash-to-terminal recovery<br>
**Reference environment required:** Dedicated rootless x86_64 Linux host<br>
**Environment available for this checkpoint:** Apple Silicon macOS with rootful Docker Desktop

## 1. What this checkpoint establishes

This checkpoint turns several Stage 1 requirements into executable code and contract-level tests. It is evidence that the intended container invocation and failure semantics can be inspected and exercised during development. It is **not** evidence that Guildmind is ready to execute external repositories or arbitrary model-generated commands.

The checkpoint covers three bounded areas:

1. a container sandbox contract that declares the required worker and evaluator restrictions;
2. a disposable, image-owned evaluator path for trusted scripted fixtures, including a preserved adversarial test that demonstrates why it is not yet a hostile-code evaluator; and
3. recovery that converts an interrupted nonterminal local run into one explicit terminal record rather than leaving it silently in flight.

The authoritative Stage 1 gate remains open for two independent reasons. First, the full controls have not been demonstrated on the reference host: Docker Desktop on this Mac uses a rootful daemon inside an ARM Linux virtual machine, not the dedicated rootless x86_64 Linux environment required by [ADR 0003](../decisions/0003-sandbox-and-evaluator-boundary.md). Second, candidate Python is currently imported into the trusted `unittest` interpreter and can read `/inputs/grader`. The checked-in tampering patch replaces `unittest.TextTestRunner.run` and is falsely reported as passing. Moving the same design to the reference host would not repair that trust-boundary failure.

Experiment 0001 remains a draft. This checkpoint does not resolve or approve its worker-model, spend, minimum-effect, or publication-level owner decisions.

## 2. Evidence vocabulary

This report uses the following terms narrowly:

- **Implemented:** code exists for the stated behavior.
- **Contract-tested:** tests inspect or exercise the command, configuration, state transition, or parser behavior. This does not prove host-level containment.
- **Development-smoked:** the behavior ran on the non-reference Docker Desktop environment recorded below.
- **Reference-verified:** the behavior passed on a dedicated rootless x86_64 Linux host with the required controls enabled.

Only **reference-verified** evidence can satisfy the hostile-code portions of the Stage 1 exit gate. A configured Docker flag is not proof that the host kernel or daemon enforced the intended boundary.

## 3. Implemented control surface

| Area | Checkpoint implementation or contract | Evidence level | Still required for the authoritative gate |
|---|---|---|---|
| Request and image identity | Requests require a digest-only image reference, absolute argument-vector command, safe execution ID, bounded limits, fixed environment mapping, and read-only nonoverlapping mounts. Image preflight requires the exact local repository digest, Linux amd64 identity, and no declared volumes; pulling and image health checks are disabled. | Code and 36 mocked Docker-CLI unit tests | Exercise the exact reviewed image through the live reference daemon and account for image-defined ambient environment. |
| Host admission | Strict mode requires Linux x86_64, rootless Docker, cgroup v2, built-in seccomp, private cgroup namespaces, and advertised memory/swap/CPU/PID enforcement. A separately selected development mode may relax only rootless and architecture checks and never reports reference readiness. | Code and 36 mocked Docker-CLI unit tests | Preserve `docker info` evidence, prove the selected endpoint is the controlled daemon, and prove each advertised facility is enforced on the reference host. |
| Container lifecycle | A bounded runner owns `create` → attached `start` → state inspection → forced removal. It disables daemon logging, applies a combined attached-output cap and wall deadline, issues a kill when either is exhausted, derives out-of-memory status from Docker state, and invalidates an otherwise successful result if cleanup fails. | Code, 36 mocked Docker-CLI tests, and live development boundary tests | Exercise kill, descendant-process, daemon-failure, post-process orphan discovery, and cleanup paths on the reference host. |
| Network | Worker/evaluator invocations request Docker's disabled network mode and receive no declared proxy or provider credentials. | Contract-tested | DNS, TCP, Unix-socket, metadata-route, and host-service probes must fail closed on the reference host. |
| Privilege | The contract requests a non-root container user, dropped capabilities, `no-new-privileges`, and a read-only root filesystem. | Contract-tested | Verify effective UID, capability sets, seccomp behavior, mount writability, and privilege-escalation attacks under the rootless daemon. |
| Resources | The contract carries CPU, memory, PID, output, and wall-time ceilings and classifies timeout, output exhaustion, and out-of-memory outcomes separately where observable. | Code and contract tests | Verify cgroup v2 enforcement plus fork, memory, disk, output, and timeout attacks. A real writable-byte quota remains required. |
| Inputs | Workspace and grader inputs are supplied as separate read-only mounts; source trees reject Git metadata, links, and special files before tests run. This physical separation does **not** hide the grader from candidate code in the current single container. | Code and fixture tests | Build a boundary in which candidate code cannot read hidden/gold/future-history material or the grader, then prove Git objects and mounts expose none of it. |
| Evaluator | The host validates and applies the patch to a fresh copy. A pinned image-owned entrypoint copies the workspace, discovers the out-of-tree grader, rejects skipped/wrong-count tests, and emits one structured completion record; the parser rejects absent, multiple, malformed, nonfinal, and mismatched records. Naïve marker spoofing is rejected. However, candidate imports share the trusted interpreter, and a live adversarial patch demonstrably forges a pass by monkeypatching `unittest`. | Code; 12 daemon-free integration cases; two positive live development paths; one strict expected failure | Redesign the candidate/grader/result boundary, make the tampering and grader-read tests pass, then run all negative controls on the reference host. |
| Patch intake | Existing validation rejects several unsafe patch shapes before evaluation. | Local integration tests | Complete the traversal, submodule, binary, decompression/file-count, grader-path, and container adversarial corpus. |
| Crash recovery and evidence | Transactional phase methods validate manifest/event/artifact-reference/budget agreement, treatment identity, lifecycle ordering, one-call accounting, terminal outcome/status, and duplicate indexes. Recovery marks missing expected artifacts `not_produced`, classifies an outstanding request `ambiguous`, charges its full reservation, commits an infrastructure-error terminal state, and is idempotent. Pre-dispatch budget refusal is separately terminalized as `budget_exhausted`. | Code; event-store, domain, CLI, and fixture-runner tests | Run real process kill points across intent, reservation, dispatch, CAS write, evaluation, and commit boundaries; verify referenced CAS bytes at startup; reconcile filesystem orphans and concurrent/open-process failure. |
| External calls | The evidence state machine now represents request start, completion, and ambiguity, and rejects terminal streams with unclassified outstanding work. No provider-backed exactly-once claim is made. | Code and local fake-model tests | Exercise a real provider adapter with SDK retries disabled, durable idempotency keys, accepted/uncommitted calls, conservative charging, and invoice/usage reconciliation. |

## 4. Stage 1 verification matrix

The blank reference-host cells are deliberate gate evidence slots, not implied passes.

| Requirement | Development evidence | Reference-host result | Required artifact |
|---|---|---|---|
| Normal container evaluator: gold patch passes | Exact digest ran the complete fixture runner twice; both runs passed 5/5 and produced the same normalized semantic digest | **Not run** | Command, test log, task/patch hash, image digest |
| Negative controls remain distinct: no-op, visible-only, hidden-only, regression | Existing local suite; container conversion is fake-sandbox tested | **Not run** | Per-case structured evaluation records |
| Invalid traversal, symlink, submodule, binary, oversize, file-count, and grader-path patches fail before apply | Partial local suite | **Not run** | Adversarial patch corpus and result table |
| Container runs non-root with read-only root, dropped capabilities, `no-new-privileges`, and required seccomp | Exact command contract plus a live development probe; all 16 active control checks were true | **Not run** | `docker inspect`, in-container probes, attack log |
| Worker/evaluator cannot reach DNS, TCP, metadata endpoints, host services, or daemon sockets | Live development probe confirmed no external TCP route, Docker environment, or Docker socket; broader route/service matrix remains open | **Not run** | Network/socket probe log |
| No host secrets, credentials, Docker socket, hidden tests, gold data, or evaluator definition reach the worker | Partial bundle/code inspection | **Not run** | Planted-secret and mount inventory report |
| No future solution survives refs, packed refs, tags, reflogs, alternates, or unreachable objects | Source-export invariant planned | **Not run** | Future-history recovery probe |
| CPU, memory, PID, writable-byte, output, and wall-time attacks terminate inside aggregate caps | Mocked lifecycle tests plus live development cgroup/output/wall probes; output was capped at 1,024 bytes, timeout was classified, and containers were removed | **Not run** | cgroup/quota readings and classified results |
| Evaluator command/grader cannot be altered and completion cannot be spoofed | **Failed:** the checked-in candidate patch monkeypatches `unittest` and is falsely accepted; the strict expected-failure test preserves the result. Candidate code can also read the grader mount. | **Blocked by design, not merely unrun** | Passing same-interpreter tampering and grader-read tests after evaluator redesign |
| Interrupted runs become one reconstructable terminal state with retained budget/failure evidence | Pure tests cover ambiguous full-charge, explicit absences, budget-exhausted classification, idempotency, completed-response preservation, strict replay/index agreement, and transactional rollback | **Not run** | Process-level kill-point/recovery table and replay output |
| Exact OCI digest re-evaluates identically; rebuild is labeled separately | Two definition builds produced the same development repository digest; that exact digest produced matching full-runner semantic results | **Not run** | Registry-preserved digest, native run, and rebuild comparison |
| 100-run normalized semantic determinism after hardening | Passed: 100 repetitions, one digest (`3ae72ecbeea53aea65f2b725766c2c25679c61e8cb48ab3b6c9976daba2ffb01`) | **Not run** | Digest-frequency report |
| At least 99% of the predeclared normal fixture campaign ends without infrastructure error | Not run | **Not run** | Attempt manifest with denominator and classifications |

## 5. Development smoke record

This section records convenience testing only. Nothing here can change the gate verdict.

| Field | Recorded value |
|---|---|
| Host | Apple Silicon macOS |
| Docker environment | Docker Desktop 29.3.1; rootful daemon inside an ARM Linux VM; cgroup v2 and the built-in seccomp profile reported |
| Reference architecture match | No; required target is native x86_64 Linux |
| Repository revision | The checkpoint commit containing this report (recorded in Git history) |
| Evaluator base image | `python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b` |
| Built evaluator repository reference | `guildmind/evaluator@sha256:248f1c9fa725c4576ca4506242351bcf97fbb577c5d410afdafe193f08c55045` from two development builds with `SOURCE_DATE_EPOCH=0` and provenance/SBOM output disabled |
| Local image ID | `sha256:248f1c9fa725c4576ca4506242351bcf97fbb577c5d410afdafe193f08c55045` (development daemon only; recorded separately from the repository reference even though the values coincide here) |
| Evaluator smoke | The exact repository digest completed 5/5 tests through `ContainerEvaluator` and through two complete `FixtureRunner` runs; the two normalized semantic digests matched |
| Active sandbox probe | All 16 development checks passed: UID/GID, capabilities, `no-new-privileges`, seccomp, network/socket/proxy absence, read-only root, writable bounded tmpfs mounts, and cgroup CPU/memory/swap/PID readings |
| Adapter lifecycle smoke | Under the explicit development-only host policy: normal execution exited 0; output retained exactly 1,024 bytes and classified `output_exhausted`; wall timeout was classified; every observed managed container was removed |
| Evaluator adversarial smoke | **Known failure:** [`unittest-tampering.patch`](../../fixtures/001-python-addition/adversarial/unittest-tampering.patch) forged a pass; retained as one strict `xfail` |
| Sandbox unit slice | 36 passed; mocked Docker CLI only |
| Default Python suite | 109 passed, 7 explicitly image/host-configured tests skipped |
| Development-image Python suite | 114 passed, 1 reference-host test skipped, 1 evaluator-tampering test expected-failed |
| Static typing | Passed for 40 source files |
| Formatting and lint | Passed (`ruff format --check` and `ruff check`) |
| Determinism rerun | 100 repetitions; one semantic digest (`3ae72ecbeea53aea65f2b725766c2c25679c61e8cb48ab3b6c9976daba2ffb01`) |
| Strict doctor result | Correctly failed with exit 1 on `architecture_not_x86_64` and `rootless_required`, while verifying the configured image locally |

An image ID from this environment is useful for reproducing this development smoke only. It is not the future campaign image identity and does not establish native x86_64 behavior.

The invocation requested disabled daemon logging and health checks, disabled networking, a read-only root, all capabilities dropped, `no-new-privileges`, the built-in seccomp profile, a private cgroup namespace, no IPC namespace, a non-root user, CPU/memory/swap/PID limits, bounded writable `tmpfs` mounts, and read-only inputs. This records that the development environment accepted those settings and completed the fixture; it is not authoritative enforcement evidence.

## 6. Known limitations and residual risks

- **Rootful development daemon:** Docker Desktop does not establish the required rootless-daemon boundary.
- **Architecture mismatch:** the available ARM VM is not the native x86_64 reference environment. Emulation or a cross-platform build cannot substitute for a native gate run.
- **Shared kernel:** worker and evaluator containers share the Docker VM kernel. Containers reduce exposure but do not defend against every kernel exploit; authoritative runs require a dedicated disposable host without unrelated secrets.
- **Shared-interpreter evaluator failure:** the parser rejects absent, duplicate, malformed, nonfinal, wrong-count, and skipped-test completion records, so simple output-marker imitation is not enough. But candidate code and the trusted `unittest` runner share one interpreter. The live tampering fixture monkeypatches the runner and forges a pass, and candidate code can read `/inputs/grader`. A separate trusted result-producing boundary is required before hostile evaluation.
- **Writable-disk enforcement:** a writable temporary area does not by itself prove a hard byte quota. The reference host must demonstrate quota enforcement and disk-bomb termination.
- **Contract versus enforcement:** command construction can request network, capability, mount, and cgroup restrictions without proving that the daemon honored them.
- **Ambient image environment:** request validation controls explicitly supplied variables, but image-defined environment and runtime defaults need live-image inspection before exact environment provenance can be claimed.
- **Docker endpoint and orphan lifecycle:** strict admission inspects the selected daemon but does not yet prove that the endpoint is the intended local controlled host. Normal cleanup is implemented, but an ambiguous `docker create` or control-plane crash can leave a deterministically named stopped container; startup discovery/cleanup by managed labels is not implemented.
- **Crash semantics are local:** the state machine represents an accepted-but-uncommitted request as ambiguous and charges its full reservation, but no real provider adapter has demonstrated idempotency, status polling, duplicate suppression, or invoice reconciliation.
- **No process-crash injection yet:** transaction rollback and explicit restart recovery are tested, but no case sends a real `SIGKILL` at SQLite/CAS/dispatch boundaries. Provider idempotency and polling are also untested.
- **CAS verification and orphan lifecycle:** SQLite validates artifact-reference metadata and its agreement with events/manifests, but startup/recovery does not yet re-read every referenced CAS byte or reconcile/quarantine unreferenced blobs left by a process crash.
- **Incomplete resource accounting:** the integrated ledger durably reserves/reconciles model usage and enforces one call for the current lifecycle, but observed evaluator/container wall and CPU consumption is not yet reconciled into `BudgetUsage`.
- **Probe is not a gate:** `doctor --production` is a necessary host/image/control probe. Even a passing probe would not certify evaluator discrimination, process-kill recovery, corpus hygiene, or the 99% reliability campaign.
- **Fixture breadth and reliability:** the repository does not yet contain the planned 20 known-outcome fixtures, and the predeclared 99% normal-fixture campaign has not run.
- **No authorization expansion:** external repositories, arbitrary model-generated commands, real provider calls, baseline campaigns, institution genomes, search, PettingZoo, persistent memory, and judge societies remain unauthorized by this checkpoint.

## 7. Reference-host gate procedure

First replace the shared-interpreter evaluator with a boundary in which candidate code cannot read the grader or mutate the trusted result producer; the current strict expected failure must become a passing adversarial test. Then run the following procedure on a dedicated disposable x86_64 Linux host with no unrelated credentials or valuable data:

1. Install and select a rootless Docker daemon. Prove the Docker endpoint is the intended controlled host, and record OS, kernel, native architecture, client/server versions, storage driver, seccomp profile, and daemon security options.
2. Confirm cgroup v2 delegation for the rootless user and prove CPU, memory, PID, and wall-time enforcement. Provision and prove a real writable-byte quota.
3. Check out the reviewed checkpoint by commit hash. Build or pull the evaluator for native `linux/amd64`; record the Dockerfile hash, pinned base digest, resulting immutable image digest, and any registry provenance.
4. Run `guildmind doctor --production --json` with the exact evaluator repository digest and require a fail-closed result if any mandatory control is unavailable. Preserve its machine-readable output; do not treat a pass as the Stage 1 verdict.
5. Run formatting, lint, type, unit, integration, replay, and 100-repetition semantic-determinism checks from the locked environment.
6. Run every adversarial sandbox/evaluator case in the matrix above. Preserve container inspect data, cgroup readings, stdout/stderr artifacts, structured outcomes, and cleanup evidence.
7. Run process-level kill points, restart recovery, referenced-CAS verification, and orphan discovery/reconciliation.
8. Run the predeclared normal-fixture reliability campaign. Reconcile every intended attempt to exactly one terminal manifest; report infrastructure failures with the denominator. The rate must be at most 1%.
9. Re-evaluate with the exact recorded image digest. Rebuild the pinned definition separately and label any comparison as supply-chain validation, not execution identity.
10. Review every failure without waiving a critical control. Update this report with immutable evidence references and retain **NOT PASSED** unless every Stage 1 exit criterion is satisfied.

## 8. Exit-gate accounting

| Stage 1 exit criterion | Verdict at this checkpoint |
|---|---|
| No missing or reordered semantic events; stable ordering is declared | Partial local evidence; reference stress/kill suite outstanding |
| Replay reconstructs the same terminal state and budget | Partial local evidence; recovery replay evidence outstanding |
| Evaluator results are deterministic and discriminating on all fixtures | Gold-path determinism passed; adversarial discrimination **failed** because same-interpreter tampering forged a pass |
| Sandbox boundary tests fail closed | **Not demonstrated on the reference host** |
| At least 99% of normal fixture runs avoid infrastructure error | **Not run** |

## 9. Verdict

**Stage 1 is NOT PASSED.**

The checkpoint is suitable for continued development against trusted repository-owned fixtures. It does not authorize external tasks, hostile repositories, arbitrary model-generated commands, provider-backed pilots, or Stage 2 baseline work. Advancement requires evaluator-process/grader isolation that closes the demonstrated false pass, the reference-host evidence above, and the still-open Stage 0 owner approvals in [Experiment 0001](../experiments/0001-institutional-search.md#3-owner-decisions-required-before-stage-0-exit).
