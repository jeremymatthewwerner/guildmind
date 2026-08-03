# Reliability Campaign — Batch 004 Local Calibration

**Recorded:** 2026-08-03<br>
**Campaign:** `stage1-local-batch-004-v1`<br>
**Verdict:** **DEVELOPMENT CALIBRATION PASS**; final Stage 1 reliability gate **NOT
PASSED**<br>
**Clean repository revision:** `a856db8f152e2c37426bde6301a96b34c805e1aa`<br>
**Provider cost:** USD 0.00

## Result

The manifest was committed and pushed before execution. Its cumulative 17-fixture,
one-round, zero-retry schedule then completed exactly as declared from a detached,
tracked-clean checkout:

| Measure | Result |
|---|---:|
| Intended attempts | 17 |
| Terminal attempts | 17 |
| Reconciled attempts | 17 |
| Expected attempts | 17 |
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
| 001 Python addition | 4001 | `expected` | `ec17084773b5d70f68987f58d62e655bf4c02bbad7a406035a8e1655be524979` |
| 002 Unicode slug normalization | 4002 | `expected` | `f2b7709e986ce64c82ec5b4bc063b8c27167cd652f0b7d30148838aa6c1173d6` |
| 003 closed-interval merge | 4003 | `expected` | `1e4516864647a675c1ca0755d665015a2eec786bbf896d2568cb63201ba7c724` |
| 004 JSON Pointer traversal | 4004 | `expected` | `275fcb7802e8ddf7dff9d57d7f5f12a9ecc7fa8d32d7da9af11dd4d369807f6e` |
| 005 structural stable dedupe | 4005 | `expected` | `ccc6dd7082e5d1866d394aec169ef748c3dd38bb1c264170f4834bb847f6b0da` |
| 006 escaped run decoder | 4006 | `expected` | `6fa6614fa58b105653307a5dbc76efb12b7c5f66156970bfd72a40bf77decda5` |
| 007 exact integer apportionment | 4007 | `expected` | `64de40c1b3f121f7e9c75ba6402474d1d51eabf84fd89b1a1dccfa34f2c64b6c` |
| 008 stable topological order | 4008 | `expected` | `243bad29f8e45a04ec6eacf29b55db01ebde477794476549325f9b45d202cea2` |
| 009 ordered nested changes | 4009 | `expected` | `de9a0ddae10a7c77cffdef5af24e82599665979512d969d9e9e1f18623943177` |
| 010 exact-fit word wrap | 4010 | `expected` | `9fa53aefd3c57d454cfce84480fdf35143e9be9f9b551cf32a49e86ce956abb7` |
| 011 half-open business days | 4011 | `expected` | `f50dfd5edab23978cab908ae40cb3b46d942db625f2b91616807d9e1b01cd526` |
| 012 canonical Roman parser | 4012 | `expected` | `d8dc43dbefb30867e8898864df4c0db9064d3b8637b13c8e64f55d759a3c3117` |
| 013 clockwise grid rotation | 4013 | `expected` | `2daa618d4144f370eeae6d929bdaba6f294a6123aae42a0b7fcf1c68cd7f4f59` |
| 014 stable transaction summary | 4014 | `expected` | `ba854b1e65f8199b5fddbaa4310f174c384a29c0c8f06eaaad7fe8c681df3855` |
| 015 raw-segment route matcher | 4015 | `expected` | `3f3ba2d405a12cfde354b1b418f3fcb7869af90a4dbf3a2df2641fc1c686fc25` |
| 016 capped backoff schedule | 4016 | `expected` | `ec4e1ac4f4e1c8d07bf416d1680183b89aef06efe489e67065b65e03e710b3b4` |
| 017 multiset inventory delta | 4017 | `expected` | `a75d0c75e98233171c327db670c08de514a8ed659ba054d27fd886908ce9b59e` |

Aggregate scripted usage was 17 model calls, zero retries, 408 uncached input tokens,
272 output tokens, zero tool calls, and zero estimated cost. The fake scripted model made
no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | `0160cdafef13e7ca4d83577d259505899f31a864487b52e74ce2fa1c6f75d502` |
| Parsed campaign manifest | `0c3d9c1ee5a083c0ffb0ca793cc9c620939dd4bedc34bc8cbc2fc4a961edab13` |
| Campaign code source | `b118246c6b21f4af5b4ea9f845afebb1bd4182a3fd9dfc47958659e7bacd37b5` |
| Report body | `be95b2d233c7e3f4a487011983b9dd1e8bfe9ad5c3eecc3b06a9107304b66131` |
| Report model/content | `8d045437f90dc904334f6ced54a4b1dc379ae3d7dfefa0d67f633fa8cc10dff2` |
| Exact report file, including final LF | `bec5308bdcb10de4dd23017f2c330daafd987d2be5b15f11d621f7ad284bbe57` |

The [source manifest](../../../../campaigns/stage1-local-batch-004-v1.json) binds all 17
fixture trees and solution patches, the local evaluator/environment, scripted model,
per-attempt budget, fixed seeds, exact round-major schedule, threshold, and zero-retry
rule. The [canonical report](report.json) retains those declarations plus every terminal
task/model/evaluator/budget, manifest/event-chain, semantic, ledger-snapshot, and storage
commitment.

The report loader revalidates its strict schema, derived aggregate claims, and body hash.
A repository test binds the report to the exact checked-in source manifest. A separate
fresh reconciliation of the preserved SQLite/CAS state, using the report's original Git
revision and timestamp, reproduced the complete report model and canonical bytes exactly.

The raw attempt state is preserved locally at
`runs/reliability-campaigns/2026-08-03-stage1-local-batch-004-v1/state` (ignored by Git).
It contains 17 isolated attempt directories and 171 regular files totaling 1,386,753
logical bytes. The canonical report is the durable repository artifact; the ignored raw
state is useful local audit material, not portable versioned evidence.

A preliminary CLI invocation used the same committed manifest but encountered a missing
report-parent directory. It failed closed during output-path validation before campaign
state, report output, or any fixture attempt was created. After provisioning only that
exact ignored parent, the recorded run above began. This pre-dispatch configuration
failure is not counted as an attempt or hidden retry.

Final repository verification passed **698 tests with 45 declared skips** under the
default no-image configuration and **730 tests with 13 declared skips** using the
preserved Batch 001–003 image and rebuilt Batch 004 image. Every configured
development-container test ran. Ruff reported 192 formatted files and no lint findings;
strict mypy passed across 83 source files. The remaining dual-image skips are 11
reference-host-only cases and two APFS-invalid-name cases.

## Reproduction

The recorded run used a detached, tracked-clean checkout at the exact revision above and
a locked virtual environment outside that checkout. Reproduction requires the manifest's
bound code and fixture identities; use the exact tested commit and new canonical real
state/report paths:

```bash
uv run --frozen guildmind campaign run campaigns/stage1-local-batch-004-v1.json \
  --repository-root . \
  --state-dir /canonical/new/path/state \
  --output /canonical/new/path/report.json
```

The output parent must already exist. The command refuses an existing state directory or
output file. Exit 0 means the complete frozen manifest produced a valid report whose
derived development verdict passed; it does not mean the wider Stage 1 gate passed. A
source, fixture, patch, evaluator, model, schedule, budget, retry, or existing-path
mismatch fails closed before or during the declared evidence path.

## Evidence boundary

This is a **local scripted batch calibration**. It expands the accepted cumulative
campaign path from 13 to 17 distinct known-outcome tasks and demonstrates that
orchestration, per-attempt isolation, ledger/CAS publication, replay, recursive audit,
reconciliation, and report aggregation continue to work across the fourth breadth batch.

It is not:

- a container-evaluator campaign—the current aggregate executor intentionally supports
  the trusted local evaluator only;
- rootless x86_64 reference-host evidence;
- the final 20-fixture × 5-round, 100-attempt denominator;
- evidence that the underlying infrastructure-error probability is below 1%;
- a real-provider idempotency, usage, invoice, or cost-reconciliation test; or
- evidence of model capability, because the scripted model returns repository-owned gold
  patches.

With only 17 observations, zero errors gives a point estimate of zero but weak information
about a population rate. The manifest's threshold is an exact development gate—any one
infrastructure error would have failed it—not a confidence claim.

The next corpus checkpoint is to implement and qualify fixtures 018–020. The eventual
acceptance path still requires all 20 fixtures, a separately frozen 100-attempt schedule,
and complete repetition on the native rootless x86_64 reference host.
