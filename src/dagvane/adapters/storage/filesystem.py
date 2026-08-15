"""Filesystem run store and content-addressed artifact store.

Durability discipline (Round 4 §10): documents and artifacts are written as
tmp file → fsync → atomic rename → fsync of the containing directory; the
event journal is append + flush + fsync per event, with gapless ``seq``
assigned by the single writer. ``events.jsonl`` is authoritative for run
state; decision and report are views derived from it, while ``manifest.json``
is the sealed pre-run configuration record referenced by hash from
``run.created``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

from dagvane.domain.models import (
    ArtifactRef,
    EventEnvelope,
    RunFinished,
    StorageError,
)
from dagvane.protocol.frames import (
    canonical_json_bytes,
    envelope_to_frame,
    frame_to_envelope,
    sha256_hex,
)

RUNS_SUBDIR = Path(".dagvane") / "runs"

MANIFEST_FILENAME = "manifest.json"
EVENTS_FILENAME = "events.jsonl"
ARTIFACTS_DIRNAME = "artifacts"
DECISION_FILENAME = "decision.json"
REPORT_FILENAME = "report.json"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc


class FilesystemArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, data: bytes, *, media_type: str, role: str) -> ArtifactRef:
        digest = sha256_hex(data)
        path = self._root / digest
        if not path.exists():
            _atomic_write(path, data)
        return ArtifactRef(sha256=digest, size=len(data), media_type=media_type, role=role)

    def load(self, sha256: str) -> bytes:
        path = self._root / sha256
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read artifact {sha256}: {exc}") from exc


class FilesystemEventJournal:
    """Single-writer append-only journal with gapless seq and terminal enforcement."""

    def __init__(self, path: Path) -> None:
        if path.exists():
            raise StorageError(f"journal {path} already exists; runs are write-once in G0")
        try:
            self._handle = open(path, "ab")
        except OSError as exc:
            raise StorageError(f"cannot open journal {path}: {exc}") from exc
        self._next_seq = 1
        self._terminal = False
        self._closed = False

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append(self, envelope: EventEnvelope) -> bytes:
        if self._closed:
            raise StorageError("journal is closed")
        if self._terminal:
            raise StorageError("journal already holds a terminal event")
        if envelope.seq != self._next_seq:
            raise StorageError(
                f"gapless seq violation: expected {self._next_seq}, got {envelope.seq}"
            )
        frame = envelope_to_frame(envelope)
        try:
            self._handle.write(frame)
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except OSError as exc:
            raise StorageError(f"cannot append event seq {envelope.seq}: {exc}") from exc
        self._next_seq += 1
        if envelope.type == RunFinished.TYPE:
            self._terminal = True
        return frame

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._handle.close()


class FilesystemRunStore:
    """Run state under ``<root>/.dagvane/runs/<run-id>/``."""

    def __init__(self, root: Path) -> None:
        self._runs_root = root / RUNS_SUBDIR

    def _run_dir(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or run_id in {".", ".."}:
            raise StorageError(f"invalid run id {run_id!r}")
        return self._runs_root / run_id

    def run_dir(self, run_id: str) -> Path:
        """Absolute run directory path (CLI convenience; never persisted)."""
        return self._run_dir(run_id)

    def create_run(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        try:
            self._runs_root.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(exist_ok=False)
            (run_dir / ARTIFACTS_DIRNAME).mkdir()
        except FileExistsError as exc:
            raise StorageError(f"run {run_id!r} already exists at {run_dir}") from exc
        except OSError as exc:
            raise StorageError(f"cannot create run dir {run_dir}: {exc}") from exc
        _fsync_dir(run_dir)
        _fsync_dir(self._runs_root)

    def run_exists(self, run_id: str) -> bool:
        return (self._run_dir(run_id) / MANIFEST_FILENAME).is_file()

    def open_journal(self, run_id: str) -> FilesystemEventJournal:
        return FilesystemEventJournal(self._run_dir(run_id) / EVENTS_FILENAME)

    def artifact_store(self, run_id: str) -> FilesystemArtifactStore:
        return FilesystemArtifactStore(self._run_dir(run_id) / ARTIFACTS_DIRNAME)

    def write_manifest(self, run_id: str, doc: Mapping[str, object]) -> None:
        _atomic_write(self._run_dir(run_id) / MANIFEST_FILENAME, canonical_json_bytes(dict(doc)))

    def write_decision(self, run_id: str, doc: Mapping[str, object]) -> None:
        _atomic_write(self._run_dir(run_id) / DECISION_FILENAME, canonical_json_bytes(dict(doc)))

    def write_report(self, run_id: str, doc: Mapping[str, object]) -> None:
        _atomic_write(self._run_dir(run_id) / REPORT_FILENAME, canonical_json_bytes(dict(doc)))

    def read_manifest(self, run_id: str) -> dict[str, object]:
        path = self._run_dir(run_id) / MANIFEST_FILENAME
        try:
            obj = json.loads(path.read_bytes())
        except OSError as exc:
            raise StorageError(f"unknown run {run_id!r}: {exc}") from exc
        except ValueError as exc:
            raise StorageError(f"manifest for run {run_id!r} is corrupt: {exc}") from exc
        if not isinstance(obj, dict):
            raise StorageError(f"manifest for run {run_id!r} must be a JSON object")
        return obj

    def iter_frames(self, run_id: str, *, since: int = 0) -> Iterator[bytes]:
        path = self._run_dir(run_id) / EVENTS_FILENAME
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read events for run {run_id!r}: {exc}") from exc
        for line in raw.splitlines(keepends=True):
            envelope = frame_to_envelope(line)
            if envelope.seq > since:
                yield line
