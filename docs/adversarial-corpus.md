# Adversarial evaluator corpus

Guildmind keeps evaluator attacks as versioned fixture data, not only as test code. Each fixture corpus has one [`corpus.json`](../fixtures/001-python-addition/adversarial/corpus.json) manifest beside its patch files. The manifest predeclares the exact patch digest, threat class, evaluator phase, terminal status, scorer classification, and output-truncation result for every case.

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
| `resource-output-bomb` | Resource exhaustion | Candidate | `output_exhausted`; output truncated; scorer absent |
| `resource-timeout` | Resource exhaustion | Candidate | `timed_out`; scorer absent |

The functional controls also run through the trusted local evaluator. A separate precondition test applies `functional-visible-only`, proves that `test_visible.py` passes, and then proves that authoritative visible-plus-hidden evaluation fails; this keeps it distinct from a generic wrong answer. The complete matrix runs through `ContainerEvaluator` in two deliberately separate test paths:

- `container` uses only `GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE` and the explicitly relaxed development host policy;
- `reference_sandbox` uses only `GUILDMIND_REFERENCE_EVALUATOR_IMAGE` and strict host admission.

Neither path falls back to the other. A passing development run is convenience evidence from that host, not reference verification. A pytest result is also not the final reference evidence package: the authoritative runner must emit a machine-readable record keyed by corpus-manifest hash, case ID, patch SHA-256, image digest and ID, host assessment, observed outcome, transcript artifact references, and cleanup result.

## Deliberate next slice

Memory, PID, and disk attacks are not assigned premature evaluator outcomes in this manifest. Their enforcement semantics need direct reference-host probes first:

- memory must distinguish Docker's `OOMKilled` state from a catchable Python `MemoryError` or host wall timeout;
- PID enforcement must record `pids.events` and `EAGAIN`, because a generic fork-bomb timeout does not prove which limit fired;
- disk must demonstrate a real hard writable-byte quota and `ENOSPC`, not merely a configured tmpfs size.

Once those probes produce stable evidence on the required rootless x86_64 Linux host, their checked-in patches and exact classifications can be added to the same closed manifest.
