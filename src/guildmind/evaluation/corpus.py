"""Strict loader for checked-in evaluator adversarial corpora."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from guildmind.domain import sha256_bytes
from guildmind.evaluation.local import EvaluationStatus, FixtureConfigurationError

_SCHEMA_VERSION = "guildmind.adversarial-corpus/v1"
_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PATCH_FILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.patch$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_THREAT_ID = re.compile(r"^T-[0-9]{2}$")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_PATCH_BYTES = 1_048_576
_MAX_CASES = 1_000

AttackClass = Literal["boundary_integrity", "functional_control", "resource_exhaustion"]
EvaluationPhase = Literal["intake", "candidate", "scorer"]
ScorerClassification = Literal["candidate_failed"]


@dataclass(frozen=True, slots=True)
class AdversarialExpectation:
    """Exact evaluator outcome required for one adversarial patch."""

    evaluation_status: EvaluationStatus
    phase: EvaluationPhase
    scorer_classification: ScorerClassification | None
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class AdversarialCase:
    """One content-addressed patch and its predeclared expected outcome."""

    case_id: str
    patch_path: Path
    patch_sha256: str
    attack_class: AttackClass
    threat_ids: tuple[str, ...]
    expected: AdversarialExpectation


@dataclass(frozen=True, slots=True)
class AdversarialCorpus:
    """A complete checked-in patch corpus loaded from one strict manifest."""

    manifest_path: Path
    manifest_sha256: str
    cases: tuple[AdversarialCase, ...]


def load_adversarial_corpus(manifest_path: Path) -> AdversarialCorpus:
    """Load and verify one complete, closed adversarial patch manifest."""

    manifest_path = manifest_path.absolute()
    root = manifest_path.parent
    try:
        root_mode = root.lstat().st_mode
    except OSError as error:
        raise FixtureConfigurationError(
            f"adversarial corpus directory is unavailable: {root}"
        ) from error
    if root.is_symlink() or not stat.S_ISDIR(root_mode):
        raise FixtureConfigurationError("adversarial corpus must be a real directory")

    manifest_data = _read_regular_file(
        manifest_path,
        label="adversarial corpus manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    raw = _strict_json_object(manifest_data)
    if set(raw) != {"schema_version", "cases"}:
        raise FixtureConfigurationError(
            "adversarial corpus must contain exactly schema_version and cases"
        )
    if raw.get("schema_version") != _SCHEMA_VERSION:
        raise FixtureConfigurationError("adversarial corpus uses an unsupported schema")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > _MAX_CASES:
        raise FixtureConfigurationError(
            "adversarial corpus cases must be a non-empty bounded array"
        )

    cases = tuple(_load_case(raw_case, root=root) for raw_case in raw_cases)
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise FixtureConfigurationError("adversarial corpus case IDs must be unique")
    if case_ids != sorted(case_ids):
        raise FixtureConfigurationError("adversarial corpus cases must be ordered by case_id")
    patch_names = [case.patch_path.name for case in cases]
    if len(patch_names) != len(set(patch_names)):
        raise FixtureConfigurationError("adversarial corpus patch files must be unique")

    try:
        discovered = {path.name for path in root.iterdir() if path.suffix == ".patch"}
    except OSError as error:
        raise FixtureConfigurationError("cannot enumerate adversarial corpus patches") from error
    listed = set(patch_names)
    if listed != discovered:
        missing = sorted(discovered - listed)
        absent = sorted(listed - discovered)
        details: list[str] = []
        if missing:
            details.append(f"unlisted patches: {', '.join(missing)}")
        if absent:
            details.append(f"missing patches: {', '.join(absent)}")
        raise FixtureConfigurationError(
            f"adversarial corpus patch inventory does not match ({'; '.join(details)})"
        )

    return AdversarialCorpus(
        manifest_path=manifest_path,
        manifest_sha256=sha256_bytes(manifest_data),
        cases=cases,
    )


def _load_case(raw: object, *, root: Path) -> AdversarialCase:
    if not isinstance(raw, dict) or set(raw) != {
        "attack_class",
        "case_id",
        "expected",
        "patch_file",
        "patch_sha256",
        "threat_ids",
    }:
        raise FixtureConfigurationError("adversarial corpus case fields are invalid")

    case_id = raw.get("case_id")
    if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
        raise FixtureConfigurationError("adversarial corpus case_id is invalid")
    patch_file = raw.get("patch_file")
    if not isinstance(patch_file, str) or _PATCH_FILE.fullmatch(patch_file) is None:
        raise FixtureConfigurationError("adversarial corpus patch_file is invalid")
    declared_sha256 = raw.get("patch_sha256")
    if not isinstance(declared_sha256, str) or _SHA256.fullmatch(declared_sha256) is None:
        raise FixtureConfigurationError("adversarial corpus patch_sha256 is invalid")
    attack_class = raw.get("attack_class")
    if attack_class not in {
        "boundary_integrity",
        "functional_control",
        "resource_exhaustion",
    }:
        raise FixtureConfigurationError("adversarial corpus attack_class is invalid")

    threat_ids_raw = raw.get("threat_ids")
    if not isinstance(threat_ids_raw, list) or not threat_ids_raw:
        raise FixtureConfigurationError("adversarial corpus threat_ids must be non-empty")
    threat_ids: list[str] = []
    for threat_id in threat_ids_raw:
        if not isinstance(threat_id, str) or _THREAT_ID.fullmatch(threat_id) is None:
            raise FixtureConfigurationError("adversarial corpus threat_id is invalid")
        threat_ids.append(threat_id)
    if len(threat_ids) != len(set(threat_ids)) or threat_ids != sorted(threat_ids):
        raise FixtureConfigurationError("adversarial corpus threat_ids must be unique and ordered")

    patch_path = root / patch_file
    patch_data = _read_regular_file(
        patch_path,
        label=f"adversarial patch {patch_file}",
        max_bytes=_MAX_PATCH_BYTES,
    )
    observed_sha256 = sha256_bytes(patch_data)
    if observed_sha256 != declared_sha256:
        raise FixtureConfigurationError(
            f"adversarial patch digest mismatch for {patch_file}: "
            f"expected {declared_sha256}, observed {observed_sha256}"
        )

    return AdversarialCase(
        case_id=case_id,
        patch_path=patch_path,
        patch_sha256=declared_sha256,
        attack_class=cast(AttackClass, attack_class),
        threat_ids=tuple(threat_ids),
        expected=_load_expectation(raw.get("expected")),
    )


def _load_expectation(raw: object) -> AdversarialExpectation:
    if not isinstance(raw, dict) or set(raw) != {
        "evaluation_status",
        "output_truncated",
        "phase",
        "scorer_classification",
    }:
        raise FixtureConfigurationError("adversarial corpus expected fields are invalid")
    status_raw = raw.get("evaluation_status")
    if not isinstance(status_raw, str):
        raise FixtureConfigurationError("adversarial corpus evaluation_status is invalid")
    try:
        status = EvaluationStatus(status_raw)
    except ValueError as error:
        raise FixtureConfigurationError(
            "adversarial corpus evaluation_status is unsupported"
        ) from error
    phase = raw.get("phase")
    if phase not in {"intake", "candidate", "scorer"}:
        raise FixtureConfigurationError("adversarial corpus phase is invalid")
    scorer_classification = raw.get("scorer_classification")
    if scorer_classification not in {None, "candidate_failed"}:
        raise FixtureConfigurationError("adversarial corpus scorer_classification is invalid")
    output_truncated = raw.get("output_truncated")
    if type(output_truncated) is not bool:
        raise FixtureConfigurationError("adversarial corpus output_truncated is invalid")

    if phase == "intake":
        if status is not EvaluationStatus.INVALID_PATCH or scorer_classification is not None:
            raise FixtureConfigurationError(
                "intake-phase corpus expectation has inconsistent status or classification"
            )
    elif phase == "candidate":
        if (
            status
            not in {
                EvaluationStatus.TIMED_OUT,
                EvaluationStatus.OUTPUT_EXHAUSTED,
                EvaluationStatus.OOM_KILLED,
            }
            or scorer_classification is not None
        ):
            raise FixtureConfigurationError(
                "candidate-phase corpus expectation has inconsistent status or classification"
            )
    elif status is not EvaluationStatus.TESTS_FAILED or scorer_classification != "candidate_failed":
        raise FixtureConfigurationError(
            "scorer-phase corpus expectation has inconsistent status or classification"
        )
    if output_truncated is not (status is EvaluationStatus.OUTPUT_EXHAUSTED):
        raise FixtureConfigurationError(
            "adversarial corpus output_truncated does not match evaluation_status"
        )

    return AdversarialExpectation(
        evaluation_status=status,
        phase=cast(EvaluationPhase, phase),
        scorer_classification=cast(ScorerClassification | None, scorer_classification),
        output_truncated=output_truncated,
    )


def _strict_json_object(data: bytes) -> dict[str, object]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    def object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        raw = json.loads(
            data,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise FixtureConfigurationError(
            f"adversarial corpus manifest must be strict UTF-8 JSON: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise FixtureConfigurationError("adversarial corpus manifest must contain an object")
    return cast(dict[str, object], raw)


def _read_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise FixtureConfigurationError(f"cannot inspect {label}: {path}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise FixtureConfigurationError(f"{label} must be a regular non-symlink file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise FixtureConfigurationError(f"cannot open {label}: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise FixtureConfigurationError(f"{label} changed while loading")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(max_bytes + 1)
    except OSError as error:
        raise FixtureConfigurationError(f"cannot read {label}: {path}") from error
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise FixtureConfigurationError(f"{label} exceeds its byte limit")
    return data
