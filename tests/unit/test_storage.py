"""Filesystem storage: CAS artifacts, gapless journal, atomic derived views."""

from __future__ import annotations

from pathlib import Path

import pytest

from dagvane.adapters.storage.filesystem import (
    FilesystemArtifactStore,
    FilesystemRunStore,
)
from dagvane.domain.models import (
    EventEnvelope,
    EventPayload,
    NodeStarted,
    RunCreated,
    RunFinished,
    StorageError,
    payload_to_data,
)
from dagvane.protocol.frames import sha256_hex

RUN_ID = "r-storage-test"


def _payload_run_created() -> RunCreated:
    return RunCreated(
        engine_version="0.0.0",
        task_sha256="t" * 64,
        plan_sha256="p" * 64,
        fixture_sha256="f" * 64,
        node_count=1,
        max_calls=1,
        max_total_tokens=1,
        max_cost_microusd=1,
    )


def _payload_run_finished() -> RunFinished:
    return RunFinished(
        status="completed", reason=None, calls=0, input_tokens=0, output_tokens=0,
        cost_microusd=0,
    )


def _envelope(seq: int, payload: EventPayload, node_id: str | None = None) -> EventEnvelope:
    return EventEnvelope(
        v=1,
        event_id=f"event-{seq}",
        run_id=RUN_ID,
        seq=seq,
        ts="2026-01-01T00:00:00.000Z",
        node_id=node_id,
        attempt=None,
        operation_id=None,
        call_id=None,
        type=payload.TYPE,
        data=payload_to_data(payload),
    )


def test_artifact_store_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref1 = store.put(b"hello", media_type="text/plain", role="proposal")
    ref2 = store.put(b"hello", media_type="text/plain", role="proposal")
    assert ref1.sha256 == ref2.sha256 == sha256_hex(b"hello")
    assert ref1.size == 5
    assert store.load(ref1.sha256) == b"hello"
    assert [p.name for p in tmp_path.iterdir()] == [ref1.sha256]
    other = store.put(b"world", media_type="text/plain", role="proposal")
    assert other.sha256 != ref1.sha256


def test_artifact_load_unknown_sha_raises(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    with pytest.raises(StorageError):
        store.load("0" * 64)


def test_journal_assigns_gapless_seq_and_returns_exact_bytes(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal = run_store.open_journal(RUN_ID)
    lines = []
    assert journal.next_seq == 1
    lines.append(journal.append(_envelope(1, _payload_run_created())))
    assert journal.next_seq == 2
    lines.append(journal.append(_envelope(2, NodeStarted(role="proposer", route_id="r"), "n")))
    lines.append(journal.append(_envelope(3, _payload_run_finished())))
    journal.close()

    journal_path = tmp_path / ".dagvane" / "runs" / RUN_ID / "events.jsonl"
    assert journal_path.read_bytes() == b"".join(lines)
    assert list(run_store.iter_frames(RUN_ID)) == lines
    assert list(run_store.iter_frames(RUN_ID, since=2)) == lines[2:]
    assert list(run_store.iter_frames(RUN_ID, since=3)) == []


def test_journal_rejects_seq_gap(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal = run_store.open_journal(RUN_ID)
    journal.append(_envelope(1, _payload_run_created()))
    with pytest.raises(StorageError, match="gapless"):
        journal.append(_envelope(3, _payload_run_finished()))


def test_journal_refuses_events_after_terminal(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal = run_store.open_journal(RUN_ID)
    journal.append(_envelope(1, _payload_run_created()))
    journal.append(_envelope(2, _payload_run_finished()))
    with pytest.raises(StorageError, match="terminal"):
        journal.append(_envelope(3, NodeStarted(role="proposer", route_id="r"), "n"))


def test_runs_are_write_once(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    with pytest.raises(StorageError, match="already exists"):
        run_store.create_run(RUN_ID)
    run_store.open_journal(RUN_ID)
    with pytest.raises(StorageError, match="already exists"):
        run_store.open_journal(RUN_ID)


def test_document_writes_are_atomic_and_canonical(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    run_store.write_manifest(RUN_ID, {"b": 1, "a": 2})
    run_dir = tmp_path / ".dagvane" / "runs" / RUN_ID
    assert (run_dir / "manifest.json").read_bytes() == b'{"a":2,"b":1}\n'
    assert not list(run_dir.glob("*.tmp"))
    assert run_store.read_manifest(RUN_ID) == {"a": 2, "b": 1}
    assert run_store.run_exists(RUN_ID)
    assert not run_store.run_exists("r-absent")


def test_invalid_run_ids_rejected(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    for bad in ("", ".", "..", "a/b"):
        with pytest.raises(StorageError):
            run_store.create_run(bad)
