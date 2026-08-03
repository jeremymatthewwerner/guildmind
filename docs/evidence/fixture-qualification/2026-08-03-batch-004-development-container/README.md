# Fixture Batch 004 — Development-Container Qualification

**Recorded:** 2026-08-03<br>
**Batch:** `stage1-fixture-batch-004` (fixtures 014–017)<br>
**Verdict:** **DEVELOPMENT PASS**; reference-host gate **NOT RUN / NOT PASSED**<br>
**Repository revision under test:**
`7c9eebaf293ea088db07fdaa9daf8441c21a0b00`<br>
**Evaluator:** `guildmind/container-python-call-v2`<br>
**Image:**
`guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e`

## Result

All four fixed fourth-batch fixtures matched their declared outcome in three consecutive
two-phase container evaluations per outcome:

| Fixture | Sealed cases | Pristine-control result ×3 | Gold result ×3 | Infrastructure errors |
|---|---:|---|---|---:|
| 014 stable transaction summary | 6 | `tests_failed` (5/6 mismatches) | `passed` (6/6) | 0 |
| 015 raw-segment route matcher | 6 | `tests_failed` (3/6 mismatches) | `passed` (6/6) | 0 |
| 016 capped backoff schedule | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |
| 017 multiset inventory delta | 6 | `tests_failed` (4/6 mismatches) | `passed` (6/6) | 0 |

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
`0c7c64f6292addc629e6480f79a20cc902b8b6889bcc143af854bec527853f13`.
The complete canonical file including its final LF has SHA-256
`d706fce22dcfcd58fe841b746b11d9bceaf9f2acbb5038798fb6f27a5e7770bd`.

## What was exercised

The four fixture families are deliberately different:

- signed transaction aggregation with missing-category filtering, refunds, retained zero
  groups, and first-seen order;
- slash-token route matching with exact literals, parameter extraction, repeated
  separator normalization, and raw percent text;
- deterministic multiplicative backoff with zero attempts, a nonintegral factor, exact
  cap saturation, and no overshoot; and
- stable multiset inventory accounting with repeated additions/removals and unchanged
  reorderings.

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

Fixture 016 is the first accepted case with finite JSON floating-point inputs and return
values. The host loader, candidate adapter, and scorer all reject nonfinite numbers. The
candidate emitted bounded canonical finite-number results; the disjoint scorer compared
them to the sealed oracle and passed all six cases. This closes the earlier unit-only
boundary for finite numbers on this development image.

The report was emitted by the repository-owned recorder only after requiring a
tracked-clean revision. It uses canonical JSON, a same-directory atomic no-replace
publication, file and parent-directory synchronization, and a self-bound body digest.
An always-on unit test independently recomputes the report and current fixture/protocol
identities. The opt-in live integration test recomputes every stable result, response,
completion, and evaluation-binding digest from fresh containers.

## Reproducible image and host facts

The evaluator was rebuilt because the candidate and scorer programs gained finite-number
support. Two consecutive builds from revision `7c9eeba` used the exact checked-in recipe,
`linux/amd64`, the pinned Python base, disabled provenance and SBOM output, and
`SOURCE_DATE_EPOCH=0`. Both produced the same manifest/image digest:

```text
sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e
```

The relevant checked-in build identities are:

| Input | SHA-256 |
|---|---|
| `Dockerfile` | `7929e07930440550caca5526fb5ef8587361d598dfa0ac9f2a9b5d3a8da5ea68` |
| `invoke.py` | `35b43d0e7d3ff0dd1734e33746dca4323fc6c1169df91a9bacbdb2ce259e98dd` |
| `score.py` | `fdb8c31068de2116a0c3377c70a910faa675fe3eed452efa19a5eb21a8c50e48` |
| `containment_probe.py` | `97004c3494e8a1f1336c0c5034988f0fb11862d192021194e10de060885b858a` |
| `resource_probe.py` | `6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7` |
| Pinned base manifest | `d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b` |

Inspection recorded Linux/amd64, UID/GID `65532:65532`, working directory `/workspace`,
the trusted scorer entry point, and no declared volumes. The host remained macOS 26.5.2
on Apple Silicon with Docker Desktop server 29.3.1. Its daemon is a rootful `aarch64`
Linux VM, so `guildmind doctor` continues to report `architecture_not_x86_64` and
`rootless_required`. Development policy permits this for finding fixture/evaluator
defects; reference policy correctly rejects it.

This new digest does not rewrite the accepted Batch 001–003 image evidence. Those reports
remain bound to
`guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7`.
Live verification therefore uses separate explicit environment variables for the
historical and current digests.

## Reproduction and verification

From a clean checkout of the recorded revision, build twice and require the same digest:

```bash
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  --build-arg SOURCE_DATE_EPOCH=0 --load \
  --tag guildmind/evaluator:stage1-batch004-build-a containers/evaluator

docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
  --build-arg SOURCE_DATE_EPOCH=0 --load \
  --tag guildmind/evaluator:stage1-batch004-build-b containers/evaluator
```

Then record into a new canonical path:

```bash
uv run python scripts/record_fixture_container_batch.py \
  --batch-id stage1-fixture-batch-004 \
  --image guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e \
  --output /new/canonical/path/report.json \
  --recorded-on 2026-08-03 \
  --repetitions 3 \
  014-transaction-summary \
  015-route-matcher \
  016-backoff-schedule \
  017-inventory-delta
```

Independent repository verification uses:

```bash
export GUILDMIND_HISTORICAL_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7'
export GUILDMIND_DEVELOPMENT_EVALUATOR_IMAGE='guildmind/evaluator@sha256:5fbe7aaa9fb81a28482cdce0a7b47a2fe4272e9b351ae5255683e12734c1959e'

uv run pytest -q tests/unit/test_fixture_batch_evidence.py
uv run pytest -q tests/integration/test_fixture_reliability_container.py -k fourth
make check
```

The static verifier passed all four checked reports. The focused Batch 004 live verifier
passed 4/4 cases with 12 historical cases deselected in 20.36 seconds. The complete
default repository gate passed with Ruff clean across 191 files, strict mypy clean across
83 source files, and 696 pytest cases passed with 45 declared skips in 20.22 seconds. The
complete dual-image pytest gate then passed 728 cases with 13 declared skips in 119.26
seconds. All 16 configured historical/current development-container cases ran; the only
remaining skips were 11 reference-host-only cases and two APFS-invalid-name cases.

No model provider, hosted runtime, cloud deployment, or paid inference was used.

## Evidence boundary and next gate

This artifact proves deterministic expected classifications for four small,
repository-owned JSON-call fixtures on one development host. It is **not**:

- rootless x86_64 reference-host evidence;
- the cumulative fixtures 001–017 campaign or its transactional ledger/CAS report;
- the final 20-fixture × 5-round reliability denominator;
- a confidence-bound claim that infrastructure reliability exceeds 99%;
- general arbitrary-repository or hostile-command containment; or
- evidence about provider idempotency, billing, or real-model capability.

The next checkpoint is to freeze a new content-bound cumulative Batch 004 manifest for
fixtures 001–017 without modifying any accepted historical manifest, then run its exact
zero-retry schedule from a clean revision into new local state/report paths.
Reference-host repetition remains required before the Stage 1 verdict can change.
