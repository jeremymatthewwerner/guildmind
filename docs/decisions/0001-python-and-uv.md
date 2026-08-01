# ADR 0001: Python 3.12 and uv

**Status:** Proposed for Stage 0 approval<br>
**Date:** 2026-07-31

## Context

Guildmind needs a small, inspectable research runtime, strong schema and test tooling, and reproducible local and Linux-container environments. Dependency resolution must not become an unrecorded experimental variable.

## Decision

Use CPython 3.12 for the initial runtime and `uv` for interpreter acquisition, project environments, dependency locking, and command execution.

- `pyproject.toml` is the human-authored dependency and tool configuration.
- `uv.lock` is committed and authoritative for resolved Python dependencies.
- CI and campaign images install with `uv sync --frozen`; ordinary CI never resolves or upgrades dependencies.
- The project supports `>=3.12,<3.13` during the first research program. Every authoritative run records the exact Python patch version, platform, lockfile hash, installed-distribution manifest, and OCI image digest.
- Developer commands run through `uv run`; no activation-dependent shell state is required.
- Dependency changes receive an explicit lockfile diff and invalidate any experimental environment hash that includes the old lock.
- Provider-backed calls do not run in ordinary CI.

The preserved campaign image digest, not a later successful rebuild, identifies the execution environment. Rebuilding from the lock is a separate supply-chain check.

## Consequences

One tool covers bootstrap and locking, while Python retains broad support for schemas, statistics, containers, and provider SDKs. The project accepts a dependency on `uv` and records its version in build provenance. Python patch upgrades are allowed for development only through reviewed lock/image revisions; they do not silently enter a frozen campaign.

## Alternatives considered

- `venv` plus `pip-tools`: viable, but splits environment creation and locking across more commands.
- Poetry or PDM: capable project managers, but add workflow surface Guildmind does not need initially.
- Conda: useful for benchmark-specific environments, but too heavy as the core project environment. A benchmark image may still use Conda internally when its pinned evaluator requires it.

## Acceptance checks

- A clean Linux checkout can run lint, type checks, and tests with only the pinned `uv` bootstrap plus `uv sync --frozen`.
- Modifying `pyproject.toml` without updating the lock fails the frozen sync.
- The run environment manifest records exact interpreter, `uv`, lockfile, distributions, platform, and image identities.
