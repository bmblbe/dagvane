"""Acceptance criterion 9: injected clock, IDs, and fake responses make
repeated runs byte-identical — across different working directories.
"""

from __future__ import annotations

import pytest

from helpers import FIXTURE_HAPPY, HAPPY_RUN_ID, TASK_BASIC, CompletedRun, run_council_cli


@pytest.fixture(scope="module")
def twin_runs(tmp_path_factory: pytest.TempPathFactory) -> tuple[CompletedRun, CompletedRun]:
    first = run_council_cli(
        TASK_BASIC, FIXTURE_HAPPY, tmp_path_factory.mktemp("det-one"), HAPPY_RUN_ID
    )
    second = run_council_cli(
        TASK_BASIC, FIXTURE_HAPPY, tmp_path_factory.mktemp("det-two"), HAPPY_RUN_ID
    )
    assert first.returncode == 0 and second.returncode == 0
    return first, second


def test_stdout_streams_are_byte_identical(
    twin_runs: tuple[CompletedRun, CompletedRun],
) -> None:
    first, second = twin_runs
    assert first.stdout == second.stdout


def test_persisted_documents_are_byte_identical(
    twin_runs: tuple[CompletedRun, CompletedRun],
) -> None:
    first, second = twin_runs
    for name in ("events.jsonl", "manifest.json", "decision.json", "report.json"):
        assert (first.run_dir / name).read_bytes() == (second.run_dir / name).read_bytes(), name


def test_artifact_stores_are_identical(
    twin_runs: tuple[CompletedRun, CompletedRun],
) -> None:
    first, second = twin_runs
    first_names = sorted(p.name for p in (first.run_dir / "artifacts").iterdir())
    second_names = sorted(p.name for p in (second.run_dir / "artifacts").iterdir())
    assert first_names == second_names
    for name in first_names:
        assert (first.run_dir / "artifacts" / name).read_bytes() == (
            second.run_dir / "artifacts" / name
        ).read_bytes()
