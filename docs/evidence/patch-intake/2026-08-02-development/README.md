# 2026-08-02 unsafe-patch intake evidence

**Scope:** Fixture 001 control-plane patch validation<br>
**Threats:** T-06 and T-07<br>
**Evidence tier:** Host-independent development test; Stage 1 remains **NOT PASSED**

This checkpoint makes unsafe patch intake part of the same content-addressed corpus as
the candidate and scorer attacks. An intake case must terminate as `invalid_patch`
with no candidate transcript, no scorer transcript, and no execution evidence. The
test replaces both Git application and sandbox dispatch with fail-if-called sentinels,
so a passing result proves rejection happened before either boundary.

## Artifact identity

| Artifact | SHA-256 or value |
|---|---|
| Adversarial manifest | `b812231939a9be514a4039b5c2546064a0f1312c10375eae79528e2696365bed` |
| Fixture manifest | `6bb9e01b351eec43237b31a47bf8df501baf00f728982478ef3e4eeda2ce5d9b` |
| Frozen workspace snapshot | `ac562355df8f4cee55b4d4c05f059eb04d57d6a114be340562f690fa1f08f132` |
| Corpus inventory | 19 cases: 9 intake, 3 candidate, 7 scorer |
| Intake byte ceiling | 4,096 bytes |
| Intake file ceiling | 1 file from the exact `README.md`, `addition.py` allowlist |

The code revision is the Git checkpoint containing this report. The manifest hashes
identify raw file bytes, including their final line feeds.

## Pre-application matrix

| Case | Attack shape | Required rejection branch |
|---|---|---|
| `intake-absolute-path` | Captured `/etc/passwd` target | Not a plain relative path |
| `intake-binary` | Git binary payload claiming a 4,294,967,295-byte literal | Binary/extended form unsupported; never decompressed or passed to Git |
| `intake-container-target` | Captured `/inputs/grader/oracle.json` target | Not a plain relative path |
| `intake-file-count` | Two individually allowed files under a one-file ceiling | More than one file |
| `intake-grader-path` | Relative `grader/cases.json` target | Not in the fixture allowlist |
| `intake-oversize` | Syntactically shaped 4,253-byte diff | Larger than the 4,096-byte ceiling |
| `intake-submodule-mode` | Git mode `160000` | Submodule modes unsupported |
| `intake-symlink-mode` | Git mode `120000` | Symlink modes unsupported |
| `intake-traversal` | `../escape.py` target | Traversal component rejected |

All nine cases produced their exact predeclared `invalid_patch` outcome. The dedicated
daemon-free matrix completed as `9 passed`; the complete default suite completed as
`213 passed, 26 skipped`. With the pinned development evaluator image configured, the
complete suite completed as `228 passed, 11 reference-host tests skipped`.

The development image used for the latter run was:

`guildmind/evaluator@sha256:ed73d842803e4ef1314da9375dc9ab932b64e818c453e25e949c08fdc28f370a`

## Parser hardening regressions

The same checkpoint closes and tests two defects found while constructing the matrix:

- patch bytes are now read once through a non-following file descriptor, bounded to
  `max_patch_bytes + 1`, and checked against the inspected inode and post-read file
  metadata; a replacement with a symlink fails closed;
- hunk numbers longer than nine digits are rejected before integer conversion, so a
  5,000-digit count becomes `invalid_patch` instead of escaping as `ValueError`.

Additional tests reject a symlinked patch file, gzip-compressed patch bytes, nested or
case-varied `.git` metadata, and a case-insensitive `.GIT` source directory. Git
metadata checks apply to every path component.

## Evidence boundary

This is control-plane parser evidence, not container-containment evidence. A valid
allowlisted patch can still contain hostile executable code and therefore still
requires the candidate sandbox, sealed scorer, resource limits, and reference-host
campaign. Guildmind accepts only a bounded raw text diff and has no archive or
decompression intake path.

Patch identity mismatch remains a distinct check: exact submitted bytes are bound
before sandbox dispatch, but currently after those already validated bytes are applied
to a disposable temporary copy. This report does not mislabel that identity check as
pre-application evidence.
