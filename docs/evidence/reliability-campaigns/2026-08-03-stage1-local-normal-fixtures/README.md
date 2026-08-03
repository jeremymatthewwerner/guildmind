# Stage 1 Local Normal-Fixture Reliability Campaign

**Recorded:** 2026-08-03<br>
**Campaign:** `stage1-local-normal-fixtures-v1`<br>
**Verdict:** **DECLARED LOCAL DEVELOPMENT GATE PASSED**; authoritative Stage 1 gate **NOT PASSED**<br>
**Frozen source revision:** `e3d0f0c51898f325a2b4a6ddd9ba7475e875950e`<br>
**Provider cost:** USD 0.00

## Result

The manifest was committed and pushed before execution. Its five explicit round-major
passes over all 20 frozen fixtures then ran once from the exact detached, tracked-clean
revision:

| Measure | Result |
|---|---:|
| Fixtures | 20 |
| Rounds per fixture | 5 |
| Intended attempts | 100 |
| Terminal attempts | 100 |
| Reconciled attempts | 100 |
| Expected attempts | 100 |
| Infrastructure errors | 0 |
| Observed infrastructure-error rate | 0.0 |
| Declared threshold | ≤ 0.01 |
| Retries | 0 |
| Campaign verdict | `campaign_passed: true` |

Every attempt produced exactly one isolated 14-event terminal stream with `succeeded`
run status, `passed` evaluation outcome, a verified complete ledger/CAS snapshot,
`healthy` storage, zero retained budget reservation, and no recovery path. The
reconciler audited each complete storage graph before and after terminal readback and
verified the state copy of the campaign manifest.

| Round | Seeds | Attempts | Expected | Events | Input tokens | Output tokens |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 6001–6020 | 20 | 20 | 280 | 480 | 320 |
| 2 | 6021–6040 | 20 | 20 | 280 | 480 | 320 |
| 3 | 6041–6060 | 20 | 20 | 280 | 480 | 320 |
| 4 | 6061–6080 | 20 | 20 | 280 | 480 | 320 |
| 5 | 6081–6100 | 20 | 20 | 280 | 480 | 320 |

| Fixture | Attempts | Expected | Events | Infrastructure errors |
|---|---:|---:|---:|---:|
| fixture-001-python-addition | 5 | 5 | 70 | 0 |
| fixture-002-slug-normalization | 5 | 5 | 70 | 0 |
| fixture-003-interval-merge | 5 | 5 | 70 | 0 |
| fixture-004-json-pointer | 5 | 5 | 70 | 0 |
| fixture-005-stable-dedupe | 5 | 5 | 70 | 0 |
| fixture-006-run-decoder | 5 | 5 | 70 | 0 |
| fixture-007-apportionment | 5 | 5 | 70 | 0 |
| fixture-008-topological-order | 5 | 5 | 70 | 0 |
| fixture-009-ordered-changes | 5 | 5 | 70 | 0 |
| fixture-010-word-wrap | 5 | 5 | 70 | 0 |
| fixture-011-business-days | 5 | 5 | 70 | 0 |
| fixture-012-roman-parser | 5 | 5 | 70 | 0 |
| fixture-013-grid-rotation | 5 | 5 | 70 | 0 |
| fixture-014-transaction-summary | 5 | 5 | 70 | 0 |
| fixture-015-route-matcher | 5 | 5 | 70 | 0 |
| fixture-016-backoff-schedule | 5 | 5 | 70 | 0 |
| fixture-017-inventory-delta | 5 | 5 | 70 | 0 |
| fixture-018-latest-versions | 5 | 5 | 70 | 0 |
| fixture-019-recursive-redaction | 5 | 5 | 70 | 0 |
| fixture-020-rule-evaluation | 5 | 5 | 70 | 0 |

Aggregate scripted usage was 100 model calls, zero retries, 2,400 uncached input tokens,
1,600 output tokens, zero tool calls, and zero estimated cost. The fake scripted model
made no network or provider request.

## Bound identities

| Artifact | SHA-256 |
|---|---|
| Raw source manifest | `147431b91c05ffe21bdd2aaf387c60e15e9d50dfdcb0aceea9a92aade54aa875` |
| Parsed campaign manifest | `84772002ae8c9521db7cc04fc964a46ea2c3a9985fe1f2f0edd48c23a05e86f7` |
| Campaign code source | `c2eb2f15c6681a48b7581dbf1cd6d26afda6ea977e68cee498c185a75246736d` |
| Report body | `68857ccd693d89d4e9197457478df2a7a043889ffc94d20816366c94b0a120ca` |
| Report model/content | `2c0f651a1bd3250049aa0cf3692cc1c51284c07aa1c90903831062566bc705e3` |
| Exact report file, including final LF | `7862236e3b71bcb312c534a52eadd27e6a35c775aac8c272daea4b018f7273ce` |

The [source manifest](../../../../campaigns/stage1-local-normal-fixtures-v1.json) binds
all 20 fixture trees and solution patches, the local evaluator/environment, scripted
model, per-attempt budget, five rounds, fixed seeds, exact round-major schedule, 1%
threshold, and zero-retry rule. The [canonical report](report.json) retains those
declarations plus every terminal task/model/evaluator/budget, manifest/event-chain,
semantic, ledger-snapshot, and storage commitment.

A separate fresh reconciliation of all 100 preserved SQLite/CAS states, using the
report's original Git revision and timestamp, reproduced the complete report model and
canonical bytes exactly. The raw and checked-in report files compare byte-for-byte.

The report records the exact clean commit without a `+dirty` suffix even though the CLI
process was launched from the main worktree containing the user's unrelated untracked
audio summary. Revision discovery was correctly bound to the declared detached
`--repository-root`; that checkout was explicitly checked tracked-clean before dispatch.

The raw attempt state is preserved locally at
`runs/reliability-campaigns/2026-08-03-stage1-local-normal-fixtures-v1/state` (ignored by
Git). It contains 100 isolated attempt directories and 1,001 regular files totaling
8,150,890 logical bytes. The canonical report is the durable repository artifact; the
ignored raw state is useful local audit material, not portable versioned evidence.

The final repository checkpoint passed `ruff format --check` and `ruff check` across
207 files, `mypy` across 83 source files, the default suite with 711 passed and 48
declared skips, and the complete dual-development-image suite with 746 passed and 13
declared skips. The remaining dual-image skips are the 11 reference-image cases and two
APFS-invalid-name cases; they are not silently treated as passes.

## Statistical interpretation

The observed infrastructure-error rate is exactly 0/100, so it clears the manifest's
predeclared empirical threshold of at most 1%. This is an observed campaign gate, not
proof that the underlying infrastructure-error probability is below 1%. Under an
independent Bernoulli interpretation, zero errors in 100 trials gives an exact 95%
one-sided upper bound of about 2.95%; repeated deterministic fixtures may also have
correlated failure modes, making that simple interpretation optimistic.

The result therefore supports the narrower statement: this exact 100-attempt local
schedule completed with no observed infrastructure error. It does not support a stronger
population-reliability or hostile-runtime claim.

## Reproduction

Reproduction requires the manifest's exact bound commit and content identities, a
canonical real repository path, and fresh state/report paths:

    uv run --frozen guildmind campaign run \
      campaigns/stage1-local-normal-fixtures-v1.json \
      --repository-root . \
      --state-dir /canonical/new/path/state \
      --output /canonical/new/path/report.json

The output parent must already exist. The command refuses an existing state directory or
output file, requires the declared repository to be tracked-clean, and ignores unrelated
untracked files when deriving the Git revision. Exit 0 means this frozen development
manifest produced a valid report whose derived gate passed; it does not mean the wider
Stage 1 gate passed.

## Evidence boundary

This is the separately frozen **100-attempt local normal-fixture campaign** required by
the development plan. It is stronger than the prior one-round calibrations, but remains
development evidence because:

- execution used the trusted local evaluator, not a container-evaluator campaign;
- the host is the current macOS development machine, not native rootless x86_64 Linux;
- the scripted model returns repository-owned gold patches, so this is infrastructure
  reliability evidence rather than model-capability evidence;
- it does not exercise real-provider idempotency, usage, invoice, or cost reconciliation;
  and
- the schedule repeats 20 known fixtures and cannot establish independence or general
  population reliability.

The local 100-attempt gate is passed. The authoritative Stage 1 verdict remains **NOT
PASSED** until the complete reference-host containment, resource, recovery, storage,
fixture, and reliability gates are repeated successfully on native rootless x86_64
Linux with their exact immutable evidence.
