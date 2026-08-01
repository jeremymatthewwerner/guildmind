from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from guildmind.evaluation import (
    EvaluationStatus,
    FixtureConfigurationError,
    load_adversarial_corpus,
)

_REPOSITORY_ROOT = Path(__file__).parents[2]
_CORPUS_ROOT = _REPOSITORY_ROOT / "fixtures" / "001-python-addition" / "adversarial"


def test_checked_in_adversarial_corpus_is_complete_and_content_addressed() -> None:
    corpus = load_adversarial_corpus(_CORPUS_ROOT / "corpus.json")

    assert len(corpus.manifest_sha256) == 64
    assert [case.case_id for case in corpus.cases] == [
        "boundary-completion-forgery",
        "boundary-empty-response",
        "boundary-grader-read",
        "boundary-unittest-tampering",
        "functional-no-op",
        "functional-visible-only",
        "functional-wrong-operation",
        "resource-output-bomb",
        "resource-timeout",
    ]
    assert {case.patch_path.name for case in corpus.cases} == {
        path.name for path in _CORPUS_ROOT.glob("*.patch")
    }
    assert all(case.patch_path.is_file() for case in corpus.cases)
    assert all(case.threat_ids for case in corpus.cases)

    output_case = next(case for case in corpus.cases if case.case_id == "resource-output-bomb")
    assert output_case.expected.evaluation_status is EvaluationStatus.OUTPUT_EXHAUSTED
    assert output_case.expected.phase == "candidate"
    assert output_case.expected.output_truncated
    assert output_case.expected.scorer_classification is None


def test_adversarial_corpus_rejects_patch_digest_drift(tmp_path: Path) -> None:
    corpus_root = tmp_path / "adversarial"
    shutil.copytree(_CORPUS_ROOT, corpus_root)
    (corpus_root / "no-op.patch").write_bytes(b"tampered patch bytes\n")

    with pytest.raises(FixtureConfigurationError, match="digest mismatch"):
        load_adversarial_corpus(corpus_root / "corpus.json")


def test_adversarial_corpus_rejects_unlisted_patch_files(tmp_path: Path) -> None:
    corpus_root = tmp_path / "adversarial"
    shutil.copytree(_CORPUS_ROOT, corpus_root)
    (corpus_root / "unlisted.patch").write_bytes((corpus_root / "no-op.patch").read_bytes())

    with pytest.raises(FixtureConfigurationError, match="unlisted patches"):
        load_adversarial_corpus(corpus_root / "corpus.json")


def test_adversarial_corpus_rejects_symlinked_patch_files(tmp_path: Path) -> None:
    corpus_root = tmp_path / "adversarial"
    shutil.copytree(_CORPUS_ROOT, corpus_root)
    patch = corpus_root / "no-op.patch"
    patch.unlink()
    patch.symlink_to(corpus_root / "wrong-operation.patch")

    with pytest.raises(FixtureConfigurationError, match="regular non-symlink"):
        load_adversarial_corpus(corpus_root / "corpus.json")


def test_adversarial_corpus_rejects_inconsistent_phase_expectations(tmp_path: Path) -> None:
    corpus_root = tmp_path / "adversarial"
    shutil.copytree(_CORPUS_ROOT, corpus_root)
    manifest_path = corpus_root / "corpus.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["cases"][0]["expected"] = {
        "evaluation_status": "timed_out",
        "phase": "scorer",
        "scorer_classification": "candidate_failed",
        "output_truncated": False,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(FixtureConfigurationError, match="scorer-phase"):
        load_adversarial_corpus(manifest_path)
