# Reliability Campaign — Batch 003 Local Calibration

**Recorded:** 2026-08-03<br>
**Campaign:** `stage1-local-batch-003-v1`<br>
**Verdict:** **DEVELOPMENT CALIBRATION PASS**; final Stage 1 reliability gate **NOT
PASSED**<br>
**Clean repository revision:** `4630c7db191d0cc2093ae84a0b288aaa337d3633`<br>
**Provider cost:** USD 0.00

## Result

The manifest was committed and pushed before execution. Its cumulative 13-fixture,
one-round, zero-retry schedule then completed exactly as declared from a detached,
tracked-clean checkout:

| Measure | Result |
|---|---:|
| Intended attempts | 13 |
| Terminal attempts | 13 |
| Reconciled attempts | 13 |
| Expected attempts | 13 |
| Infrastructure errors | 0 |
| Observed infrastructure-error rate | 0.0 |
| Threshold in this development manifest | ≤ 0.01 |
| Campaign verdict | `campaign_passed: true` |

Every attempt produced exactly one isolated 14-event terminal stream with `succeeded`
run status, `passed` evaluation outcome, a verified complete ledger/CAS snapshot,
`healthy` storage, zero retained budget reservation, and no recovery path. The campaign
reconciler audited the complete storage graph before and after terminal readback and
verified that the state copy of the campaign manifest matched the committed source
contract.

| Fixture | Seed | Disposition | Semantic digest |
|---|---:|---|---|
| 001 Python addition | 3001 | `expected` | `0d888b2550e56f613e9832c86fd52271cd64427b1bd0425c430981247c4b576e` |
| 002 Unicode slug normalization | 3002 | `expected` | `46c31ef75aca3b09dc8be026bfa5059e9dd8161493ac8b233da9f14ad8f6e907` |
| 003 closed-interval merge | 3003 | `expected` | `d32a47b53028cac4256c4fec2a56b636f12fd3782aef9ca79c1987b6589e0239` |
| 004 JSON Pointer traversal | 3004 | `expected` | `4e71edca91e2071d9ebcfd661d82d6187852d21c862cd1a66691cd58c3fd3ee8` |
| 005 structural stable dedupe | 3005 | `expected` | `bc2728725cf7da48b1b6299ce9210fb6ac9f2efa1d8a9fcf16a7454e7eacb61d` |
| 006 escaped run decoder | 3006 | `expected` | `6ed8713575e88f6fa9fe8f5c20b917eca3fbd0e59760de9b5977a58d23d636e3` |
| 007 exact integer apportionment | 3007 | `expected` | `b2aa2292ad07feb392d468fad99b9ee4e2741696b64147d7971e13c6572effca` |
| 008 stable topological order | 3008 | `expected` | `0bf8c267c336779882a7deefa884224eb70d0ef24389344e78899d837f7f38a6` |
| 009 ordered nested changes | 3009 | `expected` | `2731c799fb227d848dd5d53d1bfcca6213e79995b7af394efe489afd6d71e432` |
| 010 exact-fit word wrap | 3010 | `expected` | `1dded17441eaa245d23bc443e90e0fec9fabf537f27bd66e83a7317303b2b250` |
| 011 half-open business days | 3011 | `expected` | `247624ac67ca7b6306ed0e6912e3577cc109f21f2aa888c6907498efcc88788c` |
| 012 canonical Roman parser | 3012 | `expected` | `8afad75a9cc3fcd914d19602611e18c354d9b0c7c68e1997c3de493685574988` |
| 013 clockwise grid rotation | 3013 | `expected` | `3801242d9f3c33b3f5079328452f25f674393cf3ce2415a225996dd466c7a21a` |

Aggregate scripted usage was 13 model calls, zero retries, 312 uncached input tokens,
208 output tokens, zero tool calls, and zero estimated cost. The fake scripted model made
no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | `076bdc730a2cbaf7f20340efa76f3484be30207e13f16779860f4689a0349cd1` |
| Parsed campaign manifest | `79221b17ab955dea6e75367023b9948c65872fd02a45d74bcf525d1e64a88c12` |
| Campaign code source | `481adbbb4b608427ea281ad4d284e9f93a5f17b8aa892fe560c035e09796cfec` |
| Report body | `a24c599a17a4cb59444b6c8c4067614ae3e69c8302f50d6ca2277253dc46a01e` |
| Report model/content | `60ae57903797898f9f61b3847a32d407711650a0ac53a1e0a82c388a36eee963` |
| Exact report file, including final LF | `7783f740dc4d6f2888016d1ccc80fe839dd6a3e26f5cda720f81657f02cbadfc` |

The [source manifest](../../../../campaigns/stage1-local-batch-003-v1.json) binds all 13
fixture trees and solution patches, the local evaluator/environment, scripted model,
per-attempt budget, fixed seeds, exact round-major schedule, threshold, and zero-retry
rule. The [canonical report](report.json) retains those declarations plus every terminal
task/model/evaluator/budget, manifest/event-chain, semantic, ledger-snapshot, and storage
commitment.

The report loader revalidates its strict schema, derived aggregate claims, and body hash.
A repository test binds the report to the exact checked-in source manifest. A separate
fresh reconciliation of the preserved SQLite/CAS state, using the report's original Git
revision and timestamp, reproduced the complete report model exactly.

The raw attempt state is preserved locally at
`runs/reliability-campaigns/2026-08-03-stage1-local-batch-003-v1/state` (ignored by Git).
It contains 13 isolated attempt directories and 131 regular files totaling 1,056,575
logical bytes. The canonical report is the durable repository artifact; the ignored raw
state is useful local audit material, not portable versioned evidence.

Final repository verification passed **684 tests with 41 declared skips** under the
default no-image configuration and **712 tests with 13 declared skips** using the exact
Batch 001/002/003 development image. Every configured development-container test ran.
Ruff reported 174 formatted files and no lint findings; strict mypy passed across 83
source files. The remaining development-image skips are 11 reference-host-only cases and
two APFS-invalid-name cases.

## Reproduction

The recorded run used a detached, tracked-clean checkout at the exact revision above and
a locked virtual environment outside that checkout. Reproduction requires the manifest's
bound code and fixture identities; use the exact tested commit and new canonical real
state/report paths:

```bash
uv run --frozen guildmind campaign run campaigns/stage1-local-batch-003-v1.json \
  --repository-root . \
  --state-dir /canonical/new/path/state \
  --output /canonical/new/path/report.json
```

The command refuses an existing state directory or output file. Exit 0 means the complete
frozen manifest produced a valid report whose derived development verdict passed; it does
not mean the wider Stage 1 gate passed. A source, fixture, patch, evaluator, model,
schedule, budget, retry, or existing-path mismatch fails closed before or during the
declared evidence path.

## Evidence boundary

This is a **local scripted batch calibration**. It expands the accepted cumulative
campaign path from nine to 13 distinct known-outcome tasks and demonstrates that
orchestration, per-attempt isolation, ledger/CAS publication, replay, recursive audit,
reconciliation, and report aggregation continue to work across the third breadth batch.

It is not:

- a container-evaluator campaign—the current aggregate executor intentionally supports
  the trusted local evaluator only;
- rootless x86_64 reference-host evidence;
- the final 20-fixture × 5-round, 100-attempt denominator;
- evidence that the underlying infrastructure-error probability is below 1%;
- a real-provider idempotency, usage, invoice, or cost-reconciliation test; or
- evidence of model capability, because the scripted model returns repository-owned gold
  patches.

With only 13 observations, zero errors gives a point estimate of zero but weak information
about a population rate. The manifest's threshold is an exact development gate—any one
infrastructure error would have failed it—not a confidence claim.

The next corpus checkpoint is the separately reviewed fixtures 014–017 breadth batch.
The eventual acceptance path still requires all 20 fixtures, a separately frozen
100-attempt schedule, and complete repetition on the native rootless x86_64 reference
host.
