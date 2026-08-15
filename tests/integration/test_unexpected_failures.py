"""Durable unexpected-failure semantics (the explicit G0 failure taxonomy).

- An unexpected backend/runtime error fails the node and yields a terminal,
  reported, replayable failed run.
- A durable-journal write failure aborts with NO fabricated terminal state:
  non-terminal journal, no report, the storage error propagates.
- A broken output sink degrades streaming only; the journal stays authoritative
  and the run still reaches its natural terminal state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import (
    EVENTS_FILENAME,
    FilesystemEventJournal,
    FilesystemRunStore,
)
from dagvane.application.council import run_council
from dagvane.application.replay import fold_frames
from dagvane.domain.models import (
    EventEnvelope,
    RunStatus,
    StorageError,
    Usage,
    estimate_tokens,
)
from dagvane.ports.backend import ChatResult, PreparedRequest
from dagvane.protocol.documents import (
    FixtureResponse,
    load_fixture_file,
    load_task_file,
)
from dagvane.protocol.frames import frame_to_envelope
from helpers import FIXTURE_HAPPY, TASK_BASIC


class ExplodingBackend:
    """Raises a non-normalized RuntimeError for selected models."""

    def __init__(
        self, responses: Mapping[str, FixtureResponse], explode_models: frozenset[str]
    ) -> None:
        self._responses = dict(responses)
        self._explode = explode_models

    async def complete(self, request: PreparedRequest) -> ChatResult:
        if request.model in self._explode:
            raise RuntimeError(f"unexpected implementation bug for {request.model}")
        response = self._responses[request.model]
        usage = response.usage
        if usage is None:
            usage = Usage(
                input_tokens=estimate_tokens(request.system + request.user_text),
                output_tokens=estimate_tokens(response.text),
            )
        return ChatResult(model=request.model, text=response.text, usage=usage)


class BrokenJournal(FilesystemEventJournal):
    """Fails every append at or beyond a chosen seq — a dying durable store."""

    def __init__(self, path: Path, fail_at_seq: int) -> None:
        super().__init__(path)
        self._fail_at_seq = fail_at_seq

    def append(self, envelope: EventEnvelope) -> bytes:
        if envelope.seq >= self._fail_at_seq:
            raise StorageError("injected journal failure")
        return super().append(envelope)


class BrokenJournalStore(FilesystemRunStore):
    def __init__(self, root: Path, fail_at_seq: int) -> None:
        super().__init__(root)
        self._fail_at_seq = fail_at_seq

    def open_journal(self, run_id: str) -> FilesystemEventJournal:
        return BrokenJournal(self.run_dir(run_id) / EVENTS_FILENAME, self._fail_at_seq)


def test_backend_runtime_error_yields_a_durable_failed_run(tmp_path: Path) -> None:
    task = load_task_file(TASK_BASIC)
    fixture = load_fixture_file(FIXTURE_HAPPY)
    store = FilesystemRunStore(tmp_path)
    backend = ExplodingBackend(fixture.responses, frozenset({"fake-reviewer-a"}))

    result = run_council(task=task, fixture=fixture, store=store, backend=backend, sink=None)

    assert result.status is RunStatus.FAILED
    run_dir = store.run_dir(result.run_id)
    report = json.loads((run_dir / "report.json").read_bytes())
    assert report["status"] == "failed"
    assert report["nodes"]["review-by-a"]["reason"] == "unexpected_error"
    assert report["nodes"]["judge"]["reason"] == "dependency_failed"
    assert report["decision"] is None
    # The journal is terminal and replays cleanly through the strict validator.
    lines = (run_dir / EVENTS_FILENAME).read_bytes().splitlines(keepends=True)
    assert frame_to_envelope(lines[-1]).type == "run.finished"
    failed_events = [
        frame_to_envelope(line)
        for line in lines
        if frame_to_envelope(line).type == "node.failed"
    ]
    reasons = {env.node_id: env.data["reason"] for env in failed_events}
    assert reasons["review-by-a"] == "unexpected_error"
    assert "RuntimeError" in str(
        next(env.data["message"] for env in failed_events if env.node_id == "review-by-a")
    )
    view = fold_frames(lines)
    assert view.status is RunStatus.FAILED


def test_journal_failure_aborts_without_fabricated_terminal_state(tmp_path: Path) -> None:
    task = load_task_file(TASK_BASIC)
    fixture = load_fixture_file(FIXTURE_HAPPY)
    store = BrokenJournalStore(tmp_path, fail_at_seq=10)

    with pytest.raises(StorageError, match="injected journal failure"):
        run_council(
            task=task,
            fixture=fixture,
            store=store,
            backend=FakeBackend(fixture.responses),
            sink=None,
        )

    run_dir = store.run_dir("r-happy-0001")
    assert not (run_dir / "report.json").exists()  # no terminal durability claim
    assert not (run_dir / "decision.json").exists()
    lines = (run_dir / EVENTS_FILENAME).read_bytes().splitlines(keepends=True)
    assert lines, "the events written before the failure remain durable"
    assert all(frame_to_envelope(line).type != "run.finished" for line in lines)
    view = fold_frames(lines, require_terminal=False)
    assert view.status is RunStatus.RUNNING  # honestly non-terminal


def test_sink_failure_degrades_streaming_but_run_stays_durable(tmp_path: Path) -> None:
    task = load_task_file(TASK_BASIC)
    fixture = load_fixture_file(FIXTURE_HAPPY)
    store = FilesystemRunStore(tmp_path)
    delivered: list[bytes] = []

    def flaky_sink(line: bytes, envelope: EventEnvelope) -> None:
        if len(delivered) >= 3:
            raise BrokenPipeError("consumer went away")
        delivered.append(line)

    result = run_council(
        task=task,
        fixture=fixture,
        store=store,
        backend=FakeBackend(fixture.responses),
        sink=flaky_sink,
    )

    assert result.status is RunStatus.COMPLETED
    assert result.sink_error is not None
    assert "BrokenPipeError" in result.sink_error
    run_dir = store.run_dir(result.run_id)
    lines = (run_dir / EVENTS_FILENAME).read_bytes().splitlines(keepends=True)
    assert frame_to_envelope(lines[-1]).type == "run.finished"  # natural terminal
    assert (run_dir / "report.json").is_file()
    assert (run_dir / "decision.json").is_file()
    assert len(delivered) == 3  # streaming stopped exactly at the failure
    assert delivered == lines[:3]  # what was streamed matches the journal bytes
