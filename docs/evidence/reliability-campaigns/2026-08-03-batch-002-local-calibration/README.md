# Reliability Campaign — Batch 002 Local Calibration

**Recorded:** 2026-08-03<br>
**Campaign:** `stage1-local-batch-002-v1`<br>
**Verdict:** **DEVELOPMENT CALIBRATION PASS**; final Stage 1 reliability gate **NOT
PASSED**<br>
**Clean repository revision:** `abed26efe3f51e4cb5e4cb684c8103228400969a`<br>
**Provider cost:** USD 0.00

## Result

The manifest was committed before execution. Its cumulative nine-fixture, one-round,
zero-retry schedule then completed exactly as declared from a detached, tracked-clean
checkout:

| Measure | Result |
|---|---:|
| Intended attempts | 9 |
| Terminal attempts | 9 |
| Reconciled attempts | 9 |
| Expected attempts | 9 |
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
| 001 Python addition | 2001 | `expected` | `05925cacd763bbc2397a786501a30272861be894efce847a20265f4213d512fb` |
| 002 Unicode slug normalization | 2002 | `expected` | `21292dee44fea289b2f1107dd84ea041097d97933d1f2af24adaac8ec857ec69` |
| 003 closed-interval merge | 2003 | `expected` | `040ae021dd51ce342062f18539ce9e8164071e2e05e3839f6fd9872dff15fd59` |
| 004 JSON Pointer traversal | 2004 | `expected` | `09d50f90f672610ff191f9e0a27a2951ee4209a83fcce3efaf43a4a250ace266` |
| 005 structural stable dedupe | 2005 | `expected` | `cd33aad5857cc57145a32e168ce82bfee51bba3447d75170a7e4f420b967b30b` |
| 006 escaped run decoder | 2006 | `expected` | `e66d3fb136b135344a158699a7acdeb53b4a30d2daaf0e224c366ba4433746cc` |
| 007 exact integer apportionment | 2007 | `expected` | `89713c3841b5a2cd846a754df41418db369160c6ea52cb0ae72508cb6a512c38` |
| 008 stable topological order | 2008 | `expected` | `d896e5c7434ab7f22399ad69380385f011b27de6cd20d7c757e69e99add346fc` |
| 009 ordered nested changes | 2009 | `expected` | `db64730c52938cd6ae3a397a391316da0cbfa6adc46e4962908e58740339fd66` |

Aggregate scripted usage was nine model calls, zero retries, 216 uncached input tokens,
144 output tokens, zero tool calls, and zero estimated cost. The fake scripted model made
no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | `b78263f53cd1597ebea69ae7d3d80e094a2503313912ae0922ea98672106f399` |
| Parsed campaign manifest | `17f164177a0d1097b825db3bf2b7e15f3d15ad083265b3a51581e319827c0357` |
| Campaign code source | `481adbbb4b608427ea281ad4d284e9f93a5f17b8aa892fe560c035e09796cfec` |
| Report body | `56124944b8420a5fb36d71392468533edb9e44df430a26dbfd9299974e2b2a3c` |
| Report model/content | `529c7bee1ef3454d7dddeaee014302c1c8f9de03712cb4ce68f55d60643ce223` |
| Exact report file, including final LF | `199ba0638815786b19913574e41da179dddd735975ac43f910a140451d1be19d` |

The [source manifest](../../../../campaigns/stage1-local-batch-002-v1.json) binds all nine
fixture trees and solution patches, the local evaluator/environment, scripted model,
per-attempt budget, fixed seeds, exact round-major schedule, threshold, and zero-retry
rule. The [canonical report](report.json) retains those declarations plus every terminal
task/model/evaluator/budget, manifest/event-chain, semantic, ledger-snapshot, and storage
commitment.

The report loader revalidates its strict schema, derived aggregate claims, and body hash.
A repository test binds the report to the exact checked-in source manifest. A separate
fresh reconciliation of the preserved SQLite/CAS state, using the report's original Git
revision and timestamp, reproduced the complete report model exactly.

The 908 KiB raw attempt state is preserved locally at
`runs/reliability-campaigns/2026-08-03-stage1-local-batch-002-v1/state` (ignored by Git).
It contains nine isolated attempt directories and 91 regular files. The canonical report
is the durable repository artifact; the ignored raw state is useful local audit material,
not portable versioned evidence.

Final repository verification passed **677 tests with 37 declared skips** under the
default no-image configuration and **701 tests with 13 declared skips** using the exact
Batch 001/002 development image. Every configured development-container test ran. Ruff
reported 156 formatted files and no lint findings; strict mypy passed across 83 source
files. The remaining development-image skips are 11 reference-host-only cases and two
APFS-invalid-name cases.

## Reproduction

The recorded run used a detached, tracked-clean checkout at the exact revision above and
a locked virtual environment outside that checkout. Reproduction requires the manifest's
bound code and fixture identities; use the exact tested commit and new canonical real
state/report paths:

```bash
uv run --frozen guildmind campaign run campaigns/stage1-local-batch-002-v1.json \
  --repository-root . \
  --state-dir /canonical/new/path/state \
  --output /canonical/new/path/report.json
```

The command refuses an existing state directory or output file. Exit 0 means the complete
frozen manifest produced a valid report whose derived development verdict passed; it
does not mean the wider Stage 1 gate passed. A source, fixture, patch, evaluator, model,
schedule, budget, retry, or existing-path mismatch fails closed before or during the
declared evidence path.

## Evidence boundary

This is a **local scripted batch calibration**. It expands the accepted campaign path
from five to nine distinct known-outcome tasks and demonstrates that orchestration,
per-attempt isolation, ledger/CAS publication, replay, recursive audit, reconciliation,
and report aggregation continue to work across the second breadth batch.

It is not:

- a container-evaluator campaign—the current aggregate executor intentionally supports
  the trusted local evaluator only;
- rootless x86_64 reference-host evidence;
- the final 20-fixture × 5-round, 100-attempt denominator;
- evidence that the underlying infrastructure-error probability is below 1%;
- a real-provider idempotency, usage, invoice, or cost-reconciliation test; or
- evidence of model capability, because the scripted model returns repository-owned gold
  patches.

With only nine observations, zero errors gives a point estimate of zero but very weak
information about a population rate. The manifest's threshold is an exact development
gate—any one infrastructure error would have failed it—not a confidence claim.

The next corpus checkpoint is a separately reviewed fixtures 010–013 breadth batch. The
eventual acceptance path still requires all 20 fixtures, a separately frozen 100-attempt
schedule, and complete repetition on the native rootless x86_64 reference host.
