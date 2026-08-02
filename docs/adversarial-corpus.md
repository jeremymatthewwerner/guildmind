# Adversarial evaluator corpus

Guildmind keeps evaluator attacks as versioned fixture data, not only as test code. Each fixture corpus has one [`corpus.json`](../fixtures/001-python-addition/adversarial/corpus.json) manifest beside its patch files. The manifest predeclares the exact patch digest, threat class, evaluator phase, terminal status, scorer classification, and output-truncation result for every case. The three phases are deliberately distinct: `intake` rejects a patch before Git or a sandbox runs, `candidate` terminates inside untrusted execution, and `scorer` obtains a trusted negative verdict.

The strict loader rejects duplicate or unknown fields, invalid enums, inconsistent phase/status combinations, duplicate or unordered IDs, non-regular or symlinked patch files, digest drift, and any `.patch` file that is missing from the manifest. This makes adding an attack without an expectation—and silently changing an existing attack—an immediate test failure.

## Fixture 001 matrix

| Case | Class | Expected phase | Exact expected result |
|---|---|---|---|
| `boundary-completion-forgery` | Boundary integrity | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `boundary-empty-response` | Boundary integrity | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `boundary-grader-read` | Boundary integrity | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `boundary-unittest-tampering` | Boundary integrity | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `functional-no-op` | Functional control | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `functional-visible-only` | Functional control | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `functional-wrong-operation` | Functional control | Scorer | `tests_failed`; trusted classification `candidate_failed` |
| `intake-absolute-path` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-binary` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-container-target` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-file-count` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-grader-path` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-oversize` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-submodule-mode` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-symlink-mode` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `intake-traversal` | Boundary integrity | Intake | `invalid_patch`; Git and sandbox absent |
| `resource-memory-oom` | Resource exhaustion | Candidate | `oom_killed`; scorer absent |
| `resource-output-bomb` | Resource exhaustion | Candidate | `output_exhausted`; output truncated; scorer absent |
| `resource-timeout` | Resource exhaustion | Candidate | `timed_out`; scorer absent |

The nine intake cases run on every host with fail-if-called Git-application and sandbox boundaries. They cover absolute and traversal targets, a relative grader target, an absolute container-mount target, symlink and submodule modes, a Git binary payload with a huge claimed decompressed literal, a real byte-ceiling violation, and a real file-count violation. Exact diagnostics show that each case reaches its intended rejection branch. The [development evidence record](evidence/patch-intake/2026-08-02-development/README.md) preserves the matrix, hashes, test results, parser fixes, and evidence limits.

The functional controls also run through the trusted local evaluator. A separate precondition test applies `functional-visible-only`, proves that `test_visible.py` passes, and then proves that authoritative visible-plus-hidden evaluation fails; this keeps it distinct from a generic wrong answer. The ten cases that intentionally reach candidate or scorer execution run through `ContainerEvaluator` in two deliberately separate test paths:

- `container` uses only `GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE` and the explicitly relaxed development host policy;
- `reference_sandbox` uses only `GUILDMIND_REFERENCE_EVALUATOR_IMAGE` and strict host admission.

Neither path falls back to the other. A passing development run is convenience evidence from that host, not reference verification. A pytest result is also not the final reference evidence package: the authoritative runner must emit a machine-readable record keyed by corpus-manifest hash, case ID, patch SHA-256, image digest and ID, host assessment, observed outcome, transcript artifact references, and cleanup result.

## Resource-probe boundary

Memory, PID, and disk attacks were initially withheld from this manifest rather than
assigned outcomes from configured Docker flags. Guildmind now has a fixed image-owned
probe and a strict `guildmind.resource-probe-evidence/v1` record that separates
configuration, active enforcement, cleanup, development/reference tier, and evaluator
status.

Three repeated Docker Desktop runs produced the same development verdicts and are
preserved in the [2026-08-02 evidence bundle](evidence/resource-probes/2026-08-02-docker-desktop/README.md):

- memory ended with Docker `OOMKilled=true`, exit 137, and Guildmind `oom_killed`;
- PID pressure reached `pids.max`, the next bounded fork returned Linux `EAGAIN`, both
  available max-event counters incremented, and every child was reaped; and
- `/workspace` and `/tmp` accepted exactly their declared byte ceilings, then returned
  Linux `ENOSPC`, reported zero free bytes, and recovered after unlink.

Every report says `all_enforced=true` but `reference_passed=false`, because Docker
Desktop is a rootful ARM development environment. The stable OOM observation now has a
content-addressed evaluator case with candidate `oom_killed` and no scorer; it passed
three consecutive development repetitions. PID and disk remain direct probes: Docker
exposes no matching typed evaluator
status, so a timeout or ordinary failed response cannot honestly be relabeled PID or
disk exhaustion. All cases still require repetition on the rootless native x86_64
reference host before they contribute to the authoritative Stage 1 verdict.
