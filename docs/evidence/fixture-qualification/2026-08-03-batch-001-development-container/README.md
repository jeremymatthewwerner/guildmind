# Fixture Batch 001 — Development-Container Qualification

**Recorded:** 2026-08-03<br>
**Batch:** `stage1-fixture-batch-001` (fixtures 002–005)<br>
**Verdict:** **DEVELOPMENT PASS**; reference-host gate **NOT RUN / NOT PASSED**<br>
**Repository revision under test:**
`c11d38b8372937191153ce4a87805f27b281f1d0`<br>
**Evaluator:** `guildmind/container-python-call-v2`<br>
**Image:**
`guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7`

## Result

All four fixed first-batch fixtures matched their declared outcome in three consecutive
two-phase container evaluations per outcome:

| Fixture | Sealed cases | Pristine-control result ×3 | Gold result ×3 | Infrastructure errors |
|---|---:|---|---|---:|
| 002 Unicode slug normalization | 6 | `tests_failed` (5/6 mismatches) | `passed` (6/6) | 0 |
| 003 closed-interval merge | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 004 JSON Pointer traversal | 6 | `tests_failed` (6/6 mismatches) | `passed` (6/6) | 0 |
| 005 structural stable dedupe | 6 | `tests_failed` (5/6 mismatches) | `passed` (6/6) | 0 |

This is 4 fixtures × 2 outcomes × 3 repetitions = **24 evaluator results** and **48
disposable containers**: one untrusted candidate container and one disjoint trusted
scorer container for every result. Within each fixture/outcome cell, the three complete
normalized evaluator results were identical. Every candidate and scorer exited without
output truncation; every scorer ran all six cases with zero skips or evaluator errors.
The Docker adapter confirmed removal after each phase, and a final managed-container
inventory was empty.

The [machine-readable report](report.json) binds each fixture's canonical manifest,
frozen workspace, candidate-visible challenge, sealed oracle, task identity, resource
limits, exact pristine/gold patch, candidate response, trusted completion record, and
evaluation binding. Its self-bound body SHA-256 is
`191f09d5b3b3a5f591b159ba96a9c3805f76449c457e394ec6bac8631f850f95`.

## What was exercised

Each checked-in pristine control makes a semantics-preserving rewrite of the known-bad
implementation. The same exact patch bytes now run in both the trusted-local and
development-container gates. Each container evaluation then:

1. loads the fixture and canonical six-case oracle from frozen bytes;
2. validates the one-file patch and exact expected patch SHA-256;
3. derives an expected-value-free challenge;
4. runs patched candidate code in an offline, read-only, capability-dropped container
   with only the workspace and challenge mounted;
5. stores the bounded candidate response as hostile data;
6. runs the trusted scorer in a fresh container with only challenge, oracle, and response
   mounted; and
7. accepts only a final schema-valid completion record that matches the control-plane
   task, source, patch, image, limits, challenge, oracle, and response commitments.

The live integration test re-computes the report's stable result hash and binding fields
from every repeated run. A separate always-on unit test re-computes the report body hash,
fixture/protocol identities, patch hashes, task identity, and limits from the current
checked-in sources. This prevents the JSON artifact from becoming an unchecked narrative
snapshot.

## Image and host facts

`make evaluator-image` rebuilt the checked-in `linux/amd64` image from the pinned
`python:3.12.13-slim-bookworm` base and emitted the digest above. Docker inspection
reported Linux/amd64, image ID equal to the pinned digest, UID/GID `65532:65532`, and no
declared volumes. Build-input SHA-256 values were:

| Input | SHA-256 |
|---|---|
| `containers/evaluator/Dockerfile` | `7929e07930440550caca5526fb5ef8587361d598dfa0ac9f2a9b5d3a8da5ea68` |
| `containers/evaluator/invoke.py` | `d71179b753ed38c185ad758f0f913b2d95c3089e9903bd00a10fa0340ed30fbf` |
| `containers/evaluator/score.py` | `bce37b52b60472623c3b04c23dab4b0fdf59c4d68cdcec12a0b7b13aa5dbaedb` |
| `containers/evaluator/containment_probe.py` | `97004c3494e8a1f1336c0c5034988f0fb11862d192021194e10de060885b858a` |
| `containers/evaluator/resource_probe.py` | `6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7` |

The host was macOS 26.5.2 on Apple Silicon with Docker Desktop server 29.3.1, LinuxKit
6.12.76, cgroup v2, builtin seccomp, private cgroup namespaces, and the required memory,
swap, CPU, and PID limit controls. The Docker daemon itself reported `aarch64` and no
rootless security option. `guildmind doctor` therefore returned:

- `local_fixture_ready: true`;
- `production_sandbox_ready: false`; and
- reference failures `architecture_not_x86_64` and `rootless_required`.

The amd64 evaluator ran under Docker Desktop emulation. Development policy permits that
configuration so engineers can find fixture/evaluator defects locally; reference policy
correctly rejects it.

## Reproduction

```bash
make evaluator-image

export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'

uv run pytest -q \
  tests/integration/test_fixture_reliability_container.py \
  tests/unit/test_fixture_batch_evidence.py

make check

uv run guildmind doctor --json \
  --evaluator-image "$GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE"
```

The focused evidence-bound run passed 5/5 tests. The final development-image repository
gate passed **683 tests with 13 declared skips in 54.28 seconds**; Ruff reported 134
formatted files and no lint findings, and strict mypy passed across 80 source files. The
13 skips are the 11 unconfigured reference-host evaluator cases and two APFS-invalid-name
cases; every configured development-container case ran.

No model provider, hosted runtime, cloud deployment, or paid inference was used.

## Evidence boundary and next gate

This artifact proves deterministic expected classifications for four small,
repository-owned JSON-call fixtures on one development host. It is **not**:

- rootless x86_64 reference-host evidence;
- the five-fixture batch-calibration campaign or its transactional ledger/CAS report;
- the final 20-fixture × 5-round reliability denominator;
- a confidence-bound claim that infrastructure reliability exceeds 99%;
- general arbitrary-repository or hostile-command containment; or
- evidence about provider idempotency, billing, or real-model capability.

The next checkpoint is to freeze a new content-bound batch-calibration manifest for
fixtures 001–005 without modifying the accepted one-fixture smoke manifest, then run its
exact zero-retry schedule through new local state/report paths. Reference-host repetition
remains required before the Stage 1 verdict can change.
