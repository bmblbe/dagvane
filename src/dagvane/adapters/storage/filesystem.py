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
import re
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import (
    ArtifactRef,
    EventEnvelope,
    ProtocolError,
    RunFinished,
    SpecError,
    StorageError,
)
from dagvane.protocol.frames import (
    canonical_json_bytes,
    envelope_to_frame,
    frame_to_envelope,
    sha256_hex,
)
from dagvane.workspace.paths import ensure_expected_descendant

STATE_DIRNAME = ".dagvane"
RUNS_DIRNAME = "runs"

MANIFEST_FILENAME = "manifest.json"
EVENTS_FILENAME = "events.jsonl"
ARTIFACTS_DIRNAME = "artifacts"
DECISION_FILENAME = "decision.json"
REPORT_FILENAME = "report.json"

ARTIFACT_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_run_id(run_id: object, *, ctx: str) -> str:
    try:
        return validate_filesystem_id(run_id, ctx=ctx)
    except SpecError as exc:
        raise StorageError(str(exc)) from exc


def _require_real_root(root: Path, *, ctx: str) -> Path:
    """Anchor an absolute, real, non-symlink directory: the one trusted base
    every hierarchy check below is validated against.

    ``root`` must already be its own canonical form: a ``..`` component, or
    a symlink anywhere in an ancestor directory, would let ``resolve()``
    silently swap in a different directory than the one the caller named —
    quietly changing which tree owns authority over every path below it —
    so both are rejected before ``root`` is ever adopted."""
    if not root.is_absolute():
        raise StorageError(f"{ctx}: {root} must be an absolute path")
    if ".." in root.parts:
        raise StorageError(f"{ctx}: {root} must not contain '..' components")
    if root.is_symlink() or not root.is_dir():
        raise StorageError(f"{ctx}: {root} must be a real, non-symlink directory")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise StorageError(f"{ctx}: cannot resolve {root}: {exc}") from exc
    if resolved != root:
        raise StorageError(
            f"{ctx}: {root} is not its own canonical path (symlinked ancestor?)"
        )
    return resolved


def _verify_existing_directory(root: Path, path: Path) -> None:
    """A path segment that, if present, must be a real, symlink-free
    directory; a missing segment is left alone (callers decide whether that
    is an error or something to create)."""
    ensure_expected_descendant(root, path)
    if path.exists() and not path.is_dir():
        raise StorageError(f"{path}: expected a directory")


def _verify_existing_file(root: Path, path: Path) -> None:
    """A leaf that, if present, must be a real, symlink-free regular file."""
    ensure_expected_descendant(root, path)
    if path.exists() and not path.is_file():
        raise StorageError(f"{path}: expected a regular file")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, root: Path) -> None:
    """tmp (unpredictable name, in ``path.parent``) → fsync → atomic rename.
    ``root`` anchors a hierarchy check on ``path`` itself before any
    filesystem effect, so a symlinked leaf or an escape out of the run/
    artifact tree is refused rather than silently followed or replaced."""
    _verify_existing_file(root, path)
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise StorageError(f"cannot write {path}: {exc}") from exc


class FilesystemArtifactStore:
    """Content-addressed blobs under a real, symlink-free ``root``.

    ``root`` must already exist as a real directory: this constructor is
    used both directly (callers own the root's lifecycle) and internally by
    ``FilesystemRunStore.artifact_store``, where the run's ``artifacts/``
    directory is always created by ``create_run`` before any store is handed
    out — so requiring an existing root here never adopts or fabricates a
    directory, it only refuses to operate on one that is missing or wrong.
    """

    def __init__(self, root: Path) -> None:
        self._root = _require_real_root(root, ctx="artifact store root")

    def _artifact_path(self, sha256: str) -> Path:
        if not isinstance(sha256, str) or not ARTIFACT_NAME_RE.fullmatch(sha256):
            raise StorageError(f"invalid artifact name {sha256!r}: must be lowercase 64-hex sha256")
        path = self._root / sha256
        ensure_expected_descendant(self._root, path)
        return path

    def put(self, data: bytes, *, media_type: str, role: str) -> ArtifactRef:
        digest = sha256_hex(data)
        path = self._artifact_path(digest)
        if path.exists():
            if not path.is_file():
                raise StorageError(f"artifact {digest} exists but is not a regular file")
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise StorageError(f"cannot read artifact {digest}: {exc}") from exc
            if existing != data:
                raise StorageError(f"artifact {digest} already exists with different content")
        else:
            _atomic_write(path, data, root=self._root)
        return ArtifactRef(sha256=digest, size=len(data), media_type=media_type, role=role)

    def load(self, sha256: str) -> bytes:
        path = self._artifact_path(sha256)
        if path.exists() and not path.is_file():
            raise StorageError(f"cannot read artifact {sha256}: not a regular file")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read artifact {sha256}: {exc}") from exc
        if sha256_hex(data) != sha256:
            raise StorageError(f"artifact {sha256} content does not match its digest")
        return data


class FilesystemEventJournal:
    """Single-writer append-only journal with gapless seq and terminal enforcement."""

    def __init__(self, path: Path, *, run_id: str) -> None:
        validated_run_id = _require_run_id(run_id, ctx="journal: run_id")
        # O_CREAT|O_EXCL is the only race-free way to say "create iff
        # absent": a preceding exists()+open("ab") pair leaves a TOCTOU
        # window where a concurrent creator's journal could be silently
        # appended to (or, via a planted symlink, replaced/followed).
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):  # pragma: no branch — POSIX in CI
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise StorageError(f"journal {path} already exists; runs are write-once in G0") from exc
        except OSError as exc:
            raise StorageError(f"cannot open journal {path}: {exc}") from exc
        try:
            self._handle = os.fdopen(fd, "ab")
        except (OSError, ValueError) as exc:
            # leaf already exists via O_EXCL; must not survive a failed fdopen
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                path.unlink(missing_ok=True)
            except OSError as unlink_exc:
                raise StorageError(
                    f"cannot open journal {path}: {exc}; "
                    f"cleanup of partially created journal also failed: {unlink_exc}"
                ) from exc
            raise StorageError(f"cannot open journal {path}: {exc}") from exc
        self._run_id = validated_run_id
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
        if envelope.run_id != self._run_id:
            raise StorageError(
                f"event run_id {envelope.run_id!r} does not match journal run {self._run_id!r}"
            )
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
    """Run state under ``<root>/.dagvane/runs/<run-id>/``.

    ``root`` is anchored once, at construction, to a real absolute
    workspace directory (see ``_require_real_root``). Every method below
    re-verifies, before touching the filesystem, that every fixed level on
    the way to its target — ``.dagvane``, ``runs``, the run directory, and
    (where relevant) the ``artifacts`` directory or a fixed leaf file — is
    either absent or a real, symlink-free object of the expected type. No
    reader ever adopts or creates missing state; only ``create_run`` and the
    atomic document writers create directories or files.
    """

    def __init__(self, root: Path) -> None:
        self._root = _require_real_root(root, ctx="run store root")
        self._state_dir = self._root / STATE_DIRNAME
        self._runs_root = self._state_dir / RUNS_DIRNAME

    def _run_dir(self, run_id: str) -> Path:
        validated = _require_run_id(run_id, ctx="run store: run_id")
        run_dir = self._runs_root / validated
        for directory in (self._state_dir, self._runs_root, run_dir):
            _verify_existing_directory(self._root, directory)
        return run_dir

    def run_dir(self, run_id: str) -> Path:
        """Absolute run directory path (CLI convenience; never persisted).

        Pure: verifies the existing hierarchy is well-formed but never
        creates missing directories.
        """
        return self._run_dir(run_id)

    def create_run(self, run_id: str) -> None:
        validated = _require_run_id(run_id, ctx="run store: run_id")
        run_dir = self._runs_root / validated
        artifacts_dir = run_dir / ARTIFACTS_DIRNAME
        for directory in (self._state_dir, self._runs_root, run_dir, artifacts_dir):
            _verify_existing_directory(self._root, directory)
        if run_dir.exists():
            raise StorageError(f"run {run_id!r} already exists at {run_dir}")
        # The category parents are shared and may be created or reused by
        # any creator; a missing one is filled in but never torn down here.
        for directory in (self._state_dir, self._runs_root):
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise StorageError(f"cannot create {directory}: {exc}") from exc
        # Ownership of the run directory is acquired by a single atomic
        # mkdir(exist_ok=False): the earlier exists() above is only a fast
        # pre-check, not the authority, so a concurrent creator that wins
        # this mkdir is never adopted — the loser fails and touches nothing
        # it does not itself own.
        try:
            run_dir.mkdir()
        except FileExistsError as exc:
            raise StorageError(f"run {run_id!r} already exists at {run_dir}") from exc
        except OSError as exc:
            raise StorageError(f"cannot create run dir {run_dir}: {exc}") from exc
        # Only the caller that just created run_dir reaches here, so only
        # it may populate artifacts/ or roll back its own still-empty
        # run_dir on failure.
        created: list[Path] = [run_dir]
        try:
            artifacts_dir.mkdir()
            created.append(artifacts_dir)
            _fsync_dir(run_dir)
            _fsync_dir(self._runs_root)
        except OSError as exc:
            for path in reversed(created):
                try:
                    path.rmdir()
                except OSError:
                    pass
            raise StorageError(f"cannot create run dir {run_dir}: {exc}") from exc

    def run_exists(self, run_id: str) -> bool:
        """Pure: never creates missing state."""
        manifest = self._run_dir(run_id) / MANIFEST_FILENAME
        _verify_existing_file(self._root, manifest)
        return manifest.is_file()

    def open_journal(self, run_id: str) -> FilesystemEventJournal:
        run_dir = self._run_dir(run_id)
        journal_path = run_dir / EVENTS_FILENAME
        _verify_existing_file(self._root, journal_path)
        return FilesystemEventJournal(journal_path, run_id=run_id)

    def artifact_store(self, run_id: str) -> FilesystemArtifactStore:
        run_dir = self._run_dir(run_id)
        artifacts_dir = run_dir / ARTIFACTS_DIRNAME
        _verify_existing_directory(self._root, artifacts_dir)
        return FilesystemArtifactStore(artifacts_dir)

    def write_manifest(self, run_id: str, doc: Mapping[str, object]) -> None:
        run_dir = self._run_dir(run_id)
        if doc.get("run_id") != run_id:
            raise StorageError(
                f"manifest run_id {doc.get('run_id')!r} does not match requested run {run_id!r}"
            )
        _atomic_write(
            run_dir / MANIFEST_FILENAME, canonical_json_bytes(dict(doc)), root=self._root
        )

    def write_decision(self, run_id: str, doc: Mapping[str, object]) -> None:
        run_dir = self._run_dir(run_id)
        _atomic_write(
            run_dir / DECISION_FILENAME, canonical_json_bytes(dict(doc)), root=self._root
        )

    def write_report(self, run_id: str, doc: Mapping[str, object]) -> None:
        run_dir = self._run_dir(run_id)
        _atomic_write(
            run_dir / REPORT_FILENAME, canonical_json_bytes(dict(doc)), root=self._root
        )

    def read_manifest(self, run_id: str) -> dict[str, object]:
        run_dir = self._run_dir(run_id)
        path = run_dir / MANIFEST_FILENAME
        _verify_existing_file(self._root, path)
        try:
            obj = json.loads(path.read_bytes())
        except OSError as exc:
            raise StorageError(f"unknown run {run_id!r}: {exc}") from exc
        except ValueError as exc:
            raise StorageError(f"manifest for run {run_id!r} is corrupt: {exc}") from exc
        if not isinstance(obj, dict):
            raise StorageError(f"manifest for run {run_id!r} must be a JSON object")
        if obj.get("run_id") != run_id:
            raise StorageError(
                f"manifest run_id {obj.get('run_id')!r} does not match requested run {run_id!r}"
            )
        return obj

    def iter_frames(self, run_id: str, *, since: int = 0) -> Iterator[bytes]:
        run_dir = self._run_dir(run_id)
        path = run_dir / EVENTS_FILENAME
        _verify_existing_file(self._root, path)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise StorageError(f"cannot read events for run {run_id!r}: {exc}") from exc
        return iter(self._parse_lines(raw, run_id, since))

    @staticmethod
    def _parse_lines(raw: bytes, run_id: str, since: int) -> list[bytes]:
        selected: list[bytes] = []
        for line in raw.splitlines(keepends=True):
            try:
                envelope = frame_to_envelope(line)
            except ProtocolError as exc:
                raise StorageError(f"malformed event frame for run {run_id!r}: {exc}") from exc
            if envelope.run_id != run_id:
                raise StorageError(
                    f"event run_id {envelope.run_id!r} does not match requested run {run_id!r}"
                )
            if envelope.seq > since:
                selected.append(line)
        return selected
