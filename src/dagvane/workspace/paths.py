"""Workspace state layout: every durable Dagvane artifact of a target project
lives under ``<workspace>/.dagvane/`` (Git-ignored via a self-written
``.gitignore`` so target repositories need no preparation)."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

from dagvane.domain.models import StorageError


def ensure_expected_descendant(root: Path, target: Path) -> None:
    """Reject any ``target`` that is not a strict descendant path of ``root``.

    Pure path-component comparison: neither ``root`` nor ``target`` is
    resolved, so a caller cannot be fooled into validating one logical path
    and later touching another. Both must be absolute with no ``..``
    component. Missing path segments are fine; an *existing* symlink at or
    below ``root`` on the way to ``target`` (including the leaf) is not.
    """
    if not root.is_absolute() or not target.is_absolute():
        raise StorageError(f"{target}: root and target must be absolute paths")
    if ".." in root.parts or ".." in target.parts:
        raise StorageError(f"{target}: path traversal component is not allowed")
    if root.is_symlink() or not root.is_dir():
        raise StorageError(f"{target}: allowed root {root} must be a real directory")
    root_parts = root.parts
    target_parts = target.parts
    if len(target_parts) <= len(root_parts) or target_parts[: len(root_parts)] != root_parts:
        raise StorageError(f"{target}: must be a strict descendant of {root}")
    probe = root
    for part in target_parts[len(root_parts) :]:
        probe = probe / part
        if probe.is_symlink():
            raise StorageError(f"{target}: {probe} is a symlink")


def require_canonical_root(root: Path, *, ctx: str) -> Path:
    """Anchor an absolute, real, non-symlink, already-canonical directory: the
    one trusted base every hierarchy check below it is validated against.

    ``root`` must already be its own canonical form: a ``..`` component, or a
    symlink anywhere in an ancestor directory, would let ``resolve()``
    silently swap in a different directory than the one the caller named —
    quietly changing which tree owns authority over every path below it — so
    both are rejected before ``root`` is ever adopted.
    """
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


def _validate_against_root(path: Path, allowed_root: Path | None) -> None:
    if allowed_root is not None:
        ensure_expected_descendant(allowed_root, path)


_Identity = tuple[int, int, int]
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_TEMP_NAME_ATTEMPTS = 16


def _identity(st: os.stat_result) -> _Identity:
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))


def _stat_leaf(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _prove_leaf(dir_fd: int, name: str, expected: _Identity | None, *, ctx: str) -> None:
    actual = _stat_leaf(dir_fd, name)
    if (None if actual is None else _identity(actual)) != expected:
        raise StorageError(f"{ctx}: {name!r} identity changed")


def _renameat2(
    old_dir_fd: int, old_name: str, new_dir_fd: int, new_name: str, flags: int
) -> None:
    """Linux/POSIX atomic rename with collision/exchange semantics.

    Python does not expose ``renameat2``.  Dagvane's current local-release
    platform is Linux, so use libc directly and fail closed if the running
    libc/kernel cannot provide the operation: falling back to ``replace``
    would reopen the destination-identity race this helper exists to close.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    result = rename(
        old_dir_fd,
        os.fsencode(old_name),
        new_dir_fd,
        os.fsencode(new_name),
        flags,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), new_name)


def rename_noreplace(
    old_name: str,
    new_name: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    """Atomically rename one leaf without replacing an existing destination."""
    try:
        _renameat2(src_dir_fd, old_name, dst_dir_fd, new_name, _RENAME_NOREPLACE)
    except OSError as exc:
        if exc.errno in (errno.ENOSYS, errno.EINVAL):
            raise StorageError(
                "atomic no-replace publication requires renameat2"
            ) from exc
        raise


def _new_temp_leaf(dir_fd: int, name: str) -> tuple[int, str, _Identity]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    for _ in range(_TEMP_NAME_ATTEMPTS):
        tmp_name = f".{name}.{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            continue
        try:
            identity = _identity(os.fstat(fd))
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        return fd, tmp_name, identity
    raise StorageError(
        f"cannot write {name!r}: could not allocate an exclusive temporary leaf"
    )


def _unlink_if_identity(dir_fd: int, name: str, expected: _Identity) -> None:
    """Remove only the helper-owned entry, never a colliding replacement."""
    try:
        current = _stat_leaf(dir_fd, name)
        if current is not None and _identity(current) == expected:
            os.unlink(name, dir_fd=dir_fd)
    except OSError:
        pass


def _atomic_write_bytes_pinned(
    dir_fd: int,
    name: str,
    data: bytes,
    *,
    prove_parent: Callable[[], None] | None = None,
    reject_destination_symlink: bool = False,
) -> None:
    """Write one fixed leaf using only an already-pinned directory fd."""
    name = _require_simple_leaf_name(name)
    parent_identity = _identity(os.fstat(dir_fd))
    if parent_identity[2] != stat.S_IFDIR:
        raise StorageError(f"cannot write {name!r}: parent fd is not a directory")

    def prove() -> None:
        if _identity(os.fstat(dir_fd)) != parent_identity:
            raise StorageError(f"cannot write {name!r}: pinned parent identity changed")
        if prove_parent is not None:
            prove_parent()

    prove()
    initial_st = _stat_leaf(dir_fd, name)
    if initial_st is not None and not (
        stat.S_ISREG(initial_st.st_mode) or stat.S_ISLNK(initial_st.st_mode)
    ):
        raise StorageError(f"cannot write {name!r}: destination is not a file leaf")
    if reject_destination_symlink and initial_st is not None and stat.S_ISLNK(
        initial_st.st_mode
    ):
        raise StorageError(f"cannot write {name!r}: refusing a destination symlink")
    initial_identity = None if initial_st is None else _identity(initial_st)

    fd: int | None = None
    tmp_name: str | None = None
    written_identity: _Identity | None = None
    tmp_entry_identity: _Identity | None = None
    published = False
    try:
        fd, tmp_name, written_identity = _new_temp_leaf(dir_fd, name)
        tmp_entry_identity = written_identity
        prove()
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            fd = None
            raise
        fd = None
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

        prove()
        _prove_leaf(dir_fd, tmp_name, written_identity, ctx="temporary leaf")
        _prove_leaf(dir_fd, name, initial_identity, ctx="destination leaf")

        if initial_identity is None:
            _renameat2(dir_fd, tmp_name, dir_fd, name, _RENAME_NOREPLACE)
            published = True
            tmp_name = None
            tmp_entry_identity = None
        else:
            # Exchange is the leaf-identity CAS: if a racer replaced the
            # destination after the proof above, that replacement lands at
            # tmp_name.  Inspect it, and exchange back before refusing.
            _renameat2(dir_fd, tmp_name, dir_fd, name, _RENAME_EXCHANGE)
            published = True
            exchanged_st = _stat_leaf(dir_fd, tmp_name)
            if exchanged_st is None or _identity(exchanged_st) != initial_identity:
                _renameat2(dir_fd, tmp_name, dir_fd, name, _RENAME_EXCHANGE)
                published = False
                raise StorageError(f"destination leaf: {name!r} identity changed")
            tmp_entry_identity = initial_identity

        try:
            _prove_leaf(dir_fd, name, written_identity, ctx="published leaf")
            prove()
            os.fsync(dir_fd)
            prove()
        except BaseException:
            tmp_name, tmp_entry_identity = _rollback_publication(
                dir_fd,
                name,
                tmp_name,
                initial_identity,
                _required_identity(written_identity, ctx="written leaf"),
            )
            published = False
            raise

        # For a replacement, the old complete file now occupies tmp_name.
        # Remove it only after the new name and its parent entry are durable.
        published = False
        if tmp_name is not None:
            _prove_leaf(dir_fd, tmp_name, initial_identity, ctx="replaced leaf")
            os.unlink(tmp_name, dir_fd=dir_fd)
            tmp_name = None
            tmp_entry_identity = None
            os.fsync(dir_fd)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"cannot write {name!r} relative to pinned directory: {exc}") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None and tmp_entry_identity is not None and not published:
            _unlink_if_identity(dir_fd, tmp_name, tmp_entry_identity)


def _required_identity(identity: _Identity | None, *, ctx: str) -> _Identity:
    if identity is None:  # pragma: no cover - internal invariant
        raise StorageError(f"cannot prove {ctx} identity")
    return identity


def _rollback_publication(
    dir_fd: int,
    name: str,
    tmp_name: str | None,
    initial_identity: _Identity | None,
    written_identity: _Identity,
) -> tuple[str | None, _Identity | None]:
    """Restore the pre-call leaf after a post-publication parent failure."""
    _prove_leaf(dir_fd, name, written_identity, ctx="rollback destination")
    if initial_identity is None:
        rollback_name: str | None = None
        for _ in range(_TEMP_NAME_ATTEMPTS):
            candidate = f".{name}.{secrets.token_hex(16)}.rollback"
            try:
                _renameat2(dir_fd, name, dir_fd, candidate, _RENAME_NOREPLACE)
            except FileExistsError:
                continue
            rollback_name = candidate
            break
        if rollback_name is None:
            raise StorageError(f"cannot roll back creation of {name!r}")
        try:
            _prove_leaf(dir_fd, rollback_name, written_identity, ctx="rollback leaf")
        except BaseException:
            _renameat2(dir_fd, rollback_name, dir_fd, name, _RENAME_NOREPLACE)
            raise
        os.unlink(rollback_name, dir_fd=dir_fd)
        _prove_leaf(dir_fd, rollback_name, None, ctx="rollback cleanup")
        result: tuple[str | None, _Identity | None] = (None, None)
    else:
        if tmp_name is None:  # pragma: no cover - internal invariant
            raise StorageError("cannot roll back replacement without original leaf")
        _prove_leaf(dir_fd, tmp_name, initial_identity, ctx="rollback original")
        _renameat2(dir_fd, tmp_name, dir_fd, name, _RENAME_EXCHANGE)
        result = (tmp_name, written_identity)
    os.fsync(dir_fd)
    return result


def atomic_write_bytes(path: Path, data: bytes, *, allowed_root: Path | None = None) -> None:
    """Atomically publish bytes through one identity-pinned parent directory."""
    _validate_against_root(path, allowed_root)
    parent = path.parent
    parent_fd: int | None = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        _validate_against_root(path, allowed_root)
        before = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            raise StorageError(f"cannot write {path}: parent is not a real directory")
        parent_identity = _identity(before)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        parent_fd = os.open(parent, flags)
        if _identity(os.fstat(parent_fd)) != parent_identity:
            raise StorageError(f"cannot write {path}: parent identity changed while pinning")

        def prove_parent() -> None:
            try:
                current = os.stat(parent, follow_symlinks=False)
            except OSError as exc:
                raise StorageError(f"cannot write {path}: parent identity changed: {exc}") from exc
            if _identity(current) != parent_identity:
                raise StorageError(f"cannot write {path}: parent identity changed")

        prove_parent()
        _atomic_write_bytes_pinned(
            parent_fd,
            path.name,
            data,
            prove_parent=prove_parent,
            reject_destination_symlink=allowed_root is not None,
        )
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _require_simple_leaf_name(name: object) -> str:
    """Reject anything but a single, ordinary path component.

    ``name`` drives every ``os.open``/``os.replace``/``os.unlink`` call in
    ``atomic_write_bytes_at`` as a *dir_fd*-relative path argument — those
    calls still accept ``/``, ``..``, and absolute strings, which would let
    a value the caller does not fully control escape the pinned directory
    even though the parent is opened by fd. Fail closed on anything but a
    single non-empty, non-dot, slash-free, backslash-free component.
    """
    if not isinstance(name, str) or not name:
        raise StorageError(f"invalid leaf name {name!r}: must be a non-empty string")
    if name in (".", ".."):
        raise StorageError(f"invalid leaf name {name!r}: must not be '.' or '..'")
    if "/" in name or "\\" in name:
        raise StorageError(f"invalid leaf name {name!r}: must not contain a path separator")
    if os.path.isabs(name):
        raise StorageError(f"invalid leaf name {name!r}: must not be absolute")
    return name


def atomic_write_bytes_at(dir_fd: int, name: str, data: bytes) -> None:
    """``atomic_write_bytes``, but every step resolved relative to a pinned,
    already-owned directory fd instead of a walked pathname.

    The caller supplies the already-validated parent object; this function
    duplicates and proves that fd, then never walks a parent pathname.  Temp
    creation is exclusive and no-follow.  Publication uses dirfd-relative
    ``renameat2`` collision/exchange semantics so a changed destination leaf
    is restored and refused instead of overwritten.  The file and parent
    directory are fsynced before success.
    """
    owned_fd: int | None = None
    try:
        owned_fd = os.dup(dir_fd)
        _atomic_write_bytes_pinned(owned_fd, name, data)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"cannot write {name} relative to pinned directory: {exc}") from exc
    finally:
        if owned_fd is not None:
            try:
                os.close(owned_fd)
            except OSError:
                pass


def atomic_write_json(
    path: Path, doc: Mapping[str, object], *, allowed_root: Path | None = None
) -> None:
    atomic_write_bytes(
        path,
        json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        allowed_root=allowed_root,
    )


def read_json(path: Path, *, allowed_root: Path | None = None) -> dict[str, object]:
    _validate_against_root(path, allowed_root)
    parent = path.parent
    parent_fd: int | None = None
    try:
        parent_st = os.stat(parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_st.st_mode):
            raise StorageError(f"cannot read {path}: parent is not a directory")
        parent_identity = _identity(parent_st)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        parent_fd = os.open(parent, flags)
        if _identity(os.fstat(parent_fd)) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
        current_parent = os.stat(parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
        leaf_st = _stat_leaf(parent_fd, path.name)
        if leaf_st is None:
            raise StorageError(f"cannot read {path}: file does not exist")
        if not stat.S_ISREG(leaf_st.st_mode) or stat.S_ISLNK(leaf_st.st_mode):
            raise StorageError(f"cannot read {path}: refusing a non-regular leaf")
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | (os.O_CLOEXEC if hasattr(os, "O_CLOEXEC") else 0),
            dir_fd=parent_fd,
        )
        try:
            opened_st = os.fstat(fd)
            if _identity(opened_st) != _identity(leaf_st):
                raise StorageError(f"cannot read {path}: leaf identity changed")
            current_parent = os.stat(parent, follow_symlinks=False)
            if _identity(current_parent) != parent_identity:
                raise StorageError(f"cannot read {path}: parent identity changed")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                raw = handle.read()
            _prove_leaf(parent_fd, path.name, _identity(opened_st), ctx="read leaf")
            current_parent = os.stat(parent, follow_symlinks=False)
            if _identity(current_parent) != parent_identity:
                raise StorageError(f"cannot read {path}: parent identity changed")
        finally:
            if fd != -1:
                os.close(fd)
        loaded = json.loads(raw.decode("utf-8"))
    except OSError as exc:
        raise StorageError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StorageError(f"{path} is not valid JSON: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise StorageError(f"{path} is not valid UTF-8: {exc}") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    if not isinstance(loaded, dict):
        raise StorageError(f"{path} must contain a JSON object")
    return loaded


def append_jsonl(
    path: Path, doc: Mapping[str, object], *, allowed_root: Path | None = None
) -> None:
    _validate_against_root(path, allowed_root)
    line = json.dumps(doc, ensure_ascii=False) + "\n"
    parent_fd: int | None = None
    fd = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_against_root(path, allowed_root)
        parent_st = os.stat(path.parent, follow_symlinks=False)
        if not stat.S_ISDIR(parent_st.st_mode):
            raise StorageError(f"cannot append to {path}: parent is not a directory")
        parent_identity = _identity(parent_st)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        parent_fd = os.open(path.parent, flags)
        if _identity(os.fstat(parent_fd)) != parent_identity:
            raise StorageError(f"cannot append to {path}: parent identity changed")
        current_parent = os.stat(path.parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot append to {path}: parent identity changed")
        existing = _stat_leaf(parent_fd, path.name)
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or stat.S_ISLNK(existing.st_mode)
        ):
            raise StorageError(f"cannot append to {path}: destination is not a file leaf")
        open_flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            open_flags |= os.O_CLOEXEC
        if existing is None:
            open_flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(path.name, open_flags, 0o600, dir_fd=parent_fd)
        opened_st = os.fstat(fd)
        if not stat.S_ISREG(opened_st.st_mode) or (
            existing is not None and _identity(opened_st) != _identity(existing)
        ):
            raise StorageError(f"cannot append to {path}: leaf identity changed")
        current_parent = os.stat(path.parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot append to {path}: parent identity changed")
        payload = line.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        os.fsync(fd)
        _prove_leaf(parent_fd, path.name, _identity(opened_st), ctx="append leaf")
        current_parent = os.stat(path.parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot append to {path}: parent identity changed")
    except OSError as exc:
        raise StorageError(f"cannot append to {path}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


def read_jsonl(path: Path, *, allowed_root: Path | None = None) -> list[dict[str, object]]:
    """Strict, fail-closed JSONL reader (currently only used for Conversation
    transcripts): a missing file is a valid empty history, but any existing
    leaf must be a regular file whose every nonblank line is valid UTF-8 JSON
    encoding a JSON object — anything else (malformed JSON, invalid encoding,
    a scalar/list/string/null row, a directory/device-like leaf, or an I/O
    error) becomes a Dagvane ``StorageError`` rather than a silently dropped
    row or a raw ``JSONDecodeError``/``OSError`` escaping to the caller."""
    _validate_against_root(path, allowed_root)
    parent = path.parent
    parent_fd: int | None = None
    fd = -1
    try:
        try:
            parent_st = os.stat(parent, follow_symlinks=False)
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(parent_st.st_mode):
            raise StorageError(f"cannot read {path}: parent is not a directory")
        parent_identity = _identity(parent_st)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        parent_fd = os.open(parent, flags)
        if _identity(os.fstat(parent_fd)) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
        current_parent = os.stat(parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
        leaf_st = _stat_leaf(parent_fd, path.name)
        if leaf_st is None:
            return []
        if not stat.S_ISREG(leaf_st.st_mode) or stat.S_ISLNK(leaf_st.st_mode):
            raise StorageError(f"cannot read {path}: not a regular file")
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | (os.O_CLOEXEC if hasattr(os, "O_CLOEXEC") else 0),
            dir_fd=parent_fd,
        )
        opened_st = os.fstat(fd)
        if _identity(opened_st) != _identity(leaf_st):
            raise StorageError(f"cannot read {path}: leaf identity changed")
        current_parent = os.stat(parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
        raw = os.read(fd, max(1, os.fstat(fd).st_size))
        while len(raw) < os.fstat(fd).st_size:
            chunk = os.read(fd, os.fstat(fd).st_size - len(raw))
            if not chunk:
                break
            raw += chunk
        _prove_leaf(parent_fd, path.name, _identity(opened_st), ctx="read JSONL leaf")
        current_parent = os.stat(parent, follow_symlinks=False)
        if _identity(current_parent) != parent_identity:
            raise StorageError(f"cannot read {path}: parent identity changed")
    except OSError as exc:
        raise StorageError(f"cannot read {path}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
    rows: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError(f"{path}: invalid UTF-8 line: {exc}") from exc
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StorageError(f"{path}: invalid JSON line: {exc}") from exc
        if not isinstance(loaded, dict):
            raise StorageError(f"{path}: JSONL row must be a JSON object, got {loaded!r}")
        rows.append(loaded)
    return rows


class Workspace:
    """One target project directory and its ``.dagvane/`` state root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state_dir = self.root / ".dagvane"
        self.config_path = self.state_dir / "config.toml"
        self.conversations_dir = self.state_dir / "conversations"
        self.goals_dir = self.state_dir / "goals"
        self.agent_runs_dir = self.state_dir / "agent-runs"
        self.worktrees_dir = self.state_dir / "worktrees"

    def ensure(self) -> None:
        """Create the state layout; self-ignore so target repos stay clean."""
        if not self.root.is_dir() or self.root.is_symlink():
            raise StorageError(f"{self.root}: workspace root must be a real directory")

        ensure_expected_descendant(self.root, self.state_dir)
        self._ensure_directory(self.state_dir)

        for directory in (
            self.conversations_dir,
            self.goals_dir,
            self.agent_runs_dir,
            self.worktrees_dir,
        ):
            ensure_expected_descendant(self.state_dir, directory)
            self._ensure_directory(directory)

        gitignore = self.state_dir / ".gitignore"
        if gitignore.is_symlink():
            raise StorageError(f"{gitignore}: refusing to follow a symlink")
        if not gitignore.exists():
            atomic_write_bytes(gitignore, b"*\n", allowed_root=self.state_dir)

    @staticmethod
    def _ensure_directory(directory: Path) -> None:
        if directory.is_symlink():
            raise StorageError(f"{directory}: must not be a symlink")
        if directory.exists() and not directory.is_dir():
            raise StorageError(f"{directory}: must be a directory")
        directory.mkdir(parents=True, exist_ok=True)
