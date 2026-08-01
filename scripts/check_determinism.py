"""Repeat the fixture runner and require one normalized semantic digest."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from guildmind.models import ScriptedPatchModel
from guildmind.runtime import DeterministicClock
from guildmind.runtime.runner import FixtureRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=100)
    arguments = parser.parse_args()
    if arguments.repetitions <= 0:
        parser.error("--repetitions must be positive")

    repository = Path(__file__).resolve().parents[1]
    fixture = repository / "fixtures" / "001-python-addition"
    digests: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="guildmind-determinism-") as temporary:
        temporary_root = Path(temporary)
        for index in range(arguments.repetitions):
            clock = DeterministicClock(
                started_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index)
            )
            result = FixtureRunner(
                state_directory=temporary_root / f"state-{index:04d}",
                clock=clock,
            ).run(
                fixture_root=fixture,
                model=ScriptedPatchModel(fixture / "solution.patch"),
                run_id=f"determinism-{index:04d}",
                code_revision="determinism-check",
            )
            if result.evaluation.outcome != "passed":
                raise RuntimeError(f"repetition {index} did not pass")
            digests.add(result.semantic_digest)

    summary = {
        "digest": next(iter(digests)) if len(digests) == 1 else None,
        "distinct_digests": len(digests),
        "repetitions": arguments.repetitions,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if len(digests) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
