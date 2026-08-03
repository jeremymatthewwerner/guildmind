# Fixture Batch 002 — Development-Container Qualification

**Recorded:** 2026-08-03<br>
**Batch:** `stage1-fixture-batch-002` (fixtures 006–009)<br>
**Verdict:** **DEVELOPMENT PASS**; reference-host gate **NOT RUN / NOT PASSED**<br>
**Repository revision under test:**
`13d3b5ebf0dba0b585999e135bac15b5f0032d5d`<br>
**Evaluator:** `guildmind/container-python-call-v2`<br>
**Image:**
`guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7`

## Result

All four fixed second-batch fixtures matched their declared outcome in three consecutive
two-phase container evaluations per outcome:

| Fixture | Sealed cases | Pristine-control result ×3 | Gold result ×3 | Infrastructure errors |
|---|---:|---|---|---:|
| 006 escaped run decoder | 6 | `tests_failed` (5/6 mismatches) | `passed` (6/6) | 0 |
| 007 exact integer apportionment | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 008 stable topological order | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 009 ordered nested changes | 6 | `tests_failed` (6/6 mismatches) | `passed` (6/6) | 0 |

This is 4 fixtures × 2 outcomes × 3 repetitions = **24 evaluator results** and **48
disposable containers**: one untrusted candidate container and one disjoint trusted
scorer container for every result. Within each fixture/outcome cell, the three complete
normalized evaluator results were identical. Every candidate and scorer exited without
output truncation; every scorer ran all six cases with zero skips or evaluator errors.
The Docker adapter fails an evaluation if managed-container removal cannot be verified;
all 48 phases completed without that failure, and the final managed-container inventory
was empty.

The [machine-readable report](report.json) binds each fixture's canonical manifest,
frozen workspace, candidate-visible challenge, sealed oracle, task identity, resource
limits, exact pristine/gold patch, candidate response, trusted completion record, and
evaluation binding. Its self-bound body SHA-256 is
`8f09f1ab8a7226de786fb44dbbb648afdb96e96dc346ab48e6b2d8c372f33da3`.
The complete canonical file including its final LF has SHA-256
`ef1b50596831b8648eb6cab554dca47b0f3625a371e98b0ce3a475e9f2275df9`.

## What was exercised

The four fixture families are deliberately different:

- a stateful run parser with multi-digit counts, escaped literal separators, and a null
  malformed-input result;
- exact Hamilton apportionment with zero weights, stable index ties, and integer values
  beyond binary64's exact range;
- lexicographically minimal topological ordering with dynamic readiness and a null cycle
  result; and
- ordered root/nested set, delete, and list-insert operations whose later paths observe
  earlier changes.

Each checked-in pristine control is a semantics-preserving rewrite of the known-bad
implementation. The same exact control and gold bytes ran in the trusted-local gate and
this container gate. Each container evaluation then:

1. loaded the fixture and canonical six-case oracle from frozen bytes;
2. validated the one-file patch and exact expected patch SHA-256;
3. derived an expected-value-free challenge;
4. ran patched candidate code in an offline, read-only, capability-dropped container
   with only the workspace and challenge mounted;
5. retained the bounded candidate response as hostile data;
6. ran the trusted scorer in a fresh container with only challenge, oracle, and response
   mounted; and
7. accepted only a final schema-valid completion matching the control-plane task,
   source, patch, image, limits, challenge, oracle, and response commitments.

The report was emitted by the repository-owned recorder only after requiring a
tracked-clean revision. It uses canonical JSON, a same-directory atomic no-replace
publication, file and parent-directory synchronization, and a self-bound body digest.
An always-on unit test independently recomputes the report and current fixture/protocol
identities. The opt-in live integration test re-computes every stable result, response,
completion, and evaluation-binding digest from fresh containers.

## Image and host facts

This matrix reused the exact Batch 001 development image. Docker inspection reported
Linux/amd64, image ID equal to the pinned digest, UID/GID `65532:65532`, and no declared
volumes. The checked-in build inputs and pinned base are recorded in the
[Batch 001 image evidence](../2026-08-03-batch-001-development-container/README.md#image-and-host-facts);
they were not rebuilt or relabeled for this run.

The host remained macOS 26.5.2 on Apple Silicon with Docker Desktop server 29.3.1. Its
daemon is a rootful `aarch64` Linux VM, so `guildmind doctor` continues to report
`architecture_not_x86_64` and `rootless_required`. The amd64 evaluator ran under Docker
Desktop emulation. Development policy permits this for finding fixture/evaluator defects;
reference policy correctly rejects it.

## Reproduction and verification

From a clean checkout of the recorded revision, with the exact image already present:

```bash
uv run python scripts/record_fixture_container_batch.py \
  --batch-id stage1-fixture-batch-002 \
  --image guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7 \
  --output /new/canonical/path/report.json \
  --recorded-on 2026-08-03 \
  --repetitions 3 \
  006-run-decoder \
  007-apportionment \
  008-topological-order \
  009-ordered-changes
```

The recorder refuses tracked source drift, duplicate or unordered fixtures, mutable image
references, invalid dates, and existing output files. It does not dispatch a model.

Independent repository verification used:

```bash
export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'

uv run pytest -q tests/unit/test_fixture_batch_evidence.py
uv run pytest -q tests/integration/test_fixture_reliability_container.py -k second
make check
```

The static verifier passed both checked reports. The focused Batch 002 live verifier
passed 4/4 cases (with four Batch 001 cases deselected) in 20.30 seconds. The complete
development-image repository gate passed **699 tests with 13 declared skips in 75.60
seconds**; Ruff reported 154 formatted files and no lint findings, and strict mypy passed
across 83 source files. The 13 skips are the 11 unconfigured reference-host evaluator
cases and two APFS-invalid-name cases; every configured development-container case ran.

No model provider, hosted runtime, cloud deployment, or paid inference was used.

## Evidence boundary and next gate

This artifact proves deterministic expected classifications for four small,
repository-owned JSON-call fixtures on one development host. It is **not**:

- rootless x86_64 reference-host evidence;
- the cumulative fixtures 001–009 campaign or its transactional ledger/CAS report;
- the final 20-fixture × 5-round reliability denominator;
- a confidence-bound claim that infrastructure reliability exceeds 99%;
- general arbitrary-repository or hostile-command containment; or
- evidence about provider idempotency, billing, or real-model capability.

The next checkpoint described by this artifact was subsequently completed without
modifying either accepted historical manifest: the separately frozen cumulative
fixtures 001–009 schedule produced the
[Batch 002 local calibration](../../reliability-campaigns/2026-08-03-batch-002-local-calibration/README.md).
Reference-host repetition and the final 20-fixture denominator remain required before
the Stage 1 verdict can change.
