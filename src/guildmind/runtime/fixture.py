"""Materialize repository-owned fixtures into immutable domain evidence."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from guildmind.domain import (
    ArtifactRef,
    TaskSpec,
    canonical_json,
    canonical_sha256,
    sha256_bytes,
)
from guildmind.evaluation import LocalEvaluationSpec, load_fixture
from guildmind.storage import FileArtifactStore


def materialize_fixture_task(
    fixture_root: Path,
    artifact_store: FileArtifactStore,
    *,
    evaluator_version: str,
    environment_digest: str,
) -> tuple[TaskSpec, LocalEvaluationSpec, str]:
    local_spec = load_fixture(fixture_root)
    raw_manifest = TypeAdapter(dict[str, JsonValue]).validate_python(
        json.loads((fixture_root / "task.json").read_text(encoding="utf-8"))
    )
    problem_statement = raw_manifest.get("problem_statement")
    if not isinstance(problem_statement, str) or not problem_statement.strip():
        raise ValueError("fixture problem_statement must be a non-empty string")

    snapshot = _canonical_tree_snapshot(local_spec.pristine_workspace)
    repository_snapshot = artifact_store.put_text(
        canonical_json(snapshot),
        media_type="application/vnd.guildmind.tree+json",
    )
    problem_artifact = artifact_store.put_text(problem_statement)

    visible_tests: list[ArtifactRef] = []
    raw_visible = raw_manifest.get("visible_test_files", [])
    if not isinstance(raw_visible, list):
        raise ValueError("visible_test_files must be a string list")
    visible_paths: list[str] = []
    for item in raw_visible:
        if not isinstance(item, str):
            raise ValueError("visible_test_files must be a string list")
        visible_paths.append(item)
    for relative in visible_paths:
        visible_path = (fixture_root / relative).resolve()
        if not visible_path.is_relative_to(local_spec.pristine_workspace.resolve()):
            raise ValueError("visible tests must be inside the worker workspace")
        visible_tests.append(
            artifact_store.put_bytes(visible_path.read_bytes(), media_type="text/x-python")
        )

    hidden_hashes: list[JsonValue] = [
        sha256_bytes(path.read_bytes()) for path in local_spec.hidden_test_files
    ]
    identity: dict[str, JsonValue] = {
        "fixture_manifest": raw_manifest,
        "hidden_test_hashes": hidden_hashes,
        "repository_snapshot_sha256": repository_snapshot.sha256,
    }
    task_hash = canonical_sha256(identity)
    task = TaskSpec(
        task_id=local_spec.task_id,
        source="guildmind-fixture-v1",
        split="fixture",
        repository=f"guildmind/fixtures/{fixture_root.name}",
        repository_commit="fixture-v1",
        image_digest=environment_digest,
        task_content_hash=task_hash,
        problem_statement=problem_artifact,
        repository_snapshot=repository_snapshot,
        visible_tests=tuple(visible_tests),
        metadata={
            "evaluator_version": evaluator_version,
            "network_required": False,
        },
    )
    return task, local_spec, problem_statement


def _canonical_tree_snapshot(root: Path) -> dict[str, JsonValue]:
    files: list[dict[str, JsonValue]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"fixture workspace contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        files.append(
            {
                "content_base64": base64.b64encode(data).decode("ascii"),
                "path": relative,
                "sha256": sha256_bytes(data),
                "size_bytes": len(data),
            }
        )
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {"schema_version": "guildmind.tree/v1", "files": files}
    )
