# CAS Temporary-Write Process-Crash Follow-up — 2026-08-03

**Evidence level:** local Darwin development tests with spawned processes and real
POSIX `SIGKILL`<br>
**Scope:** `FileArtifactStore` temporary creation, partial write, and pre-file-`fsync`
process-crash seams<br>
**Stage 1 effect:** closes the three previously missing local CAS temporary-write kill
points; the subsequent bounded cooperative same-digest publisher matrix also passed,
while reference-host validation remains open, so Stage 1 is still **NOT PASSED**

## Claim established

The production [`FileArtifactStore._write_atomic()`](../../../../src/guildmind/storage/artifacts.py)
sequence is:

1. create one exclusive same-shard `.artifact-*` temporary with `tempfile.mkstemp()`;
2. write the complete bytes, flush the Python stream, and file-`fsync` its descriptor;
3. verify the temporary's exact name, regular/single-link identity, stable metadata,
   length, and SHA-256;
4. atomically rename it to the absent canonical digest name without replacement;
5. remove a verified losing publisher's temporary and `fsync` the shard; and
6. reverify the canonical artifact before returning its reference.

The earlier [atomic no-replace publication checkpoint](../2026-08-03-atomic-cas-publication/README.md)
covered three directory-creation durability seams plus the pre-rename, post-rename, and
pre-directory-`fsync` states. It did not distinguish a process dying immediately after
temporary creation, during a write, or after a full userspace flush but before the
temporary file's own `fsync`.

The expanded
[`test_artifact_publication_process_crash.py`](../../../../tests/integration/test_artifact_publication_process_crash.py)
adds those three exact states. A spawned child installs only process-local test wrappers,
performs the relevant real operation, announces the named boundary over a pipe, and
blocks. The parent sends `SIGKILL` only after receiving the exact announcement and
requires exit status `-SIGKILL`. No timing sleep, scheduler guess, production crash hook,
or production-code change is involved.

After every kill, the canonical digest name is absent and the shard contains exactly
one captured `.artifact-*` file. The read-only recursive artifact audit is complete,
permits quarantine, and reports exactly that observed path as one `temp_orphan` with the
exact observed byte length. The temporary is a mode-`0600`, single-link regular file;
its device, inode, mode, link count, size, `mtime_ns`, `ctime_ns`, and content SHA-256
form the evidence snapshot used across retries.

## Exact three-boundary matrix

The enum names and literal boundary values below are taken directly from the test.
"Visible after kill" describes the namespace and pathname-visible bytes observed by the
surviving test process. It is not a power-loss persistence claim.

| Seam | Exact synchronized operation prefix | Visible after `SIGKILL` | Exact audit |
|---|---|---|---|
| `TEMP_CREATED` (`temp_created`) | The wrapper has called the real `mkstemp`, verified prefix `.artifact-`, exact target shard, and matching descriptor/path device and inode, but has not returned `(descriptor, path)` to production. | The kernel closes the killed child's still-open descriptor. Exactly one empty temporary remains; canonical is absent. | One `temp_orphan` at the captured shard-relative path with `size_bytes=0`. |
| `PARTIAL_TEMP_WRITE` (`partial_temp_write`) | Production called `stream.write(full_data)`. The wrapper performed one real buffered write of the fixed nonzero proper prefix, flushed it, read the same bytes through the pathname, and blocked before the production write returned. No file `fsync` occurred. | Exactly one temporary contains the 13 bytes `b"atomic CAS pu"`; canonical is absent. | One `temp_orphan` at the exact captured path with `size_bytes=13`. |
| `PRE_TEMP_FILE_FSYNC` (`pre_temp_file_fsync`) | Production's complete write and stream flush returned. The wrapper intercepted `os.fsync` for the captured descriptor, required size 38, exact full pathname-visible bytes, and matching descriptor/path device and inode, then blocked before calling the real file `fsync`. | Exactly one temporary contains `b"atomic CAS publication crash evidence\n"`; canonical is absent. | One `temp_orphan` at the exact captured path with `size_bytes=38`. |

Temporary names are intentionally random. The tests do not assert a deterministic
temporary filename; they capture the one name actually created, require it to be in the
canonical shard with the `.artifact-` prefix, and use its exact relative path in the
expected finding. Requiring an exact one-entry namespace and an exact one-finding tuple
makes the assertions sensitive to duplicate or unexplained entries.

## Relationship to the existing pre-publication seam

The existing `PRE_PUBLICATION` (`pre_publication`) case blocks on entry to the
no-replace rename. Reaching that call means the real production sequence has already:

- written all 38 bytes;
- flushed the stream;
- returned from `os.fsync(stream.fileno())`;
- closed the stream; and
- completed `_verify_path()` against the temporary's exact name, size, SHA-256,
  regular/single-link status, descriptor/path identity, and stable size and timestamps.

`PRE_TEMP_FILE_FSYNC` is therefore the explicit full-bytes-before-file-`fsync` boundary,
while the existing `PRE_PUBLICATION` case supplies the corresponding
post-file-`fsync`-and-verification coverage. The latter leaves one full-sized temporary
and no canonical name after process death, and its retry still verifies canonical
publication plus stable canonical inode deduplication. Together with the three earlier
directory cases and two later rename cases, the expanded file contains nine collected
process-crash cases.

## Two-retry preservation and publication assertions

Each new case performs the same recovery sequence from the surviving test process:

1. Construct a new `FileArtifactStore` over the crashed shard and snapshot the stranded
   temporary's full filesystem evidence tuple.
2. Publish the original 38 bytes. This first retry creates and verifies its own
   temporary, wins the absent canonical digest name, and returns the expected
   `ArtifactRef`. It does not adopt, rename, modify, or delete the crashed temporary.
3. Require the shard to contain exactly two names: the exact crashed temporary and the
   lowercase SHA-256 canonical name
   `a093f806d29de96d9194efeb273ad36f682726f9fd698ac71e7aca76eac5d8f8`.
4. Require the canonical file to contain the exact original bytes and snapshot its
   device, inode, mode, link count, size, `mtime_ns`, `ctime_ns`, and content hash.
5. Require the crashed temporary's complete snapshot to be unchanged. The audit must
   now report exactly the original `temp_orphan` plus one `valid_finalized_orphan` whose
   expected and observed SHA-256 equal the canonical digest and whose size is 38.
6. Publish the same bytes a second time through the same retry store. The no-replace
   path verifies the existing canonical artifact as valid deduplication and cleans up
   only its new losing temporary.
7. Require the second reference to equal the first, `get_bytes()` to return the exact
   input, the namespace to remain exactly the same two names, both saved evidence tuples
   to remain identical, and the exact audit findings to remain unchanged.

Because this isolated audit is invoked with no ledger roots, the newly published
canonical file is expected to be classified as an ownerless valid-finalized orphan. That
classification is evidence about the test fixture, not a claim that a supported runtime
would leave a successfully committed artifact ownerless.

## Independent review outcome

Independent review found no remaining P0, P1, or P2 evidence-claim gap in these three
additions. The review specifically checked that:

- each barrier follows the claimed real creation, write/flush, or pre-`fsync` seam;
- the exact path, namespace, bytes, size, and finding assertions detect duplicates;
- the stranded temporary and canonical evidence identities survive both retries
  unchanged; and
- the documentation and test module distinguish process crash from power-loss
  durability.

This finding is bounded to the claims above. It does not convert the three tests into a
filesystem persistence proof or a concurrent-publication stress result.

## Verification

Focused run of only the three new parameter cases on the Darwin development host:

```bash
uv run pytest -q \
  tests/integration/test_artifact_publication_process_crash.py \
  -k temporary_write_process_crash
```

```text
3 passed, 6 deselected in 0.38s
```

The complete expanded process-crash file then passed:

```bash
uv run pytest -q tests/integration/test_artifact_publication_process_crash.py
```

```text
9 passed in 0.95s
```

The combined artifact-store unit and process selection was rerun after documentation
integration:

```bash
uv run pytest -q \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py
```

```text
45 passed in 0.97s
```

An independent reviewer reran the whole file with verbose collection and obtained:

```bash
uv run pytest -q tests/integration/test_artifact_publication_process_crash.py -vv
```

```text
9 passed in 0.93s
```

The reviewer also ran:

```bash
uv run ruff check tests/integration/test_artifact_publication_process_crash.py && \
  uv run mypy tests/integration/test_artifact_publication_process_crash.py
```

```text
All checks passed!
Success: no issues found in 1 source file
```

The complete repository gate was then run:

```bash
make check
```

```text
ruff format --check: 105 files already formatted
ruff check: all checks passed
mypy: 72 source files, no issues
pytest: 633 passed, 29 skipped in 13.98s
```

The declared skips are the existing opt-in digest-pinned evaluator-image/reference-host
cases plus the two APFS invalid-filename construction cases. None is one of the nine CAS
process-crash cases.

## Evidence boundary and remaining work

This is **process-crash** evidence only. The pipe proves the named userspace or syscall
prefix happened before a real `SIGKILL`, and the test records what the still-running
kernel exposes afterward. It does not cut power, reboot, force loss of dirty pages,
inspect drive caches, or prove which unsynchronized bytes or directory entries survive a
filesystem, kernel, controller, or host failure. The full bytes observed at
`PRE_TEMP_FILE_FSYNC` are explicitly not file-`fsync`ed. Even bytes observed after the
existing file-`fsync` case do not by themselves establish sudden-power-loss behavior for
the directory entry. Power-cut and storage-fault testing remain separate evidence.

The host guard requires POSIX `SIGKILL` on Darwin or Linux plus a supported atomic
exclusive rename. These cases ran through the Darwin implementation on the local
development host. They do not establish the full production path on the required native
rootless x86_64 Linux reference host, unusual local filesystems, or network filesystems.
The checked pathname and no-follow identity controls also remain a cooperative local
integrity boundary, not protection against an actively racing hostile same-UID process.

The subsequent
[same-digest publisher-contention checkpoint](../2026-08-03-cas-publisher-contention/README.md)
executes the predeclared stress: eight persistent spawned workers over 20 distinct
digest/shard rounds, totaling 160 cooperative low-level puts. Every round gates all
workers at the fully file-`fsync`ed and verified pre-rename boundary, then again after
the real no-replace result but before loser cleanup. Exactly one call wins, the other
seven temporaries retain their complete identities until release, the canonical keeps
the winning inode, all eight calls return the same reference, cleanup leaves no
temporary, and the final audit equals exactly 20 valid-finalized findings. Five
consecutive stabilized repetitions passed. The 8×20 size remains a bounded predeclared
matrix, not a statistically derived reliability threshold.

That bounded low-level same-digest race does not cover hostile same-UID insertion
or pathname replacement, corrupt-target attack composition, hash collisions, winner
death, power loss, or network filesystems. A runtime-level concurrency claim would also
need the workers to traverse the supported shared-maintenance-lease publication entry
point rather than calling `FileArtifactStore` as an uncoordinated trusted primitive.

The Stage 1 reference-host repetition, broader concurrent/open-process recovery work,
real-provider idempotency and reconciliation, hostile-code containment, and predeclared
99% normal-fixture reliability campaign also remain open. This follow-up closes the
three local CAS temporary-write seams identified by
[ADR 0002](../../../decisions/0002-sqlite-and-content-addressed-artifacts.md); it does
not change the overall Stage 1 **NOT PASSED** verdict.
