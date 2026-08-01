"""Typed execution contract shared by production sandbox adapters."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol

_EXECUTION_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIGEST_IMAGE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?/)?"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
_RESERVED_MOUNT_ROOTS = ("/dev", "/proc", "/sys", "/tmp", "/workspace")


class SandboxConfigurationError(ValueError):
    """Raised when a request would weaken or escape the sandbox contract."""


class SandboxUnavailableError(RuntimeError):
    """Raised when the configured host or image cannot satisfy the contract."""


class SandboxStatus(StrEnum):
    """Host-observed outcome of one isolated execution."""

    EXITED = "exited"
    TIMED_OUT = "timed_out"
    OUTPUT_EXHAUSTED = "output_exhausted"
    OOM_KILLED = "oom_killed"
    INFRASTRUCTURE_ERROR = "infrastructure_error"


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Mandatory aggregate limits for one sandbox execution."""

    cpu_cores: float
    memory_bytes: int
    pids: int
    workspace_bytes: int
    temporary_bytes: int
    output_bytes: int
    wall_time_seconds: float

    def __post_init__(self) -> None:
        _require_positive_float("cpu_cores", self.cpu_cores)
        _require_positive_int("memory_bytes", self.memory_bytes)
        _require_positive_int("pids", self.pids)
        _require_positive_int("workspace_bytes", self.workspace_bytes)
        _require_positive_int("temporary_bytes", self.temporary_bytes)
        _require_positive_int("output_bytes", self.output_bytes)
        _require_positive_float("wall_time_seconds", self.wall_time_seconds)


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One host-controlled source exposed read-only inside a sandbox."""

    source: Path
    target: str

    def __post_init__(self) -> None:
        if not self.source.is_absolute():
            raise SandboxConfigurationError("mount source must be an absolute host path")
        source_text = str(self.source)
        if any(character in source_text for character in ("\x00", "\n", ",")):
            raise SandboxConfigurationError("mount source contains an unsupported character")
        _validate_container_path(self.target, label="mount target")
        if any(
            self.target == reserved or self.target.startswith(f"{reserved}/")
            for reserved in _RESERVED_MOUNT_ROOTS
        ):
            raise SandboxConfigurationError(
                "mount target cannot overlap a kernel or writable sandbox mount"
            )


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """Complete, non-extensible input to a sandbox execution.

    The command is always an argument vector. There is deliberately no shell string or
    adapter-specific ``extra_args`` escape hatch.
    """

    execution_id: str
    image: str
    argv: tuple[str, ...]
    limits: SandboxLimits
    working_directory: str = "/workspace"
    environment: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[SandboxMount, ...] = ()

    def __post_init__(self) -> None:
        if _EXECUTION_ID.fullmatch(self.execution_id) is None:
            raise SandboxConfigurationError("execution_id must be a safe lowercase identifier")
        validate_image_reference(self.image)

        argv = tuple(self.argv)
        if not argv:
            raise SandboxConfigurationError("argv must not be empty")
        if not argv[0].startswith("/"):
            raise SandboxConfigurationError("argv[0] must be an absolute executable path")
        if any(not argument or "\x00" in argument for argument in argv):
            raise SandboxConfigurationError("argv values must be non-empty and contain no NUL")
        object.__setattr__(self, "argv", argv)

        _validate_container_path(self.working_directory, label="working_directory")
        if not (
            self.working_directory == "/workspace"
            or self.working_directory.startswith("/workspace/")
        ):
            raise SandboxConfigurationError("working_directory must be inside /workspace")

        normalized_environment: dict[str, str] = {}
        for key, value in self.environment.items():
            if _ENVIRONMENT_KEY.fullmatch(key) is None:
                raise SandboxConfigurationError(f"invalid environment key: {key!r}")
            if not isinstance(value, str) or "\x00" in value:
                raise SandboxConfigurationError(
                    f"environment value for {key!r} must be a NUL-free string"
                )
            normalized_environment[key] = value
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(dict(sorted(normalized_environment.items()))),
        )

        mounts = tuple(self.mounts)
        targets = [mount.target for mount in mounts]
        if len(targets) != len(set(targets)):
            raise SandboxConfigurationError("mount targets must be unique")
        for index, target in enumerate(targets):
            for other in targets[index + 1 :]:
                if target.startswith(f"{other}/") or other.startswith(f"{target}/"):
                    raise SandboxConfigurationError("mount targets must not overlap")
        object.__setattr__(self, "mounts", mounts)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Bounded evidence returned by a sandbox adapter."""

    execution_id: str
    status: SandboxStatus
    exit_code: int | None
    stdout: bytes = b""
    stderr: bytes = b""
    output_truncated: bool = False
    container_id: str | None = None
    image_id: str | None = None
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if self.status is SandboxStatus.EXITED and self.exit_code is None:
            raise ValueError("exited sandbox results require an exit code")
        if self.status is SandboxStatus.OUTPUT_EXHAUSTED and not self.output_truncated:
            raise ValueError("output-exhausted results must be marked truncated")


class Sandbox(Protocol):
    """Fail-closed execution boundary used by workers and evaluators."""

    def run(self, request: SandboxRequest) -> SandboxResult:
        """Execute one complete request without mutating its source mounts."""


def validate_image_reference(reference: str) -> None:
    """Require an immutable repository reference with no mutable tag component."""
    if _DIGEST_IMAGE.fullmatch(reference) is None:
        raise SandboxConfigurationError(
            "image must be a lowercase repository reference pinned only by sha256 digest"
        )


def _require_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SandboxConfigurationError(f"{name} must be a positive integer")


def _require_positive_float(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise SandboxConfigurationError(f"{name} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise SandboxConfigurationError(f"{name} must be a positive finite number")


def _validate_container_path(value: str, *, label: str) -> None:
    if not value or "\x00" in value or "\n" in value or "," in value:
        raise SandboxConfigurationError(f"{label} contains an unsupported character")
    path = PurePosixPath(value)
    if not path.is_absolute() or value == "/" or "//" in value:
        raise SandboxConfigurationError(f"{label} must be a plain absolute container path")
    if any(part in {"", ".", ".."} for part in value.split("/")[1:]):
        raise SandboxConfigurationError(f"{label} must not contain traversal")
