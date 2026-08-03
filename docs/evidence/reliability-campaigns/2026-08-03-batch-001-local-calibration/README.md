# Reliability Campaign — Batch 001 Local Calibration

**Recorded:** 2026-08-03<br>
**Campaign:** `stage1-local-batch-001-v1`<br>
**Verdict:** **DEVELOPMENT CALIBRATION PASS**; final Stage 1 reliability gate **NOT
PASSED**<br>
**Clean repository revision:** `fcf50a7224eae6bbb1d331d4cfe6def5a3710688`<br>
**Provider cost:** USD 0.00

## Result

The frozen five-fixture, one-round, zero-retry schedule completed exactly as declared:

| Measure | Result |
|---|---:|
| Intended attempts | 5 |
| Terminal attempts | 5 |
| Reconciled attempts | 5 |
| Expected attempts | 5 |
| Infrastructure errors | 0 |
| Observed infrastructure-error rate | 0.0 |
| Threshold in this development manifest | ≤ 0.01 |
| Campaign verdict | `campaign_passed: true` |

Every attempt produced one isolated 14-event terminal stream with `succeeded` run status,
`passed` evaluation outcome, a verified complete ledger/CAS snapshot, `healthy` storage,
zero retained budget reservation, and no recovery path. The campaign reconciler performed
the full storage audit twice around terminal readback and verified that the state copy of
the campaign manifest still matched the checked-in source contract.

| Fixture | Seed | Disposition | Semantic digest |
|---|---:|---|---|
| 001 Python addition | 1001 | `expected` | `a7501cd11ce53a3534ea6f98c06e2d05d47ea0e07af5735362f9be18e3c5142c` |
| 002 Unicode slug normalization | 1002 | `expected` | `94895ad91d8d26e3409e3f149bdfeb8dca672f67aa48728331fd00fb880cef2b` |
| 003 closed-interval merge | 1003 | `expected` | `f4255e53c0b3bdd6fd9a2913e072afbed71d478ff3a2b398f4f485a7ab68b0ad` |
| 004 JSON Pointer traversal | 1004 | `expected` | `85952df29ee36e1311d14481072ac7f16cdf5f0f102a4901c68516cd0113786d` |
| 005 structural stable dedupe | 1005 | `expected` | `eb4478fce6179b9ff9a56c0b8a41f6a8034dae70011de49cd8d89558bd8146f4` |

Aggregate scripted usage was five model calls, zero retries, 120 uncached input tokens,
80 output tokens, zero tool calls, and zero estimated cost. The fake scripted model made
no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | `63da4da322e4a83531ae7a97324531eded2a7582ad7ad57dd56e2cdfe5333c33` |
| Parsed campaign manifest | `3a1538abb48aead225e6a7e74a820df26fb03926f652a98f6fdbb4661f5b8388` |
| Campaign code source | `76d1cb573efe3d75350bc0b65e2ae1298873930fc36dc184a097b14a9d6dbd28` |
| Report body | `1672bb20ef058b3966666efa3f43f8309916cae6c4ba38ce414b1e7436875874` |
| Report model/content | `92027573e3431357defcac9d4368639a7e911a41f9c046946119d131a795cb85` |
| Exact report file, including final LF | `7140e7044c63a7ac1c1ef81b606a396b09edbc15d7bf01b0d43e8b26c4b29202` |

The [source manifest](../../../../campaigns/stage1-local-batch-001-v1.json) binds all five
fixture trees and solution patches, the local evaluator/environment, scripted model,
per-attempt budget, fixed seeds, exact round-major schedule, threshold, and zero-retry
rule. The [canonical report](report.json) retains those declarations plus each terminal
task/model/evaluator/budget, manifest/event-chain, semantic, ledger-snapshot, and storage
commitment.

The checked-in report loader re-validates its strict schema, derived aggregate claims,
and body hash. A repository test additionally loads both checked-in campaign manifests
against the current content-hashed code and fixture trees.

The 504 KB raw attempt state was preserved locally at
`runs/reliability-campaigns/2026-08-03-stage1-local-batch-001-v1/state` (ignored by Git).
A fresh reconciliation of that preserved SQLite/CAS copy, using the report's original
revision and timestamp, reproduced the checked-in report exactly. The canonical report is
the durable repository artifact; the ignored raw state is useful local audit material,
not portable versioned evidence.

## Reproduction

The recorded run used a detached clean worktree at the exact revision above. Generated
state and output paths were new, canonical real paths outside the repository:

```bash
uv run guildmind campaign run campaigns/stage1-local-batch-001-v1.json \
  --repository-root . \
  --state-dir /canonical/new/path/state \
  --output /canonical/new/path/report.json
```

An initial preflight using macOS's lexical `/var` alias was safely rejected because that
path traverses the `/var` → `/private/var` symlink. It created no campaign state and
dispatched no attempt. The successful invocation used the canonical `/private/var` path;
no safety check was disabled.

The command refuses an existing state directory or output file. Exit 0 means the
complete frozen manifest produced a valid report whose derived verdict passed; it does
not mean the wider Stage 1 gate passed.

## Evidence boundary

This is a **local scripted batch calibration**. It expands the one-fixture smoke from one
to five distinct known-outcome tasks and proves that the campaign orchestration,
per-attempt isolation, ledger/CAS publication, replay, audit, reconciliation, and report
aggregation work across that breadth.

It is not:

- a container-evaluator campaign—the current aggregate executor intentionally supports
  the trusted local evaluator only;
- rootless x86_64 reference-host evidence;
- the final 20-fixture × 5-round, 100-attempt denominator;
- evidence that the underlying infrastructure-error probability is below 1%;
- a real-provider idempotency, usage, invoice, or cost-reconciliation test; or
- evidence of model capability, because the scripted model returns repository-owned gold
  patches.

With only five observations, zero errors gives a point estimate of zero but very weak
information about a population rate. The manifest's threshold acts as an exact
development gate—any one infrastructure error would have failed it—not as a confidence
claim.

The next corpus checkpoint is fixtures 006–009 as a second reviewed breadth batch, while
the eventual acceptance path still requires all 20 fixtures, a separately frozen
100-attempt schedule, and complete repetition on the native rootless x86_64 reference
host.
