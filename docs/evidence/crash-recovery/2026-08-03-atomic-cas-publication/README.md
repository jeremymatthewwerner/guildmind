# Atomic No-Replace CAS Publication — 2026-08-03

**Evidence level:** local Darwin development tests plus a cached Linux/arm64 container
syscall smoke test<br>
**Scope:** `FileArtifactStore` publication and exact process-crash boundaries<br>
**Stage 1 effect:** closes the two-hard-link publication window; the broader recovery
gate remains **NOT PASSED**

## Claim established

`FileArtifactStore` now publishes a flushed and byte-verified temporary file with one
kernel-level, atomic, no-replace rename:

- Darwin calls libc `renamex_np(..., RENAME_EXCL)`;
- Linux calls libc `renameat2(..., RENAME_NOREPLACE)`; and
- every other platform, a missing libc symbol, an unsupported filesystem, or any
  syscall failure other than `EEXIST` fails closed with `ArtifactCorruptionError`.

There is deliberately no hard-link fallback. The former link-then-unlink sequence could
leave both the temporary name and canonical name attached to the same inode if the
publisher died between those operations. That state conflicted with Guildmind's
single-link ownership rule and caused an otherwise valid finalized blob to audit as a
hard-link integrity failure. Atomic rename changes the namespace from one temporary name
to one canonical name without exposing that two-link state.

The surrounding durability protocol remains:

1. require the controlled store root, `sha256`, and shard components to have their
   exact on-disk `DirEntry.name`, never merely a case-insensitive spelling alias;
2. `fsync` each controlled entry's immediate parent even when this invocation inherited
   the directory, repairing a creator killed after `mkdir` but before parent `fsync`;
3. create a same-directory exclusive temporary file;
4. write, flush, and file-`fsync` it;
5. verify its exact on-disk name, size, SHA-256, stable inode metadata, regular-file type,
   and single link;
6. publish without replacing an existing canonical entry;
7. remove a verified losing publisher's temporary entry and `fsync` the shard directory;
   and
8. require the exact lowercase digest entry and verify its bytes and identity before
   returning an `ArtifactRef`.

Only directory entries lexically below the resolved trusted base are unconditionally
parent-synchronized. Preexisting host ancestors above that boundary are not needlessly
opened and synchronized; a directory created there by this invocation retains the
existing create-and-sync behavior. The caller remains responsible for supplying a
durable trusted base.

An `EEXIST` result is not accepted as proof of valid deduplication. The losing temporary
is retained until the preexisting canonical target passes the same byte, exact-name, and
identity verification. A valid target permits temporary cleanup and directory sync. A
corrupt file, spelling alias, symlink, directory, or hard-linked target is rejected
without replacement; the already-fsynced verified temporary is preserved and its parent
is synchronized as recoverable evidence.

## Real-process crash matrix

`tests/integration/test_artifact_publication_process_crash.py` uses the multiprocessing
`spawn` start method. Test-only wrappers announce an exact boundary over a pipe and block;
the parent sends POSIX `SIGKILL` after receiving that announcement. No production crash
hook or timing sleep is used.

| Kill boundary | Namespace after restart | Recursive-audit classification | Retry |
|---|---|---|---|
| root `mkdir` returned, before parent `fsync` | empty controlled root | no finding | synchronizes the root parent before descending or publishing |
| `sha256` `mkdir` returned, before root `fsync` | empty `sha256` directory | no finding | synchronizes root before creating/using the shard or publishing |
| shard `mkdir` returned, before `sha256` `fsync` | empty shard | no finding | synchronizes `sha256` before temporary creation or publication |
| immediately before no-replace rename | canonical absent; one single-link temporary | `temp_orphan` | publishes canonical bytes; repeated retry preserves its inode |
| immediately after successful rename, before it returns | canonical present; temporary absent; one link | `valid_finalized_orphan` | verified dedupe preserves the winning inode |
| after rename returns, immediately before directory `fsync` | canonical present; temporary absent; one link | `valid_finalized_orphan` | verified dedupe preserves the winning inode |

For each interrupted `mkdir`, the parent test restarts the store and wraps directory
`fsync` plus the no-replace rename. Publication is allowed only after observing the
ordered parent syncs for root, `sha256`, and shard; it then produces a single-link valid
finalized orphan. This establishes the repair ordering without a production fault hook.

The focused verification command was:

```bash
uv run ruff format --check \
  src/guildmind/storage/artifacts.py \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py
uv run ruff check \
  src/guildmind/storage/artifacts.py \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py
uv run mypy \
  src/guildmind/storage/artifacts.py \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py
uv run pytest -q \
  tests/unit/test_artifact_store.py \
  tests/integration/test_artifact_publication_process_crash.py
```

Result on the Darwin development host:

```text
42 passed in 0.66s
```

The unit cases include actual host operation, successful publication, valid dedupe,
preexisting corrupt-target rejection, injected syscall `EEXIST` and non-`EEXIST`
mapping, unsupported-platform refusal, prepublication byte verification, publication
races, exact-name enforcement for controlled root/`sha256`/shard/temporary/digest entries,
actual uppercase aliases on the default case-insensitive macOS filesystem, inherited
directory parent-sync ordering, preserved verified evidence beside a corrupt target, and
an `os.link` tripwire proving the publication path does not hard-link.

The broader storage, recursive-integrity, coordinator, and guarded-recovery selection
reported:

```text
125 passed, 1 skipped in 1.13s
```

The skip is the preexisting case where the macOS filesystem refuses creation of an
invalid UTF-8 filename.

The subsequent full repository gate reported:

```text
527 passed, 28 skipped in 7.03s
```

The skips require opt-in digest-pinned development/reference evaluator images or the
Linux-host invalid-UTF-8 filename case; none is an atomic-publication test skip on this
Darwin host.

The production helper's Linux branch was also loaded from the worktree and exercised
inside the already-cached `guildmind/evaluator-resource-probe:local` image with networking
disabled, a read-only root filesystem, and a temporary `/tmp`. Docker reported
`linux/arm64`; the probe published one source, rejected a second source without replacing
the target, preserved both sets of bytes, and observed a single link:

```text
linux renameat2 no-replace: passed
```

## Subsequent temporary-write crash matrix

The follow-up [CAS temporary-write checkpoint](../2026-08-03-cas-temporary-write/README.md)
adds the three userspace prefixes that were still open here: immediately after the real
temporary creation, after a fixed proper prefix has been written and flushed, and after
the full bytes have been flushed but immediately before file `fsync`. Each killed
temporary remains an exact typed audit finding across two retries while the first retry
publishes one canonical blob and the second preserves its inode. Together with this
checkpoint's pre-publication case—which is after file `fsync`, close, and byte/identity
verification—the local CAS publication process-crash sequence is covered from temporary
creation through final directory sync. Real-process competing-publisher stress,
power-loss durability, and rootless x86_64 reference-host repetition remain separate.

## Evidence boundary

The full test suite and synchronized kill matrix executed the Darwin branch on this host.
The Linux/arm64 smoke probe executed the exact production no-replace helper, but loaded
the module with a minimal domain stub because that narrow evaluator image does not contain
Guildmind's Pydantic dependency. It therefore establishes the local container's libc
`renameat2` path, not full Linux `FileArtifactStore` integration. The rootless x86_64
Linux reference environment remains required. Filesystem support is intentionally
detected by attempting the exclusive rename and failing closed; Guildmind does not
silently weaken publication on older kernels, libc implementations, network filesystems,
or unusual mounts.

`SIGKILL` is a process-crash test, not a power-loss or storage-controller durability
test. In particular, seeing a newly created directory or canonical entry after a kill
before its parent `fsync` does not prove that entry would survive sudden power loss. The
restart cases establish that an observed inherited entry is synchronized before reuse,
and the ordinary protocol performs all required directory `fsync` calls before return.
Power-cut/fault-injection validation remains a separate reference-host gate.

The rename still uses checked pathnames rather than descriptor-relative traversal. The
existing no-follow, directory-identity, regular-file, and hard-link checks remain in
force, but they do not protect against an actively racing hostile process with the same
OS identity. Authoritative maintenance still requires the documented quiescent,
exclusive-writer boundary.
