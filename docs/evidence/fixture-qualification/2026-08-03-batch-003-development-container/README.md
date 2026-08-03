# Fixture Batch 003 — Development-Container Qualification

**Recorded:** 2026-08-03<br>
**Batch:** `stage1-fixture-batch-003` (fixtures 010–013)<br>
**Verdict:** **DEVELOPMENT PASS**; reference-host gate **NOT RUN / NOT PASSED**<br>
**Repository revision under test:**
`a39d03f8f8ca6b64fa53192c4828e45f8a4ab83c`<br>
**Evaluator:** `guildmind/container-python-call-v2`<br>
**Image:**
`guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7`

## Result

All four fixed third-batch fixtures matched their declared outcome in three consecutive
two-phase container evaluations per outcome:

| Fixture | Sealed cases | Pristine-control result ×3 | Gold result ×3 | Infrastructure errors |
|---|---:|---|---|---:|
| 010 greedy word wrap | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 011 half-open business days | 6 | `tests_failed` (5/6 mismatches) | `passed` (6/6) | 0 |
| 012 canonical Roman parser | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 013 rectangular grid rotation | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |

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
`5b1806ed105b2c3cb7f3c528669601b7fb30892080835a03fa1fd0e890035d7c`.
The complete canonical file including its final LF has SHA-256
`55374a6ebb09d70d3dbbed8709f9f1b0203832b1de462f8afca53bc255273347`.

## What was exercised

The four fixture families are deliberately different:

- greedy ordered word packing with exact-fit lines, exact single-space output, and an
  unsplit oversized-word policy;
- half-open ISO-date iteration with weekends, duplicate holidays, holiday/weekend
  overlap, and a leap-day holiday;
- canonical uppercase Roman-numeral validation across additive, legal subtractive,
  illegal repetition, and illegal subtraction forms; and
- clockwise rotation across wide, tall, single-row, single-column, empty, and mixed-JSON
  grids.

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
identities. The opt-in live integration test recomputes every stable result, response,
completion, and evaluation-binding digest from fresh containers.

## Image and host facts

This matrix reused the exact Batch 001/002 development image. Docker inspection recorded
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
  --batch-id stage1-fixture-batch-003 \
  --image guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7 \
  --output /new/canonical/path/report.json \
  --recorded-on 2026-08-03 \
  --repetitions 3 \
  010-word-wrap \
  011-business-days \
  012-roman-parser \
  013-grid-rotation
```

The recorder refuses tracked source drift, duplicate or unordered fixtures, mutable image
references, invalid dates, and existing output files. It does not dispatch a model.

Independent repository verification used:

```bash
export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'

uv run pytest -q tests/unit/test_fixture_batch_evidence.py
uv run pytest -q tests/integration/test_fixture_reliability_container.py -k third
make check
```

The static verifier passed all three checked reports. The focused Batch 003 live verifier
passed 4/4 cases (with eight earlier-batch cases deselected) in 20.32 seconds. The complete
default repository gate passed **682 tests with 41 declared skips in 19.06 seconds**. The
complete development-image gate passed **710 tests with 13 declared skips in 97.09
seconds**; every configured development-container case ran. Ruff reported 173 formatted
files and no lint findings, and strict mypy passed across 83 source files. The 13
development-image skips are 11 reference-host-only cases and two APFS-invalid-name cases.

No model provider, hosted runtime, cloud deployment, or paid inference was used.

## Evidence boundary and next gate

This artifact proves deterministic expected classifications for four small,
repository-owned JSON-call fixtures on one development host. It is **not**:

- rootless x86_64 reference-host evidence;
- the cumulative fixtures 001–013 campaign or its transactional ledger/CAS report;
- the final 20-fixture × 5-round reliability denominator;
- a confidence-bound claim that infrastructure reliability exceeds 99%;
- general arbitrary-repository or hostile-command containment; or
- evidence about provider idempotency, billing, or real-model capability.

The next checkpoint is to freeze a new content-bound cumulative Batch 003 manifest for
fixtures 001–013 without modifying any accepted historical manifest, then run its exact
zero-retry schedule from a clean revision into new local state/report paths.
Reference-host repetition remains required before the Stage 1 verdict can change.
