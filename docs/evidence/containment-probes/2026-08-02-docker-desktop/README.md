# Docker Desktop containment-probe evidence — 2026-08-02

These three canonical reports preserve repeated **development-only** observations of
the evaluator's candidate and scorer containment profiles. They do not satisfy the
Stage 1 reference-host gate, and Stage 1 remains **NOT PASSED**.

## Evidence identity

| Field | Value |
|---|---|
| Host tier | `development` |
| Host policy | `development_only` |
| Reference ready | `false` (`architecture_not_x86_64`, `rootless_required`) |
| Docker environment | Docker Desktop 4.67.0; Engine 29.3.1; Linux arm64 VM; cgroup v2 |
| Evaluator image | `guildmind/evaluator@sha256:31925a81fc6a21a82bcaf2370a6dfa20994a5427180fff8c0a3943d274e960d7` |
| Image platform | `linux/amd64` |
| Image-owned probe SHA-256 | `97004c3494e8a1f1336c0c5034988f0fb11862d192021194e10de060885b858a` |
| Probe specification SHA-256 | `df0fdd6c0cf107c8672dc3498dec431e06d07fc7b6db413a1b11d75e6d4b1ddc` |
| Dockerfile SHA-256 | `7929e07930440550caca5526fb5ef8587361d598dfa0ac9f2a9b5d3a8da5ea68` |
| Recorded code revision | `2e7fa6678de1d79256c5696ed5c2424f59a9db73+dirty` |

Two definition builds used `--platform linux/amd64`, `SOURCE_DATE_EPOCH=0`, and
disabled provenance/SBOM output. Both produced the same repository digest and local
image ID shown above, with a 1970-01-01 creation time. This is a reproducible
development build, not proof of native x86_64 execution or registry preservation.

The dirty suffix is retained deliberately. The reports were generated before the
checkpoint commit while its implementation and documentation, plus a preserved
unrelated untracked audio file, were present. Development evidence permits that
label; reference eligibility requires a clean 40- or 64-hex Git revision.

## Repeated result

All three runs recorded both profiles as `contained`, `all_contained=true`,
`reference_eligible=false`, and `reference_passed=false`. Candidate and scorer each
exited zero without output truncation, and every managed container was removed and
then verified absent.

| Boundary | Observation in every run |
|---|---|
| Profile shape | Candidate received only workspace + challenge; scorer received only challenge + grader + response. A daemon-free drift test also compares these mounts, limits, environment keys, and working directory with real `ContainerEvaluator` requests. |
| Planted secrets | Candidate found its two expected high-entropy file canaries; scorer found its three. Neither profile found any forbidden file or host-environment canary. Host source hashes were unchanged after execution. Only hashes, counts, and dispositions are retained. |
| Mounts | Every expected input was reported read-only and an active write open failed with Linux `EROFS` (errno 30). Every forbidden profile input was absent; mountinfo was complete with zero unexpected `/inputs` mounts. |
| Environment | Candidate reported exactly 12 image/runtime variables; scorer reported those 12 plus the exact 11 evaluator binding keys. Names and value hashes matched the host allowlist with no unexpected variable. |
| Interfaces and routes | Complete scans reported zero usable non-loopback interfaces and zero IPv4/IPv6 default routes; both scan-error flags were false. |
| DNS | All five external, Docker-host, and Kubernetes names returned `EAI_AGAIN` (code -3); none resolved. |
| TCP | External IPv4/IPv6, three metadata endpoints, and two fixed host routes returned `ENETUNREACH` (errno 101). Four loopback service ports returned `ECONNREFUSED` (errno 111). |
| Local sockets | Six known Unix-socket targets were absent; bounded inventories under `/run`, `/var/run`, `/tmp`, `/workspace`, and `/inputs` found zero sockets; `/proc/net/unix` had zero entries. |
| Credentials and privilege | Credential roots were absent or inaccessible (`EACCES` for the three `/root` paths). Raw IPv4 and packet socket creation both failed with `EPERM` (errno 1). |
| Output and checks | Candidate emitted 4,921 bytes and passed 86 derived checks; scorer emitted 6,275 bytes and passed 99. The host strictly parses one bounded LF-terminated record, rejects coercions/extra/duplicate fields, re-derives every check and verdict, and hashes adapter diagnostics. |

The production-shaped request limits were one CPU, 256 MiB memory, 64 PIDs, 64 MiB
workspace, 16 MiB temporary space, 16 KiB combined output, and five seconds wall
time. A separate regression run of the image-owned resource suite on this image
reported matched configuration and `all_enforced=true` for memory, PID, and writable
space.

## Files

The hashes below cover the checked-in file bytes, including the final LF.

| Report | File SHA-256 |
|---|---|
| [`run-01.json`](run-01.json) | `83eb77bdc5175866d1d057274bdd5f81033800762c48918a9f61034d017640cb` |
| [`run-02.json`](run-02.json) | `49c05e584dd74054c546531293a3575524d6efe5bcd3dd7cdb9f6ff652a71913` |
| [`run-03.json`](run-03.json) | `942acb83c2a476615ae91facbe29e1a44dc233823434430874046a0e2e5d2300` |

Fresh random canaries and timestamps intentionally make the transcript and report
hashes differ across repetitions. The contract, requested limits, named checks, and
derived verdicts are identical. Each report round-trips through the strict
`ContainmentProbeSuiteEvidence` model.

## Interpretation boundary

This bundle actively tests the evaluator's candidate and scorer requests. Guildmind
does not yet have a general worker dispatcher, so it is not worker-sandbox evidence.
It proves observations on one rootful ARM Docker Desktop VM, not a universal property
of Docker's `none` network or a defense against kernel/daemon compromise. Negative
network observations are deliberately exact: a non-loopback refusal or timeout would
be inconclusive, not contained; scan errors, malformed output, image-program drift,
or cleanup failure likewise prevent a contained verdict.

The reports are self-consistent content-hashed evidence, not signed attestations. A
trusted build/CI identity or signature remains future work. The complete suite must be
repeated from a clean reviewed revision on the dedicated rootless native x86_64 Linux
host before any report can set `reference_passed=true` or contribute authoritative
hostile-code containment evidence.
