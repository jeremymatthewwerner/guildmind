# ADR 0002: SQLite and Content-Addressed Artifacts

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-07-31

## Context

The first research program runs on one trusted control plane. It needs transactional run state and budget accounting, immutable large artifacts, crash recovery, and complete evidence without prematurely operating a database service.

## Decision

Use SQLite for authoritative metadata, state transitions, events, and budget transactions, plus a filesystem content-addressed store for immutable artifact bytes.

### Write ownership

Exactly one control-plane writer owns SQLite mutations and logical event sequence assignment. Workers and evaluators return typed messages; they never write the database or artifact references directly. Enable foreign keys, WAL mode, a bounded busy timeout, and versioned migrations. Do not place the authoritative database on a network filesystem.

### Artifact identity and commit order

- Address bytes by lowercase SHA-256 and store them under a sharded path such as `sha256/ab/cd/<digest>`.
- Hash opaque artifacts as their exact bytes. Hash structured JSON only after the schema's canonical UTF-8 serialization rules are applied.
- Write a blob to a temporary file on the destination filesystem, flush and `fsync`, verify its digest and length, atomically rename it to the final path, and sync the containing directory before committing a reference.
- In one SQLite transaction, append the event, reconcile the budget reservation, update run state, and add references only to finalized blobs.
- Never overwrite a digest path. Quarantine collisions or mismatched existing bytes as a critical integrity failure.

SQLite event and budget tables are authoritative. `events.jsonl`, Parquet, reports, and OpenTelemetry are reproducible exports, not competing sources of truth. Every started run receives a terminal manifest; unavailable outputs are explicit states such as `not_produced` or `not_run`, not missing rows.

Persist an external-request intent and idempotency key before dispatch. Guildmind promises no duplicate **committed result**, not exactly-once provider execution. Accepted requests without committed responses remain `ambiguous`, with possible spend retained.

Content hashes prove identity, not authorship. Stage approvals and lockbox access require a separate trusted authorization/signature record. Garbage collection is disabled during an active or sealed campaign.

## Recovery and scaling

Startup verifies migration state, SQLite integrity, run-state transitions, artifact references, and digest/length metadata. Orphan temporary files and unreferenced finalized blobs are reported and quarantined; recovery never invents an event or free spend.

Move to PostgreSQL/object storage only when measured concurrent worker demand requires distributed leases or the local durability/throughput envelope is insufficient. That migration requires a new ADR and evidence-preserving import checks.

## Consequences

The initial system is operationally small and supports atomic evidence updates. A single writer limits write throughput by design; evaluation workers may run concurrently while their result messages serialize at commitment. The artifact directory and SQLite database must be backed up and restored as one evidence set.

## Alternatives considered

- Store all payloads in SQLite: simple, but poor for large logs, patches, and future object-storage migration.
- JSONL as the primary store: append-friendly but cannot atomically coordinate events, budget, state, and artifact references.
- PostgreSQL and object storage immediately: useful later, but unnecessary operational surface for the local pilot.

## Acceptance checks

- Kill tests at every blob, request, event, ledger, and transaction boundary leave a reconstructable state with no dangling committed reference or erased spend.
- Concurrent workers cannot assign event sequence numbers or mutate the ledger.
- Partial writes, corrupt bytes, hash mismatches, illegal state transitions, broken previous-event links, orphan files, and absent terminal artifacts are detected.
- JSONL and analysis exports regenerate deterministically from committed state after normalizing declared diagnostic timestamps.
