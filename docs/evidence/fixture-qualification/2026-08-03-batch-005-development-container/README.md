# Fixture Batch 005 — Development-Container Qualification

**Recorded:** 2026-08-03<br>
**Batch:** `stage1-fixture-batch-005` (fixtures 018–020)<br>
**Verdict:** **DEVELOPMENT PASS**; reference-host gate **NOT RUN / NOT PASSED**<br>
**Repository revision under test:**
`6492d5580fae5ab11de8cd3231cf1f91f99f4395`<br>
**Evaluator:** `guildmind/container-python-call-v2`<br>
**Image:**
`guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e`

## Result

All three fixed fifth-batch fixtures matched their declared outcome in three consecutive
two-phase container evaluations per outcome:

| Fixture | Sealed cases | Pristine-control result ×3 | Gold result ×3 | Infrastructure errors |
|---|---:|---|---|---:|
| 018 stable SemVer record selection | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 019 recursive key redaction | 6 | `tests_failed` (2/6 mismatches) | `passed` (6/6) | 0 |
| 020 recursive Boolean-rule evaluation | 6 | `tests_failed` (6/6 mismatches) | `passed` (6/6) | 0 |

This is 3 fixtures × 2 outcomes × 3 repetitions = **18 evaluator results** and **36
disposable containers**: one untrusted candidate container and one disjoint trusted
scorer container for every result. Within each fixture/outcome cell, the three complete
normalized evaluator results were identical. Every candidate and scorer exited without
output truncation; every scorer ran all six cases with zero skips or evaluator errors.
The Docker adapter fails an evaluation if managed-container removal cannot be verified;
all 36 phases completed without that failure, and the final managed-container inventory
was empty.

The [machine-readable report](report.json) binds each fixture's canonical manifest,
frozen workspace, candidate-visible challenge, sealed oracle, task identity, resource
limits, exact pristine/gold patch, candidate response, trusted completion record, and
evaluation binding. Its self-bound body SHA-256 is
`8fabdc03d52644e882b362732241c49b75f21672769107a9a23ebe1f2ed726f8`.
The complete canonical file including its final LF has SHA-256
`4eb133889039a0c8c76f60af78d38e39c0acc5a90c6719233ffc562d55e54db0`.

## What was exercised

The three final fixture families are deliberately distinct:

- selection of the original latest record per name using full Semantic Versioning
  precedence, including numeric core/prerelease components, numeric-versus-text
  identifiers, release precedence, ignored build metadata, and stable first wins;
- recursive case-sensitive object-key removal through maps nested below maps and arrays,
  while preserving array positions, scalar values, empty containers, and root scalars;
  and
- recursive pure `fact`/`all`/`any`/`not` evaluation with arbitrary JSON fact values,
  missing and falsy facts, empty-list identities, nested operators, and child-order
  independence.

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

## Reused image and host facts

Batch 005 changes only fixture, test, and documentation bytes. The evaluator build inputs
are byte-identical to the reproducibly rebuilt Batch 004 inputs, so this checkpoint
deliberately reuses that exact digest instead of claiming an unnecessary rebuild:

| Input | SHA-256 |
|---|---|
| `Dockerfile` | `7929e07930440550caca5526fb5ef8587361d598dfa0ac9f2a9b5d3a8da5ea68` |
| `invoke.py` | `35b43d0e7d3ff0dd1734e33746dca4323fc6c1169df91a9bacbdb2ce259e98dd` |
| `score.py` | `fdb8c31068de2116a0c3377c70a910faa675fe3eed452efa19a5eb21a8c50e48` |
| `containment_probe.py` | `97004c3494e8a1f1336c0c5034988f0fb11862d192021194e10de060885b858a` |
| `resource_probe.py` | `6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7` |
| Pinned base manifest | `d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b` |

The [Batch 004 evidence](../2026-08-03-batch-004-development-container/README.md)
records the two matching builds that produced the image. Inspection at this checkpoint
again recorded Linux/amd64, UID/GID `65532:65532`, working directory `/workspace`, the
trusted scorer entry point, and no declared volumes.

The host remained macOS 26.5.2 on Apple Silicon with Docker Desktop client/server
29.3.1. Its daemon is a rootful `aarch64` Linux VM with built-in seccomp and cgroup
namespaces, so `guildmind doctor` continues to report `architecture_not_x86_64` and
`rootless_required`. Development policy permits this for finding fixture/evaluator
defects; reference policy correctly rejects it.

This reused digest does not rewrite the accepted Batch 001–004 reports. Batches 001–003
remain bound to the earlier historical image; Batches 004–005 are bound to the current
finite-number-capable image.

## Reproduction and verification

From a clean checkout of the recorded revision with the exact image present, record into
a new canonical path:

```bash
uv run python scripts/record_fixture_container_batch.py \
  --batch-id stage1-fixture-batch-005 \
  --image guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e \
  --output /new/canonical/path/report.json \
  --recorded-on 2026-08-03 \
  --repetitions 3 \
  018-latest-versions \
  019-recursive-redaction \
  020-rule-evaluation
```

Independent repository verification uses:

```bash
export GUILDMIND_HISTORICAL_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'
export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e'

uv run pytest -q tests/unit/test_fixture_batch_evidence.py
uv run pytest -q tests/integration/test_fixture_reliability_container.py -k fifth
make check
```

The static verifier passed all five checked reports. The focused Batch 005 live verifier
passed 3/3 cases with 16 historical cases deselected in 15.26 seconds. The complete
default repository gate passed with Ruff clean across 205 files, strict mypy clean across
83 source files, and 702 pytest cases passed with 48 declared skips in 21.32 seconds. The
complete dual-image pytest gate then passed 737 cases with 13 declared skips in 135.08
seconds. All 19 configured historical/current fixture-container cases and the broader
development-image cases ran; the only remaining skips were 11 reference-host-only cases
and two APFS-invalid-name cases.

No model provider, hosted runtime, cloud deployment, or paid inference was used.

## Evidence boundary and next gate

This artifact proves deterministic expected classifications for three small,
repository-owned JSON-call fixtures on one development host. It is **not**:

- rootless x86_64 reference-host evidence;
- the cumulative fixtures 001–020 campaign or its transactional ledger/CAS report;
- the final 20-fixture × 5-round reliability denominator;
- a confidence-bound claim that infrastructure reliability exceeds 99%;
- general arbitrary-repository or hostile-command containment; or
- evidence about provider idempotency, billing, or real-model capability.

The next checkpoint is to freeze a new content-bound cumulative Batch 005 manifest for
fixtures 001–020 without modifying any accepted historical manifest, then run its exact
zero-retry schedule from a clean revision into new local state/report paths. The distinct
20 × 5 campaign and reference-host repetition remain required before the Stage 1 verdict
can change.
