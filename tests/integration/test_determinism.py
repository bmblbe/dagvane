"""Acceptance criterion 9: injected clock, IDs, and fake responses make
repeated runs byte-identical — across different working directories — and the
injected sources show up exactly (fixed timestamps, sequential ids).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council
from dagvane.protocol.documents import load_fixture_file, load_task_file
from dagvane.protocol.frames import frame_to_envelope
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


def test_fixed_clock_and_sequential_ids_are_exact(happy_run: CompletedRun) -> None:
    envelopes = [frame_to_envelope(line) for line in happy_run.journal_lines()]
    # The fixture pins start=2026-01-01T00:00:00.000Z with step_ms=10; the run
    # takes one clock reading for created_ts before the first event.
    assert envelopes[0].ts == "2026-01-01T00:00:00.010Z"
    assert envelopes[1].ts == "2026-01-01T00:00:00.020Z"
    assert envelopes[0].event_id == "event-g0-000001"
    assert envelopes[1].event_id == "event-g0-000002"
    assert envelopes[0].run_id == "r-happy-0001"
    operation_ids = [env.operation_id for env in envelopes if env.operation_id is not None]
    assert operation_ids[0] == "op-g0-000001"


def test_changed_seed_and_clock_change_ids_and_timestamps(tmp_path: Path) -> None:
    fixture = dataclasses.replace(
        load_fixture_file(FIXTURE_HAPPY),
        ids_seed="other",
        clock_start="2027-06-15T12:00:00.000Z",
        run_id="r-other-0001",
    )
    store = FilesystemRunStore(tmp_path)
    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=fixture,
        store=store,
        backend=FakeBackend(fixture.responses),
        sink=None,
    )
    envelopes = [frame_to_envelope(line) for line in store.iter_frames(result.run_id)]
    assert envelopes[0].event_id == "event-other-000001"
    assert envelopes[0].ts.startswith("2027-06-15T12:00:00")
