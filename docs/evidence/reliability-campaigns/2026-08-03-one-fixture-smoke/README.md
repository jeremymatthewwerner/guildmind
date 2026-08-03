# One-Fixture Reliability-Campaign Smoke — 2026-08-03

**Evidence tier:** development<br>
**Campaign:** `stage1-local-smoke-v1`<br>
**Implementation checkpoint:** `b2fbfc8` (`Add frozen reliability campaign runner`)<br>
**Result:** 1 intended attempt, 1 reconciled terminal attempt, 1 expected result,
0 infrastructure errors; harness verdict `campaign_passed=true`<br>
**Stage 1 effect:** none; the authoritative gate remains **NOT PASSED**

## Purpose

This is the first end-to-end exercise of Guildmind's versioned reliability-campaign
contract. It proves that one repository-owned fixture can be bound to an exact source,
fixture tree, gold patch, evaluator environment, model adapter, budget, seed, schedule,
and zero-retry rule; executed through the public CLI; reconciled against its isolated
ledger/CAS evidence; and published as one canonical no-replace report.

It is a harness acceptance smoke. It is not the planned 20-fixture × 5-round campaign,
not reference-host evidence, and not a statistical claim that an underlying reliability
rate is at least 99%.

## Frozen inputs

| Identity | Value |
|---|---|
| Source manifest | [`campaigns/stage1-local-smoke-v1.json`](../../../../campaigns/stage1-local-smoke-v1.json) |
| Raw source-manifest SHA-256 | `41931eae8bcdef92bef5eff601f4a5f6f83917963ba3bb125bd8c3d1c4ba8fc2` |
| Parsed campaign-manifest SHA-256 | `2cbbbe88366b28eb534b9965e630f40c0962484e2d363ea43d981c41d0db6fef` |
| Code-source SHA-256 | `76d1cb573efe3d75350bc0b65e2ae1298873930fc36dc184a097b14a9d6dbd28` |
| Fixture-tree SHA-256 | `4265c6d84cea1446996dcd946353482e2fa5dfe9f0f749f44ee63c835454355b` |
| Gold-patch SHA-256 | `9d22ab07f9a9d126aecfe0935848b3e5614f9e114749240405d70a843261ef8b` |
| Evaluator | `guildmind/local-fixture-v1` |
| Environment | `sha256:4b5272a947d6b4d85d99cef7beb6221a52cf97f47d91c7c6f0c7a12ddc1076fc` |
| Model adapter | `guildmind/fake-scripted-patch-v1` |
| Attempt | `stage1-local-smoke-r001-fixture-001`, round 0, seed 0 |
| Retry authority | campaign retry limit 0; model retry budget 0 |

The code-source identity covers every regular file under `src/guildmind` plus
`pyproject.toml` and `uv.lock`, including file modes and paths. The fixture-tree identity
covers every regular file under `fixtures/001-python-addition`, including the sealed
grader, adversarial corpus, and gold patch.

## Invocation

The run used local Python 3.12.13 through `uv` 0.11.2 on arm64 macOS 26.5.2. It made no
provider calls and required no Docker or hosted service. Reproduction requires a clean
checkout of implementation checkpoint `b2fbfc8`; the current evolving source is expected
to fail the manifest's pre-dispatch code-identity check.

```bash
uv run guildmind campaign run campaigns/stage1-local-smoke-v1.json \
  --repository-root . \
  --state-dir runs/reliability-campaigns/2026-08-03-stage1-local-smoke-v1/state \
  --output docs/evidence/reliability-campaigns/2026-08-03-one-fixture-smoke/report.json
```

Both state and report paths were new. The CLI checked report availability before
dispatch, created one isolated attempt state, executed each declared attempt exactly
once, and published the report by same-directory no-replace rename.

## Result and independent readback

The canonical [report](report.json) validated through
`load_reliability_campaign_report` after publication. A separate storage audit of the
attempt state reported `healthy`, `storage_clean=true`, and
`references_verified=true`.

| Observation | Value |
|---|---|
| Intended / terminal / reconciled attempts | `1 / 1 / 1` |
| Attempt disposition | `expected` |
| Terminal run status / evaluation outcome | `succeeded / passed` |
| Infrastructure errors / rate | `0 / 0.0` |
| Event count | `14` |
| Semantic digest | `9d91ab94bccf8ad49e6cbc201210d88e2f7205c5d88ba89bd3f617a84d8c917c` |
| Task-content SHA-256 | `58b15c126402eb300b2104ce51cabcc4ef3fee4e91fde3b8ea2a8ba1e81a601e` |
| Report-body SHA-256 | `36fe9e4c4815d0f05868ca7cd7ecc5dc748bee531cf4bbae9c2701d869169dd6` |
| Canonical report-content SHA-256 | `ea4c3610fbe3bb2acd61cf0a969240b5206a3e422ac81199277cbef915b9fb9c` |
| Checked-in report-file SHA-256, including final LF | `0b00da84dd8c1dd504d72db95517a0a4786dff3f795fba0cd605214b970c2a19` |

The report records Git revision
`b2fbfc83bd8df96a8f62e6d259214ade4e5c3155+dirty`. At dispatch, the tracked tree was
exactly checkpoint `b2fbfc8`; the generic Git-status probe also saw the pre-existing
user-owned untracked audio file `guildmind-progress-summary.m4a`. That unrelated file is
not under the code-source inventory. More importantly, the loader and final reconciler
both independently matched the exact code-source digest above, so this development
result is bound to the intended executable source bytes without relabeling the Git
observation as clean.

## Test evidence

Twenty-four new focused tests passed before this smoke:

- strict model and loader rejection of unknown/duplicate fields, unsafe paths, duplicate
  fixture/attempt identities, incomplete schedules, retries, reference/local mismatch,
  and changed code, fixture, or patch bytes;
- successful execution and canonical report readback;
- derived-claim, body-hash, and duplicate-JSON-key tampering;
- execution-exception recovery and retained infrastructure classification;
- budget refusal as a complete unexpected result rather than an infrastructure error;
- missing, nonterminal, undeclared/malformed run-set, and corrupt-ledger reconciliation;
  and
- CLI success, valid failed-gate exit 2, and existing-output refusal before state
  creation.

The complete repository gate immediately before checkpoint `b2fbfc8` reported:

```text
ruff format --check: 112 files already formatted
ruff check: all checks passed
mypy: 77 source files, no issues found
pytest: 658 passed, 29 declared skips in 15.47s
```

## Evidence boundary and next denominator

One successful attempt has no power to establish a population reliability bound. The
declared 1% threshold is mechanically exercised here—any single infrastructure error
would have produced a 100% observed rate and a failed harness verdict—but the denominator
is intentionally only one.

The next evidence-bearing work is to add distinct known-outcome fixtures in reviewed
batches, prove each pristine fixture fails and gold patch passes, then freeze a separate
20-fixture × 5-round manifest. That 100-attempt campaign will report an empirical
infrastructure-error rate at one-percentage-point resolution. Even a 0/100 result will
not by itself prove that the underlying rate is below 1%; any confidence claim requires
its own preregistered statistical rule and a larger denominator.

The ignored local state retains the full SQLite ledger and CAS for this machine. The
checked-in report is self-contained at the manifest, result, identity, count, and digest
level, but it does not embed every referenced artifact byte and is not a signed remote
attestation.
