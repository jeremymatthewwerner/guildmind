# Docker Desktop resource-probe evidence — 2026-08-02

These three canonical reports preserve repeated **development-only** observations from
the fixed Guildmind candidate resource profile. They do not satisfy the Stage 1
reference-host gate.

## Evidence identity

| Field | Value |
|---|---|
| Host tier | `development` |
| Host policy | `development_only` |
| Reference ready | `false` (`architecture_not_x86_64`, `rootless_required`) |
| Evaluator image | `guildmind/evaluator@sha256:ed73d842803e4ef1314da9375dc9ab932b64e818c453e25e949c08fdc28f370a` |
| Image-owned probe SHA-256 | `6a123e58ad47b4fc03079ee770763287c0b7d36a16299a9c646d5bc3dcfcf3f7` |
| Probe specification SHA-256 | `68815367c0231e9d7084bb6400fae0772c3fa2f5db3c87c9f37bc5eaa826fd3f` |
| Recorded code revision | `a583ba227027405888f5e836d235c1780762f2a4+dirty` |

The dirty suffix is retained deliberately. The reports were written after the probe
implementation commit while the explicit evidence-output change and the preserved,
untracked walking-summary audio file were present. Development evidence permits that
label; reference eligibility requires a clean 40- or 64-hex Git revision.

## Repeated result

All three runs recorded `configuration.verdict=matched`, `all_enforced=true`,
`reference_eligible=false`, and `reference_passed=false`.

| Probe | Observation in every run | Verdict |
|---|---|---|
| Memory | Docker state exit 137 with `OOMKilled=true`; no controller kill or probe output | `enforced` |
| PIDs | Limit/current/peak 64; 62 children; next fork `EAGAIN` (Linux errno 11); global and local max-event deltas +1; current returned to 2 | `enforced` |
| `/workspace` | Exactly 67,108,864 bytes written; `ENOSPC` (Linux errno 28); zero free bytes at failure; full recovery after unlink | `enforced` |
| `/tmp` | Exactly 16,777,216 bytes written; `ENOSPC` (Linux errno 28); zero free bytes at failure; full recovery after unlink | `enforced` |
| Cleanup | Configuration plus all three workload containers were removed and then verified absent | confirmed |

## Files

The hashes below cover the checked-in file bytes, including the final LF.

| Report | File SHA-256 |
|---|---|
| [`run-01.json`](run-01.json) | `8968299051d9be4ddc28da54aa1842bec8e3fb896035e42cededf2f85b955a0e` |
| [`run-02.json`](run-02.json) | `a0a203c8c0642c97b137ade682f74cec2fbd633272503f5233fe4ed01ff2ea63` |
| [`run-03.json`](run-03.json) | `0ae2d512a2f356de024e7146fe5220f4800c42f753e7d19b6839ca50235ef5f6` |

Each report embeds the normalized active observations, Docker state, exact requested
limits, transcript hashes and sizes, named checks, and removal/absence evidence. A
candidate or evaluator status is recorded as an observation and is never substituted
for the active enforcement verdict.

## Interpretation boundary

The OOM result now anchors the checked-in `resource-memory-oom` evaluator case, whose
expected candidate status is `oom_killed` and whose scorer is absent. PID and
writable-space enforcement
remain direct probes: Docker exposes no corresponding Guildmind terminal status, so a
generic timeout or functional failure must not be labeled PID or disk exhaustion.

The complete suite must be repeated on the dedicated rootless native x86_64 Linux host
from a clean reviewed revision before any report can set `reference_passed=true`.
