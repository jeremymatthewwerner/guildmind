"""Untrusted-side adapter for the bounded ``python-call-v1`` protocol.

This process shares an interpreter with candidate code. Its output is therefore hostile
data, never an evaluator verdict. A separate container scores the response.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

_INPUT_WORKSPACE = Path("/inputs/workspace")
_INPUT_CHALLENGE = Path("/inputs/challenge.json")
_WORK_ROOT = Path("/workspace")
_WORKSPACE = _WORK_ROOT / "repository"
_RESULT_PREFIX = "GUILDMIND_CANDIDATE_RESPONSE="
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_CALLABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_JSON_DEPTH = 32


def _require_plain_tree(root: Path) -> None:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("workspace input must be a real directory")
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if path.name == ".git":
            raise RuntimeError("workspace input contains forbidden Git metadata")
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise RuntimeError("workspace input contains a link or special file")


def _load_challenge() -> tuple[dict[str, Any], bytes]:
    if _INPUT_CHALLENGE.is_symlink() or not _INPUT_CHALLENGE.is_file():
        raise RuntimeError("challenge input must be a regular file")
    data = _INPUT_CHALLENGE.read_bytes()
    raw = json.loads(data)
    if not isinstance(raw, dict):
        raise RuntimeError("challenge must be a JSON object")
    if set(raw) != {"cases", "entrypoint", "protocol", "schema_version"}:
        raise RuntimeError("challenge fields are invalid")
    if raw.get("schema_version") != "guildmind.python-call-challenge/v1":
        raise RuntimeError("challenge schema is invalid")
    if raw.get("protocol") != "python-call-v1":
        raise RuntimeError("challenge protocol is invalid")
    return raw, data


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_json_value(value: Any, *, depth: int = 0) -> None:
    """Require the exact JSON subset before candidate values reach serialization."""

    if depth > _MAX_JSON_DEPTH:
        raise TypeError("candidate return value exceeds the JSON nesting limit")
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return
    if value_type is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("candidate return object keys must be exact strings")
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError(f"candidate return value is not protocol JSON: {value_type.__name__}")


def _emit(payload: dict[str, Any]) -> None:
    encoded = _canonical(payload)
    stream = sys.__stdout__ or sys.stdout
    stream.write(f"{_RESULT_PREFIX}{encoded}\n")
    stream.flush()


def main() -> int:
    _require_plain_tree(_INPUT_WORKSPACE)
    challenge, challenge_bytes = _load_challenge()
    if any(_WORK_ROOT.iterdir()):
        raise RuntimeError("writable workspace is not empty")
    shutil.copytree(_INPUT_WORKSPACE, _WORKSPACE, symlinks=False)
    os.chdir(_WORKSPACE)
    sys.path.insert(0, str(_WORKSPACE))

    entrypoint = challenge["entrypoint"]
    if not isinstance(entrypoint, dict) or set(entrypoint) != {"callable", "module"}:
        raise RuntimeError("challenge entrypoint is invalid")
    module_name = entrypoint.get("module")
    callable_name = entrypoint.get("callable")
    if not isinstance(module_name, str) or _MODULE.fullmatch(module_name) is None:
        raise RuntimeError("challenge module is invalid")
    if not isinstance(callable_name, str) or _CALLABLE.fullmatch(callable_name) is None:
        raise RuntimeError("challenge callable is invalid")

    module = importlib.import_module(module_name)
    target = getattr(module, callable_name)
    if not callable(target):
        raise RuntimeError("challenge entrypoint is not callable")

    cases = challenge["cases"]
    if not isinstance(cases, list):
        raise RuntimeError("challenge cases are invalid")
    results: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"args", "case_id", "kwargs"}:
            raise RuntimeError("challenge case is invalid")
        case_id = case.get("case_id")
        args = case.get("args")
        kwargs = case.get("kwargs")
        if (
            not isinstance(case_id, str)
            or not isinstance(args, list)
            or not isinstance(kwargs, dict)
        ):
            raise RuntimeError("challenge case values are invalid")
        try:
            value = target(*args, **kwargs)
            _validate_json_value(value)
        except BaseException as error:
            results.append(
                {
                    "case_id": case_id,
                    "error_type": type(error).__name__,
                    "kind": "raised",
                }
            )
        else:
            results.append({"case_id": case_id, "kind": "returned", "value": value})

    _emit(
        {
            "challenge_sha256": hashlib.sha256(challenge_bytes).hexdigest(),
            "results": results,
            "schema_version": "guildmind.python-call-response/v1",
        }
    )
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:
        try:
            _emit(
                {
                    "error": f"{type(error).__name__}: {error}",
                    "schema_version": "guildmind.python-call-response/v1",
                }
            )
        finally:
            exit_code = 2
    raise SystemExit(exit_code)
