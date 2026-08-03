# Stage 1 Normal-Fixture Reliability Corpus

## Purpose and evidence boundary

Stage 1 needs a frozen set of small, known-outcome tasks before it can measure
infrastructure reliability. This corpus is deliberately narrower than a capability
benchmark: every task has a repository-owned pristine bug, a reviewed gold patch, a
bounded `python-call-v1` contract, visible failure evidence, and sealed cases. Its job is
to distinguish harness failures from expected task outcomes across different input and
algorithm shapes.

The final development/reference campaign will contain 20 distinct fixtures × 5 explicit
rounds = 100 attempts. That denominator gives an empirical infrastructure-error rate at
one-percentage-point resolution. It does not prove that the underlying rate is below 1%;
such a claim requires a separately preregistered confidence rule and more observations.

Fixture 001 and the one-attempt campaign are accepted development harness evidence.
Fixtures 002–005 now have committed fixture directories and have passed both the
three-repeat trusted-local gate and the three-repeat two-phase development-container gate
for their exact pristine controls and gold patches. A separately frozen five-fixture
batch-calibration campaign also reconciled every attempt as expected with zero
infrastructure errors. Fixtures 001–005 are therefore development-qualified; they still
require inclusion in the final frozen schedule and native rootless x86_64 reference-host
repetition. Fixtures 006–009 now have committed directories and exact controls and have
passed both their three-repeat trusted-local pristine/gold gate and their three-repeat
two-phase development-container gate. A separately committed cumulative campaign then
reconciled all nine fixtures as expected with zero infrastructure errors. Fixtures
001–009 are therefore development-qualified; they still require inclusion in the final
frozen schedule and native rootless x86_64 reference-host repetition. Fixtures 010–013
now have committed directories and exact controls and pass their three-repeat
trusted-local pristine/gold gate and their three-repeat two-phase development-container
gate. A separately committed cumulative campaign then reconciled all 13 fixtures as
expected with zero infrastructure errors. Fixtures 001–013 are therefore
development-qualified; they still require inclusion in the final frozen schedule and
native rootless x86_64 reference-host repetition. Rows 014–020 remain a construction plan.
Locally-qualified, development-qualified, or planned rows may not be counted in the final
campaign denominator, and none of these calibrations may be relabeled as the final Stage 1
denominator.

## Anti-duplication and acceptance rules

A fixture is eligible only if all of the following hold:

1. Its callable contract and dominant bug mechanism are materially different from every
   accepted fixture. Renaming `add` to `sum` or swapping another arithmetic operator does
   not create a new fixture.
2. The batch expands at least one data shape or reasoning mode: scalar, Unicode text,
   sequence, mapping, nested JSON, parser, matrix, graph, date, multiset, ordered
   aggregation, or recursive rule evaluation.
3. At least one visible case fails on the pristine workspace. The complete sealed oracle
   also fails pristine and passes the exact gold patch in three repeated local runs.
4. Sealed cases include boundary and plausible-wrong-implementation discriminators, not
   merely more examples of the visible case. Case IDs, order, count, and expected JSON
   types are frozen.
5. The patch allowlist contains only the implementation files the task actually needs;
   grader paths, tests, configuration, and generated files remain immutable.
6. Task, source tree, gold patch, oracle, evaluator, and eventual attempt schedule are
   content-bound. A byte or mode change creates a new identity and requires new evidence.
7. Local evaluation is trusted-fixture development evidence. Acceptance for the Stage 1
   reference gate additionally requires the exact fixture/gold path through the
   two-phase container evaluator on the dedicated rootless x86_64 host.

## Frozen family matrix

| ID | Callable | Primary semantic / bug class | Principal JSON shape | Key sealed discriminator | Status |
|---|---|---|---|---|---|
| 001 | `add(left, right)` | Signed scalar arithmetic; wrong operator | integer scalars | mixed signs and unbounded integer carry | Development-qualified; reference pending |
| 002 | `slugify(text)` | Unicode-aware normalization and separator collapse | string | punctuation runs, outer separators, Unicode letters | Development-qualified; reference pending |
| 003 | `merge_intervals(intervals)` | Ordering plus closed-integer interval coalescing | nested integer lists | unsorted containment, adjacency, negatives, empty input | Development-qualified; reference pending |
| 004 | `resolve_pointer(document, pointer)` | Escaped-token traversal across maps and arrays | arbitrary nested JSON | RFC 6901 `~0`/`~1`, root pointer, empty key, list index | Development-qualified; reference pending |
| 005 | `dedupe_by(records, key)` | Stable first-wins deduplication by structural JSON identity | list of objects | falsy, list-valued, and object-valued keys while preserving order | Development-qualified; reference pending |
| 006 | `decode_runs(encoded)` | Stateful run-length parser with multi-digit counts | string → list/null | multi-digit runs, escaped literal separators, zero/invalid run policy | Development-qualified; reference pending |
| 007 | `apportion(total, weights)` | Exact largest-remainder integer allocation and deterministic ties | integer + integer list | exact total, zero weights, index tie break, >53-bit values | Development-qualified; reference pending |
| 008 | `topological_order(nodes, edges)` | Graph dependency resolution with stable ready-queue order | strings + edge pairs | disconnected graph, diamond, dynamic lexical tie, cycle result | Development-qualified; reference pending |
| 009 | `apply_changes(document, changes)` | Ordered immutable-style nested state transformation | nested JSON + operations | root/nested set, delete/insert, order-sensitive list indexes | Development-qualified; reference pending |
| 010 | `wrap_words(words, width)` | Greedy formatting with exact whitespace and overflow policy | string list + integer | exact-fit, oversized token, empty input, multiple lines | Development-qualified; reference pending |
| 011 | `business_days(start, end, holidays)` | Date parsing and inclusive/exclusive calendar boundary | ISO strings + list | weekend endpoints, leap day, holiday/weekend overlap | Development-qualified; reference pending |
| 012 | `parse_roman(text)` | Symbol parser with subtractive-pair validation | string | repeated symbols, legal subtraction, canonical rejection result | Development-qualified; reference pending |
| 013 | `rotate_grid(grid)` | Rectangular matrix index transformation | nested lists | non-square matrix, one row/column, empty grid | Development-qualified; reference pending |
| 014 | `summarize_transactions(rows)` | Filtered, grouped, stable aggregation | list of objects | refunds, missing categories, zero amounts, first-seen order | Planned |
| 015 | `match_route(pattern, path)` | Tokenized path matching and parameter extraction | strings → object/null | literal precedence, repeated separators, percent text policy | Planned |
| 016 | `backoff_schedule(base, factor, cap, attempts)` | Bounded deterministic recurrence | numeric scalars | cap crossing, zero attempts, nonintegral factor, no overshoot | Planned |
| 017 | `inventory_delta(before, after)` | Multiset accounting rather than set difference | string lists → object | duplicates, removals/additions of same item, stable keys | Planned |
| 018 | `latest_versions(records)` | Semantic-version precedence and stable record selection | list of objects | prerelease ordering, numeric components, equal-version first win | Planned |
| 019 | `redact_keys(value, blocked)` | Recursive shape-preserving transformation | arbitrary nested JSON | blocked keys below arrays/maps, empty containers, scalar root | Planned |
| 020 | `evaluate_rule(rule, facts)` | Recursive boolean rule tree with explicit missing-fact policy | nested rule object + map | nested all/any/not, falsy facts, missing fact, short-circuit independence | Planned |

## First-batch local qualification

The parameterized integration gate in
[`test_fixture_reliability_corpus.py`](../tests/integration/test_fixture_reliability_corpus.py)
loads fixtures 002–005 from frozen bytes and checks, for each fixture, that:

- the task identity, six-case count, one-file patch allowlist, and hidden-test boundary
  match the checked-in manifest;
- challenge derivation removes every expected value while the sealed oracle retains it,
  and a second load produces identical canonical bytes and hashes;
- the visible test fails on the pristine workspace;
- a semantically pristine control patch returns the same `tests_failed` result in three
  consecutive trusted-local evaluations;
- the exact gold patch returns the same `passed` result in three consecutive
  trusted-local evaluations; and
- evaluation leaves the frozen pristine implementation unchanged.

That is 24 repeated authoritative local evaluations across four distinct semantic
families, plus the four direct visible-failure checks. The complete repository gate at
this checkpoint was 662 passed and 29 declared skips, with Ruff, formatting, and strict
mypy clean. The skips include the digest-pinned development/reference container cases,
so this is deliberately **local qualification**, not container or reference-host
evidence. It used the scripted/local path and incurred no model-provider or deployment
cost.

## First-batch development-container qualification

The exact same four pristine controls and four gold patches then ran three times each
through `ContainerEvaluator` using the rebuilt digest-pinned development image. All 12
pristine-control results were stable `tests_failed` classifications; all 12 gold results
were stable `passed` classifications; every scorer ran all six cases; and there were no
infrastructure errors, skips, truncations, or residual managed containers.

The [development-container evidence](evidence/fixture-qualification/2026-08-03-batch-001-development-container/README.md)
and its self-bound JSON report preserve fixture, source, challenge, oracle, task, limits,
patch, response, completion, image, and evaluation-binding hashes. An always-on unit test
recomputes the source identities and report body; the opt-in live integration test
recomputes every stable result and binding from 48 fresh disposable containers.

The host was rootful Docker Desktop on Apple Silicon, running the linux/amd64 evaluator
under emulation. This is useful development qualification and explicitly not the required
native rootless x86_64 reference-host result.

## First-batch local campaign calibration

The immutable
[`stage1-local-batch-001-v1`](../campaigns/stage1-local-batch-001-v1.json) manifest adds
fixtures 001–005 to one explicit round with seeds 1001–1005, one model call per attempt,
zero retries, and exact code/fixture/patch/evaluator/model/budget identities. It was
committed before execution and run from a detached clean worktree.

All 5/5 declared attempts were terminal, reconciled, expected, replay-valid, and
storage-clean; the aggregate report contained 70 events and zero infrastructure errors.
The [canonical calibration evidence](evidence/reliability-campaigns/2026-08-03-batch-001-local-calibration/README.md)
is independently hash-bound and reloaded by the repository test suite. This proves the
local campaign harness across five semantic families. Five observations are not the
100-attempt Stage 1 denominator and do not establish a population reliability rate.

## Second-batch local and development-container qualification

Fixtures 006–009 add four new implementation and data-shape families:

- a stateful run decoder with multi-digit counts, escaped literal characters, and an
  explicit malformed-input result;
- exact integer Hamilton apportionment with zero weights, stable index ties, and a
  beyond-binary64 discriminator;
- lexicographically minimal topological ordering with a dynamic ready queue and explicit
  cycle result; and
- ordered nested JSON set/delete/insert operations whose paths resolve against the
  result of every preceding change.

Each has one visibly failing pristine implementation, one visible and five hidden tests,
six sealed oracle cases, one exact semantics-preserving pristine control, one one-file
gold patch, and the same bounded `python-call-v1` contract. The shared integration gate
produced three identical `tests_failed` results for every pristine control and three
identical `passed` results for every gold patch: 24 trusted-local evaluations plus four
direct visible-failure checks.

The exact same controls and gold patches then ran three times apiece through the pinned
two-phase development image. All 12 pristine results were stable `tests_failed`
classifications, all 12 gold results were stable `passed` classifications, all six cases
ran in every scorer, and there were no errors, skips, truncations, or residual managed
containers. That adds 24 container results and 48 disposable candidate/scorer
containers.

The self-bound [Batch 002 report](evidence/fixture-qualification/2026-08-03-batch-002-development-container/README.md)
records the exact source revision, image, patch, protocol, response, completion, and
evaluation-binding identities. An always-on verifier recomputes the report and source
identities; an opt-in live test reproduced all result bindings from fresh containers.
The host remained rootful Docker Desktop on Apple Silicon, so native rootless x86_64
reference-host repetition is still pending.

## Second-batch local campaign calibration

The immutable
[`stage1-local-batch-002-v1`](../campaigns/stage1-local-batch-002-v1.json) manifest adds
fixtures 001–009 to one explicit round with seeds 2001–2009, one model call per attempt,
zero retries, and exact code/fixture/patch/evaluator/model/budget identities. It was
committed before execution and run from a detached, tracked-clean checkout.

All 9/9 declared attempts were terminal, reconciled, expected, replay-valid, and
storage-clean; the aggregate report contained 126 events and zero infrastructure errors.
A fresh reconciliation of the preserved raw state reproduced the report exactly. The
[canonical Batch 002 evidence](evidence/reliability-campaigns/2026-08-03-batch-002-local-calibration/README.md)
is strict-schema and hash-bound and is reloaded by the repository test suite. This proves
the local campaign harness across nine semantic families. Nine observations are not the
100-attempt Stage 1 denominator and do not establish a population reliability rate.

## Third-batch local and development-container qualification

Fixtures 010–013 add four further implementation and data-shape families:

- greedy word packing with exact-fit lines, single-space output, and unsplit oversized
  words;
- half-open ISO-date iteration with weekdays, duplicate holidays, weekend overlap, and a
  leap-day holiday;
- canonical uppercase Roman-numeral validation across additive and subtractive forms;
  and
- clockwise index transformation for wide, tall, single-row, single-column, empty, and
  mixed-JSON grids.

Each has one visibly failing pristine implementation, one visible and five hidden tests,
six sealed oracle cases, one exact semantics-preserving pristine control, one one-file
gold patch, and the bounded `python-call-v1` contract. The shared integration gate
produced three identical `tests_failed` results for every pristine control and three
identical `passed` results for every gold patch: 24 trusted-local evaluations plus four
direct visible-failure checks.

The exact same controls and gold patches then ran three times apiece through the pinned
two-phase development image. All 12 pristine results were stable `tests_failed`
classifications, all 12 gold results were stable `passed` classifications, all six cases
ran in every scorer, and there were no errors, skips, truncations, or residual managed
containers. That adds 24 container results and 48 disposable candidate/scorer
containers. The self-bound
[Batch 003 report](evidence/fixture-qualification/2026-08-03-batch-003-development-container/README.md)
records the exact source revision, image, patch, protocol, response, completion, and
evaluation-binding identities. Its static verifier and opt-in live reproduction passed.

The separately committed
[`stage1-local-batch-003-v1`](../campaigns/stage1-local-batch-003-v1.json) manifest froze
fixtures 001–013 into one explicit round with seeds 3001–3013 and zero retries. It ran
from its exact detached, tracked-clean revision before report documentation was added.
All 13/13 declared attempts were terminal, reconciled, expected, replay-valid, and
storage-clean; the aggregate report contained 182 events and zero infrastructure errors.
A fresh reconciliation of the preserved raw state reproduced the report exactly. The
[canonical Batch 003 campaign evidence](evidence/reliability-campaigns/2026-08-03-batch-003-local-calibration/README.md)
is strict-schema and hash-bound and is reloaded by the repository test suite. This proves
the local campaign harness across 13 semantic families. Thirteen observations are not
the 100-attempt Stage 1 denominator and do not establish a population reliability rate.

The development-container host remained rootful Docker Desktop on Apple Silicon, so
native rootless x86_64 reference-host repetition remains pending. No provider or hosted
runtime was used.

## Batch protocol

Fixtures are added in small, reviewable batches. A batch produces:

- immutable fixture directories and gold patches;
- loader/protocol tests and three-repeat pristine/gold local evidence per fixture;
- development two-phase container results when a digest-pinned image is available;
- a new content-bound batch-calibration campaign manifest with fixed attempt IDs, seeds,
  budgets, and zero retries; and
- one canonical campaign report whose denominator contains only accepted fixtures.

Existing manifests are never edited to absorb new fixtures. `stage1-local-smoke-v1`
remains the one-fixture harness identity; each calibration batch and the final 100-attempt
campaign receives a new campaign ID and evidence path.
