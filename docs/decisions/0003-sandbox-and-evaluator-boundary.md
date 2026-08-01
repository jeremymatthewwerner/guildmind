# ADR 0003: Sandbox and Evaluator Boundary

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-07-31

## Context

Repository code, model-generated commands, and submitted patches are untrusted. Correctness tests and gold data must remain outside the treatment, while resource consumption must be comparable across solo and multi-agent organizations. A local subprocess is not a security boundary.

## Decision

Use a native Guildmind `Sandbox`/`Evaluator` boundary backed initially by rootless Docker on a controlled x86_64 Linux host. The coding-task lifecycle remains the source of truth; a later PettingZoo adapter is optional and cannot define scheduler semantics.

### Worker boundary

Compile each task into physically separate worker and grader bundles. The worker receives only a randomized alias, prompt, sanitized pre-cutoff source snapshot, permitted visible tests, and a bounded writable workspace. Prefer exporting the source tree without `.git`; if history is required, reconstruct only approved objects.

Worker containers use a prebuilt offline image, non-root UID, read-only base, dropped capabilities, `no-new-privileges`, default-or-stricter seccomp, no network, no host secrets, no Docker socket, and enforced cgroup v2 CPU/memory/PID plus disk, output, and wall-time limits. Limits apply to the whole organization/task attempt. `doctor` fails closed when the reference host cannot enforce them.

Model calls occur in the trusted control plane. It holds provider credentials and brokers only declared tool operations into the sandbox. Workers cannot access the search database, task indexes, future history, evaluator, hidden tests, gold patch, or grader metadata.

### Patch boundary

The worker returns a patch as hostile data. Before application, reject absolute or traversal paths, unexpected path prefixes, grader/test modifications, symlink and submodule mode changes, binary/oversize payloads, excessive file counts, and other schema violations. Apply an accepted patch to a fresh clean repository, never the worker's mutable workspace.

### Evaluator boundary

Run the patched repository in a fresh disposable evaluator container with no network, credentials, search state, or writable evaluator definition. Hidden tests and the fixed test command live in an immutable out-of-tree grader bundle. Candidate code cannot select the command or write the grader. The evaluator has independent resource/output/time caps and emits a content-hashed result identifying task, source, patch, worker/grader images, evaluator version, tests, command, and evidence.

The control plane alone commits that result. An evaluator failure is not a worker success or failure and follows the experiment's frozen infrastructure policy.

## Consequences

Worker and evaluator setup costs more than a subprocess loop, but the boundary supports credible hidden evaluation and adversarial repositories. Rootless containers reduce privilege; they do not eliminate kernel risk, so authoritative work runs on dedicated controlled hosts with no unrelated secrets.

Task generation and authoritative execution are pinned to x86_64 Ubuntu 22.04-compatible images for the first corpus. Image cache demand is measured early; large campaigns may require 500 GB–1 TB of local cache or a trusted remote registry with digest pinning and eviction.

## Alternatives considered

- Local subprocesses: acceptable only for repository-owned deterministic engineering fixtures, never untrusted/external tasks.
- Reuse the worker container for grading: rejected because the candidate may alter its filesystem and evaluator inputs.
- Give containers a package-install network: rejected; dependencies must be prebuilt or mirrored into the immutable image.
- Adopt a third-party agent framework or environment API as the core lifecycle: rejected because hidden orchestration would become part of the treatment.

## Acceptance checks

- Planted hidden/gold/future-history strings cannot be found from the worker, including through refs, packed refs, tags, reflogs, alternates, or unreachable objects.
- Network, credential, host path, Docker socket, privilege, fork, memory, disk, output, and timeout attacks fail closed.
- Traversal, symlink, submodule, binary, oversize, and grader-path patches are rejected before application.
- No-op, visible-test-only, unsafe, timeout, regression, and gold patches produce the expected distinct evaluator results.
- Re-evaluation uses the exact recorded image digests; a later rebuild is labeled validation rather than identity.

See the full [threat model](../threat-model.md).
