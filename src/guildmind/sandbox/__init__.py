"""Production execution contracts and local workspace preparation helpers.

Only adapters implementing :class:`Sandbox` are execution boundaries. The patch helpers
remain engineering safeguards and must not be used as hostile-code isolation.
"""

from guildmind.sandbox.base import (
    Sandbox,
    SandboxConfigurationError,
    SandboxLimits,
    SandboxMount,
    SandboxRequest,
    SandboxResult,
    SandboxStatus,
    SandboxUnavailableError,
)
from guildmind.sandbox.docker import (
    DockerCleanupEvidence,
    DockerContainerState,
    DockerExecutionEvidence,
    DockerHostAssessment,
    DockerHostMode,
    DockerHostPolicy,
    DockerKillEvidence,
    DockerSandbox,
    ObservedSandboxRun,
    assess_docker_info,
)
from guildmind.sandbox.local import (
    PatchApplyError,
    PatchPolicy,
    PatchValidationError,
    ValidatedPatch,
    copy_and_apply_patch,
    validate_patch,
)
from guildmind.sandbox.resource_probe import (
    ConfigurationVerdict,
    EnforcementVerdict,
    EvidenceTier,
    ResourceProbeKind,
    ResourceProbeSuiteEvidence,
    candidate_resource_limits,
    resource_probe_spec_sha256,
    run_resource_probe_suite,
)
from guildmind.sandbox.selftest import SandboxSelfTestReport, run_sandbox_self_test

__all__ = [
    "ConfigurationVerdict",
    "DockerCleanupEvidence",
    "DockerContainerState",
    "DockerExecutionEvidence",
    "DockerHostAssessment",
    "DockerHostMode",
    "DockerHostPolicy",
    "DockerKillEvidence",
    "DockerSandbox",
    "EnforcementVerdict",
    "EvidenceTier",
    "ObservedSandboxRun",
    "PatchApplyError",
    "PatchPolicy",
    "PatchValidationError",
    "ResourceProbeKind",
    "ResourceProbeSuiteEvidence",
    "Sandbox",
    "SandboxConfigurationError",
    "SandboxLimits",
    "SandboxMount",
    "SandboxRequest",
    "SandboxResult",
    "SandboxSelfTestReport",
    "SandboxStatus",
    "SandboxUnavailableError",
    "ValidatedPatch",
    "assess_docker_info",
    "candidate_resource_limits",
    "copy_and_apply_patch",
    "resource_probe_spec_sha256",
    "run_resource_probe_suite",
    "run_sandbox_self_test",
    "validate_patch",
]
