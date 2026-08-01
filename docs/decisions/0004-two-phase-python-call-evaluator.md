# ADR 0004: Two-Phase Python-Call Evaluator

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-08-01

## Context

The first container evaluator mounted a patched workspace and grader into one container, then imported candidate Python into the same interpreter as the trusted `unittest` runner. An adversarial patch replaced `unittest.TextTestRunner.run` and was falsely accepted. Candidate import-time code could also read the grader mount. Container restrictions could limit host impact, but no host configuration could make that same-process result trustworthy.

The immediate need is a defensible evaluator for deterministic repository-owned micro-fixtures. It must close the demonstrated result-forgery and grader-read path without pretending to solve arbitrary repository test frameworks.

## Decision

Use two sequential sandbox executions connected only by bounded files written by the trusted host:

1. The **candidate invoke phase** receives a fresh patched workspace and a canonical challenge containing the entrypoint, opaque ordered case IDs, arguments, and keyword arguments. It does not receive the oracle, expected values, grader source, or scorer environment.
2. Candidate stdout is retained as hostile response bytes. A marker or apparent success claim in those bytes has no evaluator authority.
3. The **trusted score phase** receives the canonical challenge, exact bounded response bytes, and sealed oracle. It receives no candidate workspace and imports no candidate module.
4. The host supplies the committed patch artifact SHA-256 to the evaluator, which compares it with the exact validated patch bytes before any candidate/test dispatch. Only a strictly validated score-phase completion record can determine the result. Its evaluation-binding hash commits to the frozen task/source, patch, challenge, response, oracle, pinned image, evaluator version, protocol, expected count, and per-phase limits. Classification and observed counts are validated as exact completion fields and are then covered by the content-hashed evaluation result.
5. Fixture loading freezes the canonical manifest, source tree, visible and hidden tests, and oracle before model dispatch. Evaluation uses those frozen bytes even if the fixture paths later change or disappear.
6. Exact bounded candidate and scorer stdout are retained as named content-addressed evidence artifacts. The public structured result also carries a sanitized classification/count summary and the exact completion-record hash.

The host enforces exact mount allowlists:

| Phase | Required read-only inputs | Forbidden input |
|---|---|---|
| Candidate invoke | `/inputs/workspace`, `/inputs/challenge.json` | `/inputs/grader`, expected outcomes, scorer response path |
| Trusted score | `/inputs/challenge.json`, `/inputs/grader`, `/inputs/response.txt` | `/inputs/workspace`, candidate source |

The first protocol, `python-call-v1`, supports pure function calls whose arguments, keyword arguments, and returned values fit a bounded JSON subset. Oracles use deterministic `case-NNNN` IDs and returned-value expectations. Duplicate JSON keys, non-finite numbers, floats, surrogate code points, coerced Python types such as tuples or non-string-keyed dictionaries, unexpected fields, unordered or missing IDs, count mismatches, and ambiguous completion records fail closed. Protocol records are ASCII-safe and framed only by ASCII line feed.

Both phases currently use the same digest-pinned image for operational simplicity. Candidate access to the generic scorer source is not confidential; the security property comes from the absent oracle and the fresh scorer process/container. Splitting invoke and score into distinct images remains available as defense in depth.

## Security argument

Candidate code can monkeypatch or terminate the invoke adapter and can fabricate any response. That does not create a passing verdict unless the fabricated returned values match the sealed oracle. Candidate-emitted `GUILDMIND_EVALUATION_RESULT` lines remain bytes inside `/inputs/response.txt`; the control plane parses only score-container stdout.

Candidate code necessarily observes the function inputs it is asked to process. This decision protects expected outcomes, grader material, and result integrity; it does not promise that dynamic inputs remain secret from executing code. Repeated pass/fail queries can leak information, so attempt limits and lockbox access rules remain security controls.

The two containers still share the Docker host kernel. Rootless Docker, no network, no credentials, a read-only root, non-root UID, dropped capabilities, `no-new-privileges`, seccomp, resource limits, a dedicated host, and the reference-host adversarial gate remain required.

## Consequences

- The known `unittest` monkeypatch false pass, direct grader-path probe, candidate completion-marker forgery, and empty-response attack are rejected in the development smoke.
- Candidate and scorer failures have different semantics. Candidate timeout, output exhaustion, and OOM remain typed candidate outcomes; scorer sandbox or completion failures are infrastructure errors.
- Exact frozen-task, source, challenge, response, oracle, evaluator, limit, completion, and binding hashes become evaluator evidence; raw bounded transcripts remain available for independent re-scoring.
- Evaluation requires two sandbox startups and can consume up to the declared limit in each phase until an evaluation-wide resource ledger is implemented.
- Unkeyed oracle/task commitments are retained in trusted evidence. They must not be exposed as unrestricted optimizer feedback for low-entropy oracles; a keyed or access-controlled commitment/projection is required before adaptive search.
- Existing local `unittest` evaluation remains a trusted engineering convenience only.
- Arbitrary pytest, command-oriented, stateful, filesystem, service, and repository-wide tasks need separately designed adapters that preserve the same disjoint trust zones. They may not import candidate code into the trusted scorer.

## Alternatives considered

- **A parent wrapper around the existing `unittest` interpreter:** rejected because candidate import-time code still mutates shared modules and can read the grader mount.
- **A child process in the same container:** rejected because it inherits the mount namespace; the current non-root/capability contract has no clean mechanism to let the parent read the grader while denying it to candidate code.
- **Host-only scoring:** viable for a trusted declarative oracle, but the isolated scorer keeps the result producer inside the digest-pinned, resource-bounded evaluator contract and makes its output independently observable.
- **One fresh candidate container per case:** stronger against cross-case state and whole-suite input disclosure, but multiplies startup and complicates aggregate limits. It remains the preferred next refinement if measurement shows the batch challenge creates material leakage risk.
- **Expose hidden `unittest` files read-only:** rejected. Read-only prevents modification, not disclosure or same-interpreter tampering.

## Acceptance checks

- No sandbox request contains both candidate workspace and grader mounts.
- Candidate challenge bytes contain no expected outcomes or grader files.
- Scorer receives exact bounded response bytes and no candidate workspace.
- Gold observations pass; no-op, wrong, tampering, grader-read, malformed-response, completion-forgery, timeout, output, memory, and PID attacks receive their predeclared classifications.
- Missing, duplicate, nonfinal, malformed, extra-field, wrong-count, wrong-binding, or exit/verdict-mismatched scorer completions become infrastructure errors.
- Exact candidate and scorer transcript bytes can be recovered from the evaluation's named CAS evidence and rechecked against their recorded hashes.
- Repeated gold runs with the exact image digest produce the same normalized semantic evidence.
- The complete suite passes on the dedicated rootless x86_64 reference host before this boundary authorizes anything beyond repository-owned fixtures.
