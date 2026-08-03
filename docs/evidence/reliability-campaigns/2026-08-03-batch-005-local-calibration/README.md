# Reliability Campaign — Batch 005 Local Calibration

**Recorded:** 2026-08-03<br>
**Campaign:** stage1-local-batch-005-v1<br>
**Verdict:** **DEVELOPMENT CALIBRATION PASS**; final Stage 1 reliability gate **NOT PASSED**<br>
**Frozen source revision:** f0ca4e77898371d041f0740c869d9cca1256dc44<br>
**Recorded CLI Git marker:** f0ca4e77898371d041f0740c869d9cca1256dc44+dirty<br>
**Provider cost:** USD 0.00

## Result

The manifest was committed and pushed before execution. Its cumulative 20-fixture,
one-round, zero-retry schedule then completed as declared from a detached checkout of
the frozen source revision:

| Measure | Result |
|---|---:|
| Intended attempts | 20 |
| Terminal attempts | 20 |
| Reconciled attempts | 20 |
| Expected attempts | 20 |
| Infrastructure errors | 0 |
| Observed infrastructure-error rate | 0.0 |
| Threshold in this development manifest | ≤ 0.01 |
| Campaign verdict | campaign_passed: true |

Every attempt produced exactly one isolated 14-event terminal stream with succeeded run
status, passed evaluation outcome, a verified complete ledger/CAS snapshot, healthy
storage, zero retained budget reservation, and no recovery path. The campaign reconciler
audited the complete storage graph before and after terminal readback and verified that
the state copy of the campaign manifest matched the committed source contract.

| Fixture | Seed | Disposition | Semantic digest |
|---|---:|---|---|
| fixture-001-python-addition | 5001 | expected | 00ba261c8b425af9a0dc477ca83fe6ccf41d4702627895c9b13a84621aae32e0 |
| fixture-002-slug-normalization | 5002 | expected | 9a0c8eaceb916bb71b2902b51c13516e9f5b8c0da5bc785d6e8fc9e8d2ff5b1e |
| fixture-003-interval-merge | 5003 | expected | 982870528ce48036b7b1630f8969bf7a2f7fb2de0402bb23f6326d413bb71efb |
| fixture-004-json-pointer | 5004 | expected | 8a689f45d73d7373a1133deb98d3ec6f58faee3cb7c3b7a9fb79f975493ea6aa |
| fixture-005-stable-dedupe | 5005 | expected | bd0ede5ae36f6d4eca7db1b09474ef452ddf66b11594603a3896926d11e239aa |
| fixture-006-run-decoder | 5006 | expected | 9cc947ecb62f512aa9ea2ced8f60407e8172f42557c130bc20c4611c373a1849 |
| fixture-007-apportionment | 5007 | expected | 79d466156815e3d69e2c2619f7e7593d2d157c705992fd5a18943911bf6c335c |
| fixture-008-topological-order | 5008 | expected | 7fbd049b0058c71c286034c6db22426549ee551d47461fe59766d3af876359b8 |
| fixture-009-ordered-changes | 5009 | expected | d01fbd72566618ba79533bb64443387860e7491e1b52e97113aeb9c0041fa278 |
| fixture-010-word-wrap | 5010 | expected | 2e717ba99f106fefbdffe7ed3c61909f0b67fb61126c08c4d1d12f1a74066770 |
| fixture-011-business-days | 5011 | expected | 2d7858e6e210d84656ac3fdd73949b8928486eacdd7f5f6b8a099d886bd068f3 |
| fixture-012-roman-parser | 5012 | expected | c20bbfab63b3e92ee5b76bf975eefb7552db2337db145fff4912b25874f1d584 |
| fixture-013-grid-rotation | 5013 | expected | a1cc5fab929ddc943071920f29261e8c3a6a82f5e78373933837baf063d58482 |
| fixture-014-transaction-summary | 5014 | expected | a0c34790881e929bb96c898aba9084927db2a09fbe86a64390f4e114806c84a8 |
| fixture-015-route-matcher | 5015 | expected | 60c83ca3cc1c0f4e1c585ca29968ffc6a55c0b99ee719bee1cf8a9093ad8cbbb |
| fixture-016-backoff-schedule | 5016 | expected | 48c2d74a68ad34d276ef1c68b2606e402846c58fa8b5166a9497380ac3f7d490 |
| fixture-017-inventory-delta | 5017 | expected | 9f340493e5a18b4622f44e222660a28c9e0c405d03fa648f8c63ddd82c4016f1 |
| fixture-018-latest-versions | 5018 | expected | 3f630da1a75b41a76d93829484d7ee0978d0f561006e152ed43b168566846962 |
| fixture-019-recursive-redaction | 5019 | expected | bd17f6b46eef25d4e7a5c746fe1744c2ddf5bb749a2364d6a81fc9337df6ef4b |
| fixture-020-rule-evaluation | 5020 | expected | 32ecb1b84538d6a261d71b6ebcdd7cc118f9c44e4a04583a68f96d47c7b62686 |

Aggregate scripted usage was 20 model calls, zero retries, 480 uncached input tokens,
320 output tokens, zero tool calls, and zero estimated cost. The fake scripted model
made no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | d9f7f4154617289267c3df6ed20353e9b7269d60ac3690783fbf8fcf9001a79c |
| Parsed campaign manifest | 0411b72a90be17f05e50980450bbb8a448d8715940e6acdf52bf1349b79cf750 |
| Campaign code source | b118246c6b21f4af5b4ea9f845afebb1bd4182a3fd9dfc47958659e7bacd37b5 |
| Report body | ec743636f730d0f5eca83c08a7503f225b87b8929eefc917cfcfcf36c8a0043d |
| Report model/content | 01d4a15218dbb80934636b1687b73ee301ae5e6761d1dc5d3fa9d83e9f1c9498 |
| Exact report file, including final LF | 43e38e69029022d33c2423ef94a225cec4766cb42585f1c27b7e2d5c38e69246 |

The [source manifest](../../../../campaigns/stage1-local-batch-005-v1.json) binds all 20
fixture trees and solution patches, the local evaluator/environment, scripted model,
per-attempt budget, fixed seeds, exact round-major schedule, threshold, and zero-retry
rule. The [canonical report](report.json) retains those declarations plus every terminal
task/model/evaluator/budget, manifest/event-chain, semantic, ledger-snapshot, and storage
commitment.

A separate fresh reconciliation of the preserved SQLite/CAS state, using the report's
original Git marker and timestamp, reproduced the complete report model and canonical
bytes exactly. The raw and checked-in report files also compare byte-for-byte.

The raw attempt state is preserved locally at
runs/reliability-campaigns/2026-08-03-stage1-local-batch-005-v1/state (ignored by Git).
It contains 20 isolated attempt directories and 201 regular files totaling 1,636,092
logical bytes. The canonical report is the durable repository artifact; the ignored raw
state is useful local audit material, not portable versioned evidence.

## Pre-dispatch failures and Git-marker limitation

The first CLI invocation used the same committed manifest but placed its detached
worktree below /tmp. On macOS, /tmp traverses a symlink to /private/tmp. Repository-root
validation rejected that path before campaign state, report output, or any fixture
attempt was created. The accepted run used a canonical /private/tmp worktree. This
pre-dispatch configuration failure is not counted as an attempt or hidden retry.

The accepted report's Git marker ends in +dirty even though execution loaded the
content-bound manifest from a detached checkout at f0ca4e7. The campaign CLI currently
derives that descriptive marker from its ambient process working directory rather than
from --repository-root, and it counts untracked files. The invocation was launched from
the main worktree, where the user's untracked audio summary was present; that file was
not read, staged, or included in the campaign code/fixture identities.

The canonical report retains the marker exactly rather than rewriting history. The
manifest's independent code-source hash and all 20 fixture-tree/patch hashes verified,
so this does not indicate source-byte drift. It is nevertheless a provenance weakness:
this report must not be described as having a clean Git-marker field. The CLI must bind
revision discovery to the declared repository root and require tracked-clean state
before the final 20 × 5 campaign is frozen.

Final repository verification passed **704 tests with 48 declared skips** under the
default no-image configuration and **739 tests with 13 declared skips** using the
preserved Batch 001–003 image plus the current Batch 004–005 image. Every configured
development-container test ran. Ruff reported 206 formatted files and no lint findings;
strict mypy passed across 83 source files. The remaining dual-image skips are 11
reference-host-only cases and two APFS-invalid-name cases.

## Reproduction

The recorded run used a detached checkout at the frozen revision and a locked virtual
environment outside that checkout. Reproduction requires the manifest's bound code and
fixture identities, a canonical real repository path, and new state/report paths:

    uv run --frozen guildmind campaign run campaigns/stage1-local-batch-005-v1.json \
      --repository-root . \
      --state-dir /canonical/new/path/state \
      --output /canonical/new/path/report.json

The output parent must already exist. The command refuses an existing state directory or
output file. Exit 0 means the complete frozen manifest produced a valid report whose
derived development verdict passed; it does not mean the wider Stage 1 gate passed.

## Evidence boundary

This is a **local scripted batch calibration**. It expands the accepted cumulative
campaign path from 17 to all 20 distinct known-outcome tasks and demonstrates that
orchestration, per-attempt isolation, ledger/CAS publication, replay, recursive audit,
reconciliation, and report aggregation continue to work across the complete fixture
corpus.

It is not:

- a container-evaluator campaign—the current aggregate executor intentionally supports
  the trusted local evaluator only;
- rootless x86_64 reference-host evidence;
- the final 20-fixture × 5-round, 100-attempt denominator;
- evidence that the underlying infrastructure-error probability is below 1%;
- a real-provider idempotency, usage, invoice, or cost-reconciliation test; or
- evidence of model capability, because the scripted model returns repository-owned gold
  patches.

With only 20 observations, zero errors gives a point estimate of zero but weak
information about a population rate. The manifest's threshold is an exact development
gate—any one infrastructure error would have failed it—not a confidence claim.

The next checkpoint is to correct and directly test campaign Git provenance, then freeze
the distinct 20-fixture × 5-round schedule before any of its 100 attempts run. Native
rootless x86_64 repetition remains required for the authoritative Stage 1 verdict.
