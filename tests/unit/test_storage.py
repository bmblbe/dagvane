"""Filesystem storage: CAS artifacts, gapless journal, atomic derived views."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import IO
from unittest import mock

import pytest

from dagvane.adapters.storage.filesystem import (
    STATE_DIRNAME,
    FilesystemArtifactStore,
    FilesystemEventJournal,
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
from dagvane.protocol.frames import envelope_to_frame, sha256_hex

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
    run_store.write_manifest(RUN_ID, {"b": 1, "a": 2, "run_id": RUN_ID})
    run_dir = tmp_path / ".dagvane" / "runs" / RUN_ID
    assert (run_dir / "manifest.json").read_bytes() == (
        b'{"a":2,"b":1,"run_id":"r-storage-test"}\n'
    )
    assert not list(run_dir.glob("*.tmp"))
    assert run_store.read_manifest(RUN_ID) == {"a": 2, "b": 1, "run_id": RUN_ID}
    assert run_store.run_exists(RUN_ID)
    assert not run_store.run_exists("r-absent")


def test_invalid_run_ids_rejected(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    for bad in ("", ".", "..", "a/b"):
        with pytest.raises(StorageError):
            run_store.create_run(bad)


@pytest.mark.parametrize(
    "method",
    ["run_dir", "run_exists", "open_journal", "artifact_store", "read_manifest", "iter_frames"],
)
def test_every_run_store_reader_rejects_invalid_run_id(tmp_path: Path, method: str) -> None:
    run_store = FilesystemRunStore(tmp_path)
    with pytest.raises(StorageError):
        getattr(run_store, method)("../escape")


@pytest.mark.parametrize("method", ["write_manifest", "write_decision", "write_report"])
def test_every_run_store_writer_rejects_invalid_run_id(tmp_path: Path, method: str) -> None:
    run_store = FilesystemRunStore(tmp_path)
    with pytest.raises(StorageError):
        getattr(run_store, method)("../escape", {})


def test_write_manifest_rejects_mismatched_run_id(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    with pytest.raises(StorageError, match="run_id"):
        run_store.write_manifest(RUN_ID, {"run_id": "someone-else"})


def test_read_manifest_rejects_mismatched_or_missing_run_id(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    manifest_path = tmp_path / ".dagvane" / "runs" / RUN_ID / "manifest.json"
    manifest_path.write_bytes(b'{"a":1}\n')
    with pytest.raises(StorageError, match="run_id"):
        run_store.read_manifest(RUN_ID)

    manifest_path.write_bytes(b'{"run_id":"someone-else"}\n')
    with pytest.raises(StorageError, match="run_id"):
        run_store.read_manifest(RUN_ID)


def test_journal_rejects_event_run_id_mismatch(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal = run_store.open_journal(RUN_ID)
    mismatched = EventEnvelope(
        v=1,
        event_id="event-1",
        run_id="someone-else",
        seq=1,
        ts="2026-01-01T00:00:00.000Z",
        node_id=None,
        attempt=None,
        operation_id=None,
        call_id=None,
        type=_payload_run_created().TYPE,
        data=payload_to_data(_payload_run_created()),
    )
    with pytest.raises(StorageError, match="run_id"):
        journal.append(mismatched)


def test_iter_frames_rejects_event_run_id_mismatch(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal_path = tmp_path / ".dagvane" / "runs" / RUN_ID / "events.jsonl"
    forged = EventEnvelope(
        v=1,
        event_id="event-1",
        run_id="someone-else",
        seq=1,
        ts="2026-01-01T00:00:00.000Z",
        node_id=None,
        attempt=None,
        operation_id=None,
        call_id=None,
        type=_payload_run_created().TYPE,
        data=payload_to_data(_payload_run_created()),
    )
    journal_path.write_bytes(envelope_to_frame(forged))
    with pytest.raises(StorageError, match="run_id"):
        list(run_store.iter_frames(RUN_ID))


def test_artifact_names_must_be_lowercase_64_hex_sha256(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    for bad in ("../escape", "A" * 64, "z" * 64, "f" * 63, "f" * 65, ""):
        with pytest.raises(StorageError):
            store.load(bad)


def test_artifact_put_rejects_content_mismatch_on_hash_collision(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.put(b"hello", media_type="text/plain", role="proposal")
    tampered_path = tmp_path / ref.sha256
    tampered_path.write_bytes(b"tampered")
    with pytest.raises(StorageError, match="different content"):
        store.put(b"hello", media_type="text/plain", role="proposal")


def test_journal_rejects_invalid_run_id_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    with pytest.raises(StorageError):
        FilesystemEventJournal(path, run_id="../escape")
    assert not path.exists()


def test_iter_frames_rejects_malformed_frame_and_yields_no_prefix(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    journal = run_store.open_journal(RUN_ID)
    good_line = journal.append(_envelope(1, _payload_run_created()))
    journal.close()
    journal_path = tmp_path / ".dagvane" / "runs" / RUN_ID / "events.jsonl"
    journal_path.write_bytes(good_line + b"not json at all\n")
    with pytest.raises(StorageError, match="malformed"):
        run_store.iter_frames(RUN_ID)


def test_atomic_write_leaves_preexisting_predictable_tmp_file_unchanged(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    run_dir = tmp_path / ".dagvane" / "runs" / RUN_ID
    stale_tmp = run_dir / "manifest.json.tmp"
    stale_tmp.write_bytes(b"stale-leftover")
    run_store.write_manifest(RUN_ID, {"run_id": RUN_ID})
    assert stale_tmp.read_bytes() == b"stale-leftover"
    assert run_store.read_manifest(RUN_ID) == {"run_id": RUN_ID}


def test_artifact_load_rejects_tampered_content(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    ref = store.put(b"hello", media_type="text/plain", role="proposal")
    (tmp_path / ref.sha256).write_bytes(b"tampered")
    with pytest.raises(StorageError, match="does not match its digest"):
        store.load(ref.sha256)


# --- Strict filesystem hierarchy validation ---------------------------------


def test_run_store_rejects_relative_root(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        FilesystemRunStore(Path("relative/root"))


def test_run_store_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        FilesystemRunStore(tmp_path / "does-not-exist")


def test_run_store_rejects_root_that_is_a_file(tmp_path: Path) -> None:
    root = tmp_path / "root-is-a-file"
    root.write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        FilesystemRunStore(root)


def test_run_store_rejects_symlink_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(StorageError):
        FilesystemRunStore(link)


def test_run_store_rejects_dotdot_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    outside = tmp_path / "other" / ".." / "real"
    (tmp_path / "other").mkdir()
    with pytest.raises(StorageError, match=r"\.\."):
        FilesystemRunStore(outside)
    # No .dagvane hierarchy was ever created under the intended root.
    assert not (real / STATE_DIRNAME).exists()


def test_run_store_rejects_symlinked_ancestor_root(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_sub = real_parent / "sub"
    real_sub.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent)
    root_via_link = link_parent / "sub"
    with pytest.raises(StorageError, match="canonical"):
        FilesystemRunStore(root_via_link)
    # No state was created under the real, resolved directory either.
    assert not (real_sub / STATE_DIRNAME).exists()


def test_artifact_store_direct_construction_requires_real_existing_root(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        FilesystemArtifactStore(tmp_path / "missing")

    a_file = tmp_path / "a-file"
    a_file.write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        FilesystemArtifactStore(a_file)

    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real)
    with pytest.raises(StorageError):
        FilesystemArtifactStore(link)

    dotdot = tmp_path / "other" / ".." / "real-root"
    with pytest.raises(StorageError, match=r"\.\."):
        FilesystemArtifactStore(dotdot)

    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(tmp_path)
    with pytest.raises(StorageError, match="canonical"):
        FilesystemArtifactStore(link_parent / "real-root")


@pytest.mark.parametrize("segment", [".dagvane", ".dagvane/runs"])
def test_run_store_rejects_directory_segment_that_is_a_file(
    tmp_path: Path, segment: str
) -> None:
    path = tmp_path / segment
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a directory")
    run_store = FilesystemRunStore(tmp_path)
    with pytest.raises(StorageError):
        run_store.run_dir(RUN_ID)
    with pytest.raises(StorageError):
        run_store.create_run(RUN_ID)


def test_run_store_rejects_run_directory_that_is_a_file(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    runs_root = tmp_path / ".dagvane" / "runs"
    runs_root.mkdir(parents=True)
    (runs_root / RUN_ID).write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        run_store.run_dir(RUN_ID)
    with pytest.raises(StorageError):
        run_store.run_exists(RUN_ID)


def test_run_store_rejects_artifacts_directory_that_is_a_file(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    run_dir = run_store.run_dir(RUN_ID)
    artifacts_dir = run_dir / "artifacts"
    shutil.rmtree(artifacts_dir)
    artifacts_dir.write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        run_store.artifact_store(RUN_ID)


def test_run_store_rejects_manifest_leaf_that_is_a_directory(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    run_dir = run_store.run_dir(RUN_ID)
    (run_dir / "manifest.json").mkdir()
    with pytest.raises(StorageError):
        run_store.run_exists(RUN_ID)
    with pytest.raises(StorageError):
        run_store.read_manifest(RUN_ID)
    with pytest.raises(StorageError):
        run_store.write_manifest(RUN_ID, {"run_id": RUN_ID})


@pytest.mark.parametrize(
    "link_segment", [".dagvane", ".dagvane/runs", f".dagvane/runs/{RUN_ID}"]
)
def test_run_store_rejects_symlinked_hierarchy_segment(
    tmp_path: Path, link_segment: str
) -> None:
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    link_path = tmp_path / link_segment
    link_path.parent.mkdir(parents=True, exist_ok=True)
    link_path.symlink_to(real_target)
    run_store = FilesystemRunStore(tmp_path)
    with pytest.raises(StorageError):
        run_store.run_dir(RUN_ID)
    with pytest.raises(StorageError):
        run_store.create_run(RUN_ID)


def test_run_store_rejects_symlinked_manifest_leaf(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    run_store.create_run(RUN_ID)
    run_dir = run_store.run_dir(RUN_ID)
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b'{"secret":"unchanged"}')
    (run_dir / "manifest.json").symlink_to(sentinel)
    with pytest.raises(StorageError):
        run_store.write_manifest(RUN_ID, {"run_id": RUN_ID})
    assert sentinel.read_bytes() == b'{"secret":"unchanged"}'
    assert not list(run_dir.glob("*.tmp"))


def test_create_run_never_adopts_an_existing_run_directory(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    runs_root = tmp_path / ".dagvane" / "runs"
    runs_root.mkdir(parents=True)
    preexisting = runs_root / RUN_ID
    preexisting.mkdir()
    (preexisting / "planted.txt").write_bytes(b"attacker-controlled")
    with pytest.raises(StorageError, match="already exists"):
        run_store.create_run(RUN_ID)
    # Adopted-directory contents are untouched: create_run refused rather
    # than reusing (or cleaning up) a directory it did not create.
    assert (preexisting / "planted.txt").read_bytes() == b"attacker-controlled"


def test_create_run_cleans_up_only_empty_directories_it_created(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    other_run = "r-sibling-untouched"
    run_store.create_run(other_run)  # pre-existing sibling: must survive

    real_mkdir = Path.mkdir
    run_dir = tmp_path / ".dagvane" / "runs" / RUN_ID
    artifacts_dir = run_dir / "artifacts"

    def flaky_mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if self == artifacts_dir:
            raise OSError("simulated failure creating artifacts dir")
        real_mkdir(self, mode, parents=parents, exist_ok=exist_ok)

    with mock.patch.object(Path, "mkdir", flaky_mkdir):
        with pytest.raises(StorageError, match="cannot create run dir"):
            run_store.create_run(RUN_ID)

    # The failed call's own new directory is gone...
    assert not run_dir.exists()
    # ...but the pre-existing runs/ root and sibling run survive untouched.
    assert (tmp_path / ".dagvane" / "runs").is_dir()
    assert (tmp_path / ".dagvane" / "runs" / other_run).is_dir()


def test_create_run_loses_race_to_a_concurrently_created_run_directory(
    tmp_path: Path,
) -> None:
    """A competing creator inserts run_dir *between* the pre-check and the
    ownership mkdir. The loser must fail, must not touch the winner's
    directory, and must leave no manifest/artifacts/success state behind."""
    run_store = FilesystemRunStore(tmp_path)
    run_dir = tmp_path / ".dagvane" / "runs" / RUN_ID
    real_mkdir = Path.mkdir
    injected = False

    def racing_mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        nonlocal injected
        if self == run_dir and not injected:
            injected = True
            # A concurrent creator wins ownership first and starts
            # populating the directory with its own state.
            real_mkdir(run_dir, mode)
            (run_dir / "events.jsonl").write_bytes(b"winner-owned-bytes")
        real_mkdir(self, mode, parents=parents, exist_ok=exist_ok)

    with mock.patch.object(Path, "mkdir", racing_mkdir):
        with pytest.raises(StorageError, match="already exists"):
            run_store.create_run(RUN_ID)

    assert injected
    # The winner's directory and its bytes are untouched by the loser.
    assert (run_dir / "events.jsonl").read_bytes() == b"winner-owned-bytes"
    # The loser created no artifacts/ (only the winner may populate run_dir).
    assert not (run_dir / "artifacts").exists()
    # No success state: create_run did not complete for the loser.
    assert not (run_dir / "manifest.json").exists()
    assert not run_store.run_exists(RUN_ID)


def test_journal_open_uses_exclusive_creation_and_never_replaces_existing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    path.write_bytes(b"pre-existing-untouched")
    with pytest.raises(StorageError, match="already exists"):
        FilesystemEventJournal(path, run_id=RUN_ID)
    assert path.read_bytes() == b"pre-existing-untouched"


def test_journal_open_refuses_to_follow_a_symlinked_events_path(tmp_path: Path) -> None:
    sentinel = tmp_path / "sentinel.jsonl"
    sentinel.write_bytes(b"do-not-touch")
    link = tmp_path / "events.jsonl"
    link.symlink_to(sentinel)
    with pytest.raises(StorageError):
        FilesystemEventJournal(link, run_id=RUN_ID)
    assert sentinel.read_bytes() == b"do-not-touch"


@pytest.mark.parametrize("fdopen_exc", [OSError("boom"), ValueError("boom")])
def test_journal_open_cleans_up_after_fdopen_failure_and_allows_retry(
    tmp_path: Path, fdopen_exc: Exception
) -> None:
    path = tmp_path / "events.jsonl"
    real_fdopen = os.fdopen
    calls = 0
    captured_fd: list[int] = []

    def failing_fdopen(fd: int, mode: str = "r") -> IO[bytes]:
        nonlocal calls
        calls += 1
        if calls == 1:
            captured_fd.append(fd)
            raise fdopen_exc
        return real_fdopen(fd, mode)

    with mock.patch("os.fdopen", failing_fdopen):
        with pytest.raises(StorageError) as exc_info:
            FilesystemEventJournal(path, run_id=RUN_ID)

    assert not isinstance(exc_info.value, (OSError, ValueError))
    assert not path.exists()
    with pytest.raises(OSError):
        os.fstat(captured_fd[0])

    journal = FilesystemEventJournal(path, run_id=RUN_ID)
    try:
        assert journal.next_seq == 1
        assert path.exists()
    finally:
        journal.close()


def test_artifact_put_rejects_a_non_regular_existing_digest_path(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    digest = sha256_hex(b"x")
    (tmp_path / digest).mkdir()
    with pytest.raises(StorageError):
        store.put(b"x", media_type="text/plain", role="proposal")


def test_artifact_load_rejects_a_non_regular_existing_digest_path(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    digest = "b" * 64
    (tmp_path / digest).mkdir()
    with pytest.raises(StorageError):
        store.load(digest)


def test_artifact_put_and_load_refuse_a_symlinked_digest_path(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path)
    sentinel = tmp_path / "sentinel-blob"
    sentinel.write_bytes(b"do-not-touch")
    digest = sha256_hex(b"x")
    (tmp_path / digest).symlink_to(sentinel)
    with pytest.raises(StorageError):
        store.put(b"x", media_type="text/plain", role="proposal")
    with pytest.raises(StorageError):
        store.load(digest)
    assert sentinel.read_bytes() == b"do-not-touch"


def test_run_store_readers_do_not_create_missing_hierarchy(tmp_path: Path) -> None:
    run_store = FilesystemRunStore(tmp_path)
    assert run_store.run_dir(RUN_ID) == tmp_path / ".dagvane" / "runs" / RUN_ID
    assert not (tmp_path / ".dagvane").exists()
    assert run_store.run_exists(RUN_ID) is False
    assert not (tmp_path / ".dagvane").exists()
