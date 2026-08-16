"""Managed disposable Git worktrees (R1-A security remediation).

Every disposable worktree Dagvane creates is bound to a durable *owner
record* outside the target, at the internally derived
``<worktrees>/.owners/<target-leaf>.json``. The record binds a schema
version, a lifecycle state
(``preclaim``/``abandoned``/``claimed``/``ready``/``advancing``/
``removing``/``removed``), the
canonical repository root, Git common dir, worktrees root and target path,
the device/inode identity of each authority directory and of the target,
the validated owner identity (Goal ID, optional run ID), a closed purpose,
the exact commit SHA, and an unpredictable per-generation nonce.

``create`` returns a generation-bearing handle, not a raw path as cleanup
authority.  Its per-target, no-follow ``flock`` lease remains held from the
first claim through all baseline use and proven removal.  A second caller
therefore cannot recycle a live generation. Every record transition and
terminal tombstone checkpoints through the fixed, pinned record inode and
compares its nonce, state and complete owner binding with the handle. The
bounded checksummed slot ring retains an older committed full-state slot until
the next slot is durable. Stale handles and replaced records fail closed
without overwriting or unlinking replacement bytes.

Protocol. A fresh create first persists a complete, exclusive ``preclaim``
record carrying an unpredictable nonce, then creates that generation's
private empty staging directory. Its captured identity is CAS-persisted as
``claimed`` before the same inode is published at the public target and Git
may populate it through an inherited descriptor path. ``git worktree add
--detach`` is followed by exact
target-identity and raw-porcelain registration proof (path, SHA, detached,
non-bare), then a CAS transition to ``ready``.

Removal never passes the public target to recursive Git cleanup. It proves
the same identity/registration, CAS-transitions to ``removing``, uses
``git worktree move`` to relocate that registration to a nonce-private
quarantine, re-proves the moved inode and registration, and only then runs
``git worktree remove --force`` through an ``O_NOFOLLOW`` directory fd for the
quarantine. Absence is re-proved before a durable ``removed/absent`` tombstone
is checkpointed in the owner journal. A crash between steps converges on the same
nonce/inode; foreign or replacement state is preserved. There is no
``shutil.rmtree``, no ``git worktree prune``, and no glob cleanup anywhere in
this protocol.

``create`` implements fresh disposable semantics for baseline, verification,
and review views. Candidate generations use ``acquire_existing`` /
``create_candidate`` and are retained when their lease closes. Candidate SHA
changes are a durable two-phase protocol: ``begin_sha_advance`` records an
``advancing`` token before Git may commit, while ``complete_sha_advance``
accepts only a clean detached one-child commit and rebinds the same generation.
Re-acquisition reconciles either crash window from the owner record and Git
registration without trusting caller state or recreating the checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dagvane.adapters.localexec import GitOps
from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError, StorageError
from dagvane.workspace.paths import rename_noreplace, require_canonical_root

try:  # Managed workspace execution is currently POSIX-only.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platform
    fcntl = None  # type: ignore[assignment]

_OWNERS_DIR = ".owners"
_RECORD_VERSION = 5
_LEGACY_RECORD_VERSION = 4
_MAX_RECORD_BYTES = 262144
_MAX_RECORD_DOC_BYTES = 16384
_FRAME_PREFIX = b"\x1e"
_FRAME_HEADER_BYTES = 75  # prefix + 8-hex length + ':' + sha256 + ':'
_CHECKPOINT_MAGIC = b"\xa5DVWTv5\0"
_CHECKPOINT_HEADER = struct.Struct(">8sQH32s")
_CHECKPOINT_SLOT_BYTES = 32768
_CHECKPOINT_SLOT_COUNT = _MAX_RECORD_BYTES // _CHECKPOINT_SLOT_BYTES
_CHECKPOINT_SEQUENCE_MASK = (1 << 64) - 1
_LEGACY_RECOVERY_MAGIC = b"\xa5DVWTR4\0"
_LEGACY_RECOVERY_HEADER = struct.Struct(">8sQQH32s")
_LEGACY_RECOVERY_BYTES = _CHECKPOINT_SLOT_BYTES
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_STATES = (
    "preclaim",
    "abandoned",
    "claimed",
    "ready",
    "advancing",
    "removing",
    "removed",
)
_LOCATIONS = ("staging", "public", "quarantine", "absent")
_RECORD_KEYS = frozenset(
    {
        "version",
        "state",
        "repo",
        "repo_dev",
        "repo_ino",
        "common_dir",
        "common_dir_dev",
        "common_dir_ino",
        "worktrees_root",
        "worktrees_root_dev",
        "worktrees_root_ino",
        "owners_dir",
        "owners_dir_dev",
        "owners_dir_ino",
        "lease",
        "lease_dev",
        "lease_ino",
        "target",
        "target_dev",
        "target_ino",
        "goal",
        "run_id",
        "purpose",
        "sha",
        "advance_token",
        "nonce",
        "location",
    }
)


class WorktreePurpose(StrEnum):
    """The closed set of reasons a managed worktree may exist."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"
    VERIFY = "verify"
    REVIEW = "review"


def validate_worktree_sha(value: object, *, ctx: str) -> str:
    """Exact lowercase 40-hex commit SHA only: no refs, no abbreviations,
    no case folding — the accepted value is returned unchanged."""
    if not isinstance(value, str) or not _SHA40_RE.fullmatch(value):
        raise SpecError(
            f"{ctx}: {value!r} must be an exact lowercase 40-hex commit SHA"
        )
    return value


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    """A validated managed-worktree identity: who owns it, why, at what SHA.

    A baseline worktree belongs to the Goal alone (``run_id`` must be
    ``None``); every other purpose requires a validated run ID.
    """

    goal_name: str
    purpose: WorktreePurpose
    sha: str
    run_id: str | None = None

    def __post_init__(self) -> None:
        validate_filesystem_id(self.goal_name, ctx="worktree spec: goal_name")
        if not isinstance(self.purpose, WorktreePurpose):
            raise SpecError("worktree spec: purpose must be a WorktreePurpose")
        validate_worktree_sha(self.sha, ctx="worktree spec: sha")
        if self.purpose is WorktreePurpose.BASELINE:
            if self.run_id is not None:
                raise SpecError("worktree spec: a baseline worktree carries no run_id")
        else:
            validate_filesystem_id(self.run_id, ctx="worktree spec: run_id")

    @property
    def target_leaf(self) -> str:
        """The deterministic target directory name (the names the existing
        workflow already uses are preserved exactly)."""
        if self.purpose is WorktreePurpose.BASELINE:
            return f"{self.goal_name}-baseline"
        if self.purpose is WorktreePurpose.CANDIDATE:
            return f"{self.goal_name}-{self.run_id}"
        return f"{self.goal_name}-{self.run_id}-{self.purpose.value}"


@dataclass(frozen=True, slots=True)
class PorcelainWorktree:
    """One strictly parsed ``git worktree list --porcelain -z`` entry."""

    path: str
    head: str | None
    branch: str | None
    detached: bool
    bare: bool


def _malformed(reason: str) -> StorageError:
    return StorageError(f"git worktree list --porcelain -z: {reason}")


def _decode(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _malformed(f"non-UTF-8 attribute line {line!r}") from exc


def _parse_entry(lines: list[bytes]) -> PorcelainWorktree:
    first = _decode(lines[0])
    if not first.startswith("worktree "):
        raise _malformed(f"entry must start with a worktree line, got {first!r}")
    path = first[len("worktree ") :]
    if not path or not os.path.isabs(path):
        raise _malformed(f"non-absolute worktree path {path!r}")
    head: str | None = None
    branch: str | None = None
    detached = False
    bare = False
    seen: set[str] = set()
    for raw in lines[1:]:
        line = _decode(raw)
        key = line.split(" ", 1)[0]
        if key in seen or key == "worktree":
            raise _malformed(f"duplicate {key!r} attribute for {path!r}")
        seen.add(key)
        if key == "HEAD":
            value = line[len("HEAD ") :] if line.startswith("HEAD ") else ""
            if not _SHA40_RE.fullmatch(value):
                raise _malformed(f"invalid HEAD {value!r} for {path!r}")
            head = value
        elif key == "branch":
            value = line[len("branch ") :] if line.startswith("branch ") else ""
            if not value:
                raise _malformed(f"empty branch attribute for {path!r}")
            branch = value
        elif key == "detached":
            if line != "detached":
                raise _malformed(f"malformed detached attribute for {path!r}")
            detached = True
        elif key == "bare":
            if line != "bare":
                raise _malformed(f"malformed bare attribute for {path!r}")
            bare = True
        elif key in ("locked", "prunable"):
            pass  # flag with optional free-text reason; carries no authority
        else:
            raise _malformed(f"unknown attribute {line!r} for {path!r}")
    if bare:
        if head is not None or branch is not None or detached:
            raise _malformed(f"bare entry {path!r} carries checkout attributes")
    else:
        if head is None:
            raise _malformed(f"entry {path!r} has no HEAD")
        if detached == (branch is not None):
            raise _malformed(
                f"entry {path!r} must carry exactly one of detached/branch"
            )
    return PorcelainWorktree(
        path=path, head=head, branch=branch, detached=detached, bare=bare
    )


def parse_worktree_list_z(raw: bytes) -> dict[str, PorcelainWorktree]:
    """Strict, closed parser for raw ``git worktree list --porcelain -z``.

    Every attribute line is NUL-terminated; an entry ends with one extra
    NUL. Anything else — truncated output, an empty entry, an unknown or
    duplicate attribute, an invalid HEAD, a non-absolute path, a duplicate
    target, an ambiguous branch/detached combination — fails closed."""
    if not raw:
        raise _malformed("empty output (a repository always lists itself)")
    tokens = raw.split(b"\0")
    if tokens[-1] != b"":
        raise _malformed("truncated output (missing final NUL)")
    entries: dict[str, PorcelainWorktree] = {}
    lines: list[bytes] = []
    for token in tokens[:-1]:
        if token != b"":
            lines.append(token)
            continue
        if not lines:
            raise _malformed("empty entry")
        entry = _parse_entry(lines)
        if entry.path in entries:
            raise _malformed(f"duplicate worktree entry {entry.path!r}")
        entries[entry.path] = entry
        lines = []
    if lines:
        raise _malformed("output ends inside an entry (missing terminator)")
    return entries


class GitWorktreePlumbing(Protocol):
    """The exact, closed set of Git commands the managed protocol may run."""

    def common_dir(self, repo: Path, repo_fd: int) -> Path: ...

    def list_z(self, repo: Path, repo_fd: int, common_fd: int) -> bytes: ...

    def add_detached(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int, sha: str
    ) -> None: ...

    def move_exact(
        self,
        repo: Path,
        repo_fd: int,
        common_fd: int,
        source_fd: int,
        target_parent_fd: int,
        target_leaf: str,
    ) -> None: ...

    def remove_forced_fd(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int
    ) -> None: ...

    def is_clean(self, worktree: Path) -> bool: ...

    def commit_parents(
        self, repo: Path, repo_fd: int, common_fd: int, sha: str
    ) -> tuple[str, ...]: ...


class LocalGitWorktreePlumbing:
    """Real Git plumbing, delegating to the subprocess-allowlisted adapter."""

    def common_dir(self, repo: Path, repo_fd: int) -> Path:
        with GitOps.pinned_worktree_authority(repo_fd, None):
            return GitOps.common_dir(repo)

    def list_z(self, repo: Path, repo_fd: int, common_fd: int) -> bytes:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            return GitOps.worktree_list_porcelain_z(repo)

    def add_detached(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int, sha: str
    ) -> None:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            GitOps.worktree_add_detached_exact(repo, target_fd, sha)

    def move_exact(
        self,
        repo: Path,
        repo_fd: int,
        common_fd: int,
        source_fd: int,
        target_parent_fd: int,
        target_leaf: str,
    ) -> None:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            GitOps.worktree_move_exact(
                repo, source_fd, target_parent_fd, target_leaf
            )

    def remove_forced_fd(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int
    ) -> None:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            GitOps.worktree_remove_forced_fd(repo, target_fd)

    def is_clean(self, worktree: Path) -> bool:
        return GitOps.is_clean(worktree)

    def commit_parents(
        self, repo: Path, repo_fd: int, common_fd: int, sha: str
    ) -> tuple[str, ...]:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            return GitOps.commit_parents(repo, sha)


Identity = tuple[int, int]


class _PinnedDirectory:
    """One canonical directory path pinned by fd and re-proved by identity."""

    def __init__(self, path: Path, *, ctx: str) -> None:
        self.path = path
        self._ctx = ctx
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise StorageError(f"{ctx}: cannot pin {path}: {exc}") from exc
        try:
            st = os.fstat(self.fd)
            self.identity: Identity = (st.st_dev, st.st_ino)
            self.prove()
        except BaseException:
            os.close(self.fd)
            raise

    def prove(self) -> None:
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise StorageError(f"{self._ctx}: {self.path} vanished: {exc}") from exc
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self.identity:
            raise StorageError(
                f"{self._ctx}: {self.path} no longer names the pinned directory"
            )

    def close(self) -> None:
        os.close(self.fd)


class _PinnedGitAuthority:
    """Pinned repository root and the exact Git common-directory authority."""

    def __init__(
        self, repo: Path, git: GitWorktreePlumbing, *, ctx: str
    ) -> None:
        self.repo = _PinnedDirectory(repo, ctx=f"{ctx}: repository root")
        self.common: _PinnedDirectory | None = None
        try:
            self.repo.prove()
            common_path = require_canonical_root(
                git.common_dir(repo, self.repo.fd),
                ctx=f"{ctx}: Git common directory",
            )
            self.repo.prove()
            self.common = _PinnedDirectory(
                common_path, ctx=f"{ctx}: Git common directory"
            )
            confirmed_common = require_canonical_root(
                git.common_dir(repo, self.repo.fd),
                ctx=f"{ctx}: Git common directory confirmation",
            )
            if confirmed_common != common_path:
                raise StorageError(
                    f"{ctx}: Git common directory changed while being pinned"
                )
            self.prove()
        except BaseException:
            try:
                if self.common is not None:
                    self.common.close()
            finally:
                self.repo.close()
            raise

    def prove(self) -> None:
        self.repo.prove()
        assert self.common is not None
        self.common.prove()

    @property
    def repo_fd(self) -> int:
        return self.repo.fd

    @property
    def common_fd(self) -> int:
        assert self.common is not None
        return self.common.fd

    def close(self) -> None:
        try:
            if self.common is not None:
                self.common.close()
        finally:
            self.repo.close()


class _PinnedWorktreesRoot:
    """The worktrees root and its ``.owners`` child pinned by directory fds."""

    def __init__(self, root: Path, *, ctx: str) -> None:
        self._ctx = ctx
        self._root = root
        self._pin = _PinnedDirectory(root, ctx=f"{ctx}: worktrees root")
        self.root_fd = self._pin.fd
        self.owners_fd: int | None = None
        self._owners_identity: Identity | None = None

    @property
    def root_identity(self) -> Identity:
        return self._pin.identity

    @property
    def owners_identity(self) -> Identity:
        if self._owners_identity is None:
            raise StorageError(f"{self._ctx}: owner-record directory is not pinned")
        return self._owners_identity

    def prove_root(self) -> None:
        self._pin.prove()

    def prove(self) -> None:
        self.prove_root()
        if self.owners_fd is None:
            return
        try:
            st = os.stat(_OWNERS_DIR, dir_fd=self.root_fd, follow_symlinks=False)
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: owner-record directory vanished: {exc}"
            ) from exc
        if not stat.S_ISDIR(st.st_mode) or (
            st.st_dev,
            st.st_ino,
        ) != self._owners_identity:
            raise StorageError(
                f"{self._ctx}: owner-record directory no longer names the "
                "pinned directory"
            )

    def open_owners(self, *, create: bool) -> bool:
        """Pin ``.owners`` and fsync the worktrees parent when it is created."""
        if self.owners_fd is not None:
            return True
        self.prove_root()
        created = False
        if create:
            try:
                os.mkdir(_OWNERS_DIR, mode=0o700, dir_fd=self.root_fd)
                created = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise StorageError(
                    f"{self._ctx}: cannot create owner-record directory: {exc}"
                ) from exc
            if created:
                try:
                    os.fsync(self.root_fd)
                except OSError as exc:
                    raise StorageError(
                        f"{self._ctx}: cannot fsync worktrees root after "
                        f"creating {_OWNERS_DIR}: {exc}"
                    ) from exc
        try:
            fd = os.open(
                _OWNERS_DIR,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        except FileNotFoundError:
            if create:
                raise StorageError(
                    f"{self._ctx}: owner-record directory vanished after creation"
                ) from None
            return False
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: owner-record directory {_OWNERS_DIR!r} is not a "
                f"real directory (symlink?): {exc}"
            ) from exc
        try:
            st = os.fstat(fd)
            self._owners_identity = (st.st_dev, st.st_ino)
            self.owners_fd = fd
            self.prove()
        except BaseException:
            os.close(fd)
            self.owners_fd = None
            self._owners_identity = None
            raise
        return True

    def stat_target(self, leaf: str) -> os.stat_result | None:
        self.prove_root()
        try:
            return os.stat(leaf, dir_fd=self.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"{self._ctx}: cannot stat target {leaf!r}: {exc}") from exc

    def create_empty_target(self, leaf: str) -> Identity:
        self.prove_root()
        try:
            os.mkdir(leaf, mode=0o700, dir_fd=self.root_fd)
        except OSError as exc:
            raise StorageError(f"{self._ctx}: cannot create target {leaf!r}: {exc}") from exc
        st = self.stat_target(leaf)
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise StorageError(f"{self._ctx}: created target is not a real directory")
        try:
            os.fsync(self.root_fd)
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: cannot fsync worktrees root after target creation: {exc}"
            ) from exc
        return (st.st_dev, st.st_ino)

    def entry_absent(self, leaf: str) -> bool:
        return self.stat_target(leaf) is None

    def prove_target(self, leaf: str, identity: Identity) -> None:
        st = self.stat_target(leaf)
        if st is None or not stat.S_ISDIR(st.st_mode) or (
            st.st_dev,
            st.st_ino,
        ) != identity:
            raise StorageError(
                f"{self._ctx}: target {leaf!r} no longer names the owned "
                f"directory dev={identity[0]} ino={identity[1]}"
            )

    def open_target_fd(self, leaf: str, identity: Identity) -> int:
        """Open and return the exact owned directory inode, never a symlink.

        The caller owns the returned fd. Re-proving the public leaf after the
        open closes the path-to-fd binding gap before an fd-bound effect.
        """
        self.prove_target(leaf, identity)
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: cannot pin owned target {leaf!r}: {exc}"
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != identity:
                raise StorageError(
                    f"{self._ctx}: opened target {leaf!r} is not the owned inode"
                )
            self.prove_target(leaf, identity)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def prove_absent(self, leaf: str) -> None:
        if self.stat_target(leaf) is not None:
            raise StorageError(
                f"{self._ctx}: entry {leaf!r} unexpectedly exists; refusing"
            )

    def target_is_empty(self, leaf: str, identity: Identity) -> bool:
        self.prove_target(leaf, identity)
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self.root_fd,
            )
        except OSError as exc:
            raise StorageError(f"{self._ctx}: cannot pin target {leaf!r}: {exc}") from exc
        try:
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != identity:
                raise StorageError(f"{self._ctx}: target changed while being opened")
            return not os.listdir(fd)
        finally:
            os.close(fd)

    def close(self) -> None:
        try:
            if self.owners_fd is not None:
                os.close(self.owners_fd)
        finally:
            self._pin.close()


class _TargetLease:
    """No-follow per-target flock kept for the whole managed lifecycle."""

    def __init__(
        self,
        pinned: _PinnedWorktreesRoot,
        leaf: str,
        *,
        ctx: str,
        create: bool,
    ) -> None:
        if fcntl is None:  # pragma: no cover - non-POSIX platform
            raise SpecError("managed worktrees require POSIX flock")
        if not pinned.open_owners(create=create):
            raise StorageError(f"{ctx}: no existing owner-record authority")
        assert pinned.owners_fd is not None
        self._pinned = pinned
        self._name = leaf + ".lock"
        self._ctx = ctx
        self._fd: int | None = None
        created = False
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            if create:
                try:
                    fd = os.open(
                        self._name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=pinned.owners_fd,
                    )
                    created = True
                except FileExistsError:
                    fd = os.open(self._name, flags, dir_fd=pinned.owners_fd)
            else:
                fd = os.open(self._name, flags, dir_fd=pinned.owners_fd)
        except FileNotFoundError:
            raise StorageError(f"{ctx}: no existing lifecycle lease") from None
        except OSError as exc:
            raise StorageError(f"{ctx}: cannot open lifecycle lease: {exc}") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(f"{ctx}: lifecycle lease is not a regular file")
            self._identity: Identity = (st.st_dev, st.st_ino)
            if created:
                os.fsync(fd)
                os.fsync(pinned.owners_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise StorageError(
                    f"{ctx}: lifecycle is active in another process; "
                    "the generation cannot be stolen"
                ) from None
            self._fd = fd
            self.prove()
        except BaseException:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                self._fd = None
            else:
                os.close(fd)
            raise

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def name(self) -> str:
        return self._name

    @property
    def identity(self) -> Identity:
        return self._identity

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise StorageError(f"{self._ctx}: lifecycle handle is closed")
        return self._fd

    def prove(self) -> None:
        if self._fd is None:
            raise StorageError(f"{self._ctx}: lifecycle handle is closed")
        self._pinned.prove()
        assert self._pinned.owners_fd is not None
        try:
            st = os.stat(
                self._name,
                dir_fd=self._pinned.owners_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StorageError(f"{self._ctx}: lifecycle lease vanished: {exc}") from exc
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != self._identity:
            raise StorageError(
                f"{self._ctx}: lifecycle lease path no longer names the locked inode"
            )

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            assert fcntl is not None
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(slots=True)
class _OwnerRecord:
    state: str
    nonce: str
    location: str
    target_identity: Identity
    bound_sha: str
    advance_token: str | None


@dataclass(slots=True)
class _RecordSnapshot:
    data: bytes
    identity: Identity
    valid_size: int
    file_size: int
    sequence: int | None
    slot_index: int | None
    recovered_from_lease: bool


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    data: bytes
    sequence: int
    slot_index: int
    frame_size: int


@dataclass(slots=True)
class _Generation:
    nonce: str
    state: str
    location: str
    target_identity: Identity
    record_identity: Identity
    bound_sha: str
    advance_token: str | None


@dataclass(slots=True)
class _LifecycleSession:
    spec: WorktreeSpec
    target: Path
    ctx: str
    pinned: _PinnedWorktreesRoot
    authority: _PinnedGitAuthority
    lease: _TargetLease
    expected: dict[str, object]
    generation: _Generation | None = None
    active: bool = True

    def prove(self) -> None:
        if not self.active:
            raise StorageError(f"{self.ctx}: lifecycle handle is closed")
        self.lease.prove()
        self.authority.prove()
        self.pinned.prove()

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            self.lease.close()
        finally:
            try:
                self.authority.close()
            finally:
                self.pinned.close()


class ManagedWorktreeHandle:
    """Generation-bound authority for one active managed worktree.

    ``close`` only releases the lifecycle lease (modeling process death or
    abandonment); it never deletes bytes. Disposable-view callers pass the
    live handle to :meth:`ManagedWorktrees.remove`, which proves and removes
    the same generation before releasing it. Candidate callers deliberately
    close and retain the generation as durable owner evidence.
    """

    __slots__ = ("_manager_token", "_session", "_generation")

    def __init__(self, manager_token: object, session: _LifecycleSession) -> None:
        generation = session.generation
        if generation is None or generation.state != "ready":
            raise StorageError(f"{session.ctx}: cannot issue a non-ready handle")
        self._manager_token = manager_token
        self._session = session
        self._generation = generation.nonce

    @property
    def path(self) -> Path:
        return self._session.target

    @property
    def generation(self) -> str:
        return self._generation

    @property
    def bound_sha(self) -> str:
        """Manager-authoritative commit bound to this live generation."""
        generation = self._session.generation
        if generation is None or generation.nonce != self._generation:
            raise StorageError(
                f"{self._session.ctx}: stale generation has no SHA authority"
            )
        return generation.bound_sha

    @property
    def active(self) -> bool:
        return self._session.active

    @contextmanager
    def pinned_authority(self) -> Iterator[tuple[int, int, Identity]]:
        """Yield the live target fd, Git common-dir fd, and target identity.

        The session's lifecycle proofs run before and after the fd is exposed,
        so callers can bind both shell cwd and candidate Git operations to the
        same managed generation without reopening its public pathname.
        """
        session = self._session
        session.prove()
        generation = session.generation
        if generation is None or generation.nonce != self._generation:
            raise StorageError(f"{session.ctx}: stale generation authority")
        target_fd = session.pinned.open_target_fd(
            session.spec.target_leaf, generation.target_identity
        )
        try:
            session.prove()
            yield target_fd, session.authority.common_fd, generation.target_identity
            session.prove()
        finally:
            os.close(target_fd)

    def close(self) -> None:
        """Release without cleanup; retry may recover this durable generation."""
        self._session.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort fd hygiene
        try:
            self.close()
        except BaseException:
            pass


@dataclass(frozen=True, slots=True)
class _ShaAdvanceToken:
    """Unforgeable in-process capability for one durable advancing record."""

    manager_token: object
    generation: str
    token: str
    old_sha: str


class ManagedWorktrees:
    """Typed, fail-closed lifecycle for Dagvane's disposable Git worktrees."""

    def __init__(
        self,
        *,
        repo_root: Path,
        worktrees_root: Path,
        git: GitWorktreePlumbing | None = None,
    ) -> None:
        self._repo = require_canonical_root(repo_root, ctx="managed worktrees: repo root")
        self._root = require_canonical_root(
            worktrees_root, ctx="managed worktrees: worktrees root"
        )
        self._git = git if git is not None else LocalGitWorktreePlumbing()
        self._handle_token = object()

    @staticmethod
    def _require_spec(spec: object) -> WorktreeSpec:
        if not isinstance(spec, WorktreeSpec):
            raise SpecError("managed worktrees: spec must be a WorktreeSpec")
        return spec

    def target_path(self, spec: WorktreeSpec) -> Path:
        return self._root / self._require_spec(spec).target_leaf

    def create(self, spec: WorktreeSpec) -> ManagedWorktreeHandle:
        """Create one fresh disposable generation and keep its lease held."""
        spec = self._require_spec(spec)
        if spec.purpose is WorktreePurpose.CANDIDATE:
            raise SpecError(
                "managed worktrees: candidate creation requires create_candidate"
            )
        session: _LifecycleSession | None = None
        try:
            session, snapshot = self._open_session(spec)
            if snapshot is None:
                self._prove_unclaimed_absence(session)
                self._fresh_create(session)
            else:
                record = self._load_generation(
                    session, snapshot, expected_sha=None
                )
                if (
                    record.state not in ("removed", "abandoned")
                    and record.bound_sha != spec.sha
                ):
                    raise StorageError(
                        f"{session.ctx}: owner record SHA {record.bound_sha} "
                        f"does not match requested disposable SHA {spec.sha}"
                    )
                if record.state == "preclaim":
                    if self._finish_preclaim(session):
                        self._finish_partial_create(session)
                elif record.state == "claimed":
                    self._finish_partial_create(session)
                elif record.state == "advancing":
                    raise StorageError(
                        f"{session.ctx}: disposable generation cannot be advancing"
                    )
                elif record.state in ("removed", "abandoned"):
                    self._prove_no_public_target(session)
                    self._fresh_create(session)
                else:
                    self._finish_removal(
                        session, transition_first=(record.state == "ready")
                    )
                    self._fresh_create(session)
            return ManagedWorktreeHandle(self._handle_token, session)
        except BaseException:
            if session is not None:
                session.close()
            raise

    def acquire_existing(
        self, spec: WorktreeSpec
    ) -> ManagedWorktreeHandle | None:
        """Acquire only an already-created candidate generation.

        The caller's ``spec.sha`` is only a persisted-state assertion hint.
        The owner record is authoritative after the complete repository,
        owner, purpose, generation, inode and registration binding is proven.
        This path never creates a directory, starts a new generation, or calls
        ``git worktree add``. Incomplete preclaims are durably abandoned;
        incomplete claimed generations remain available to an explicit
        :meth:`create_candidate` call. ``None`` means there is no complete
        candidate checkout that acquisition may return.
        """
        spec = self._require_candidate_spec(spec)
        session: _LifecycleSession | None = None
        try:
            if self._prove_absent_acquisition_without_create(spec):
                return None
            session, snapshot = self._open_session(spec, create_authority=False)
            if snapshot is None:
                self._prove_unclaimed_absence(session)
                session.close()
                return None
            record = self._load_generation(session, snapshot, expected_sha=None)
            if record.state == "preclaim":
                self._abandon_preclaim_without_create(session)
                session.close()
                return None
            elif record.state == "claimed":
                if not self._acquire_claimed_without_create(session):
                    session.close()
                    return None
            elif record.state == "advancing":
                self._recover_sha_advance(session)
            elif record.state == "removing":
                self._finish_removal(session, transition_first=False)
                self._prove_unclaimed_absence(session)
                session.close()
                return None
            elif record.state == "removed":
                self._prove_no_public_target(session)
                session.close()
                return None
            elif record.state == "abandoned":
                self._prove_no_public_target(session)
                session.close()
                return None
            else:
                self._prove_bound_at(session, session.spec.target_leaf)
            return ManagedWorktreeHandle(self._handle_token, session)
        except BaseException:
            if session is not None:
                session.close()
            raise

    def create_candidate(self, spec: WorktreeSpec) -> ManagedWorktreeHandle:
        """Create a new candidate only when no generation or target exists."""
        spec = self._require_candidate_spec(spec)
        session: _LifecycleSession | None = None
        try:
            session, snapshot = self._open_session(spec)
            if snapshot is not None:
                # Do not fresh-replace retained candidate evidence. The caller
                # must acquire and reconcile the existing owner generation.
                record = self._load_generation(
                    session, snapshot, expected_sha=None
                )
                if record.state in ("removed", "abandoned"):
                    self._prove_no_public_target(session)
                    self._fresh_create(session)
                    return ManagedWorktreeHandle(self._handle_token, session)
                if record.state == "preclaim":
                    if self._finish_preclaim(session):
                        self._finish_partial_create(session)
                    return ManagedWorktreeHandle(self._handle_token, session)
                if record.state == "claimed":
                    self._finish_partial_create(session)
                    return ManagedWorktreeHandle(self._handle_token, session)
                raise StorageError(
                    f"{session.ctx}: an owned candidate generation already exists; "
                    "acquire it instead of recreating it"
                )
            self._prove_unclaimed_absence(session)
            self._fresh_create(session)
            return ManagedWorktreeHandle(self._handle_token, session)
        except BaseException:
            if session is not None:
                session.close()
            raise

    def prove_for_use(
        self,
        handle: object,
        *,
        expected_bound_sha: str,
    ) -> Path:
        """Re-prove one candidate generation immediately around an effect."""
        validate_worktree_sha(
            expected_bound_sha, ctx="managed worktrees: expected_bound_sha"
        )
        session = self._require_live_handle(handle, candidate=True)
        generation = session.generation
        assert generation is not None
        if generation.state not in ("ready", "advancing"):
            raise StorageError(
                f"{session.ctx}: generation state {generation.state!r} "
                "does not authorize candidate use"
            )
        if generation.bound_sha != expected_bound_sha:
            raise StorageError(
                f"{session.ctx}: handle is bound to {generation.bound_sha}, "
                f"not expected SHA {expected_bound_sha}"
            )
        self._require_generation_snapshot(session, state=generation.state)
        if generation.location != "public":
            raise StorageError(f"{session.ctx}: candidate generation is not public")
        self._prove_bound_at(session, session.spec.target_leaf)
        return session.target

    def begin_sha_advance(
        self,
        handle: object,
        *,
        expected_old_sha: str,
    ) -> object:
        """Durably authorize exactly one candidate commit from ``old_sha``."""
        validate_worktree_sha(
            expected_old_sha, ctx="managed worktrees: expected_old_sha"
        )
        session = self._require_live_handle(handle, candidate=True)
        generation = session.generation
        assert generation is not None
        if generation.state != "ready":
            raise StorageError(
                f"{session.ctx}: SHA advance requires a ready generation"
            )
        if generation.bound_sha != expected_old_sha:
            raise StorageError(
                f"{session.ctx}: SHA advance expected {expected_old_sha}, "
                f"but generation is bound to {generation.bound_sha}"
            )
        self._prove_bound_at(session, session.spec.target_leaf)
        token = secrets.token_hex(16)
        self._cas_transition(
            session,
            expected_state="ready",
            new_state="advancing",
            expected_location="public",
            new_advance_token=token,
            replace_advance_token=True,
        )
        self._prove_bound_at(session, session.spec.target_leaf)
        return _ShaAdvanceToken(
            manager_token=self._handle_token,
            generation=generation.nonce,
            token=token,
            old_sha=expected_old_sha,
        )

    def complete_sha_advance(
        self,
        handle: object,
        token: object,
        *,
        new_sha: str,
    ) -> None:
        """Bind the same generation to a proven clean one-child commit."""
        validate_worktree_sha(new_sha, ctx="managed worktrees: new_sha")
        session = self._require_live_handle(handle, candidate=True)
        generation = session.generation
        assert generation is not None
        if (
            not isinstance(token, _ShaAdvanceToken)
            or token.manager_token is not self._handle_token
            or token.generation != generation.nonce
            or token.token != generation.advance_token
            or token.old_sha != generation.bound_sha
        ):
            raise StorageError(f"{session.ctx}: stale or foreign SHA-advance token")
        if generation.state != "advancing":
            raise StorageError(
                f"{session.ctx}: generation has no active SHA advance"
            )
        self._require_generation_snapshot(session, state="advancing")
        self._prove_advanced_commit(session, old_sha=token.old_sha, new_sha=new_sha)
        self._cas_transition(
            session,
            expected_state="advancing",
            new_state="ready",
            expected_location="public",
            new_sha=new_sha,
            new_advance_token=None,
            replace_advance_token=True,
        )
        self._prove_bound_at(session, session.spec.target_leaf)

    def remove(self, handle: ManagedWorktreeHandle) -> None:
        """Remove exactly the live generation represented by ``handle``."""
        session = self._require_live_handle(handle)
        try:
            self._finish_removal(session, transition_first=True)
        finally:
            handle.close()

    @staticmethod
    def _require_candidate_spec(spec: object) -> WorktreeSpec:
        required = ManagedWorktrees._require_spec(spec)
        if required.purpose is not WorktreePurpose.CANDIDATE:
            raise SpecError(
                "managed worktrees: candidate lifecycle requires candidate purpose"
            )
        return required

    def _require_live_handle(
        self,
        handle: object,
        *,
        candidate: bool = False,
    ) -> _LifecycleSession:
        if not isinstance(handle, ManagedWorktreeHandle):
            raise SpecError(
                "managed worktrees: lifecycle operation requires a generation handle"
            )
        if handle._manager_token is not self._handle_token:
            raise SpecError("managed worktrees: handle belongs to another manager")
        session = handle._session
        if not session.active or handle.generation != (
            session.generation.nonce if session.generation is not None else None
        ):
            raise StorageError(f"{session.ctx}: stale or closed generation handle")
        if candidate and session.spec.purpose is not WorktreePurpose.CANDIDATE:
            raise SpecError(
                f"{session.ctx}: operation requires a candidate generation"
            )
        return session

    def _open_session(
        self, spec: WorktreeSpec, *, create_authority: bool = True
    ) -> tuple[_LifecycleSession, _RecordSnapshot | None]:
        target = self._root / spec.target_leaf
        ctx = f"managed worktree {spec.target_leaf}"
        pinned = _PinnedWorktreesRoot(self._root, ctx=ctx)
        lease: _TargetLease | None = None
        authority: _PinnedGitAuthority | None = None
        try:
            lease = _TargetLease(
                pinned,
                spec.target_leaf,
                ctx=ctx,
                create=create_authority,
            )
            snapshot = self._read_record_snapshot(pinned, spec, ctx, lease=lease)
            if snapshot is None and pinned.stat_target(spec.target_leaf) is not None:
                raise StorageError(
                    f"{ctx}: {target} exists but has no owner record; "
                    "refusing to adopt or touch it"
                )
            authority = _PinnedGitAuthority(self._repo, self._git, ctx=ctx)
            expected = self._expected_fields(spec, target, pinned, authority, lease)
            session = _LifecycleSession(
                spec=spec,
                target=target,
                ctx=ctx,
                pinned=pinned,
                authority=authority,
                lease=lease,
                expected=expected,
            )
            return session, snapshot
        except BaseException:
            if authority is not None:
                authority.close()
            if lease is not None:
                lease.close()
            pinned.close()
            raise

    def _prove_absent_acquisition_without_create(
        self, spec: WorktreeSpec
    ) -> bool:
        """Linearize a pristine candidate lookup without creating authority.

        A valid first generation publishes its complete preclaim record only
        after its lease exists and before it creates any target. Observing no
        record on both sides of target/registration absence therefore permits
        ``acquire_existing`` to return ``None`` without creating ``.owners`` or
        a lease. If a concurrent record appears, the caller retries through
        the ordinary locked existing-generation path.
        """
        target = self._root / spec.target_leaf
        ctx = f"managed worktree {spec.target_leaf}"
        pinned = _PinnedWorktreesRoot(self._root, ctx=ctx)
        authority: _PinnedGitAuthority | None = None

        def record_present() -> bool:
            if not pinned.open_owners(create=False):
                return False
            assert pinned.owners_fd is not None
            pinned.prove()
            try:
                st = os.stat(
                    self._record_name(spec),
                    dir_fd=pinned.owners_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise StorageError(
                    f"{ctx}: cannot inspect existing owner record: {exc}"
                ) from exc
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(f"{ctx}: owner record is not a regular file")
            pinned.prove()
            return True

        try:
            if record_present():
                return False
            if pinned.stat_target(spec.target_leaf) is not None:
                raise StorageError(
                    f"{ctx}: {target} exists but has no owner record; "
                    "refusing to adopt or touch it"
                )
            authority = _PinnedGitAuthority(self._repo, self._git, ctx=ctx)
            raw = self._git.list_z(
                self._repo, authority.repo_fd, authority.common_fd
            )
            authority.prove()
            pinned.prove_root()
            if parse_worktree_list_z(raw).get(str(target)) is not None:
                raise StorageError(
                    f"{ctx}: {target} is registered with Git without an owner "
                    "record; refusing to adopt it"
                )
            if record_present():
                return False
            if pinned.stat_target(spec.target_leaf) is not None:
                if record_present():
                    return False
                raise StorageError(
                    f"{ctx}: {target} appeared without an owner record; refusing"
                )
            authority.prove()
            pinned.prove()
            return True
        finally:
            if authority is not None:
                authority.close()
            pinned.close()

    def _prove_unclaimed_absence(self, session: _LifecycleSession) -> None:
        session.pinned.prove_absent(session.spec.target_leaf)
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: {session.target} is already registered with Git "
                "without this generation; refusing to adopt it"
            )

    def _prove_no_public_target(self, session: _LifecycleSession) -> None:
        """Prove a terminal/abandoned generation has no public checkout."""
        session.pinned.prove_absent(session.spec.target_leaf)
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: terminal generation still has a public "
                "Git registration; refusing reuse"
            )

    def _load_generation(
        self,
        session: _LifecycleSession,
        snapshot: _RecordSnapshot,
        *,
        expected_sha: str | None,
    ) -> _OwnerRecord:
        record = self._parse_record(
            snapshot.data,
            session.expected,
            session.ctx,
            expected_sha=expected_sha,
        )
        session.generation = _Generation(
            nonce=record.nonce,
            state=record.state,
            location=record.location,
            target_identity=record.target_identity,
            record_identity=snapshot.identity,
            bound_sha=record.bound_sha,
            advance_token=record.advance_token,
        )
        return record

    # -- identity and record protocol ---------------------------------------

    def _expected_fields(
        self,
        spec: WorktreeSpec,
        target: Path,
        pinned: _PinnedWorktreesRoot,
        authority: _PinnedGitAuthority,
        lease: _TargetLease,
    ) -> dict[str, object]:
        assert authority.common is not None
        return {
            "version": _RECORD_VERSION,
            "repo": str(self._repo),
            "repo_dev": authority.repo.identity[0],
            "repo_ino": authority.repo.identity[1],
            "common_dir": str(authority.common.path),
            "common_dir_dev": authority.common.identity[0],
            "common_dir_ino": authority.common.identity[1],
            "worktrees_root": str(self._root),
            "worktrees_root_dev": pinned.root_identity[0],
            "worktrees_root_ino": pinned.root_identity[1],
            "owners_dir": str(self._root / _OWNERS_DIR),
            "owners_dir_dev": pinned.owners_identity[0],
            "owners_dir_ino": pinned.owners_identity[1],
            "lease": str(self._root / _OWNERS_DIR / lease.name),
            "lease_dev": lease.identity[0],
            "lease_ino": lease.identity[1],
            "target": str(target),
            "goal": spec.goal_name,
            "run_id": spec.run_id,
            "purpose": spec.purpose.value,
        }

    @staticmethod
    def _record_bytes(doc: dict[str, object]) -> bytes:
        payload = (
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_RECORD_DOC_BYTES:
            raise StorageError("managed worktree owner record document is oversized")
        return payload

    @staticmethod
    def _record_frame(data: bytes) -> bytes:
        """Legacy v4 append frame, retained only for on-disk migration."""
        if len(data) > _MAX_RECORD_DOC_BYTES:
            raise StorageError("managed worktree owner record document is oversized")
        digest = hashlib.sha256(data).hexdigest().encode("ascii")
        return (
            _FRAME_PREFIX
            + f"{len(data):08x}".encode("ascii")
            + b":"
            + digest
            + b":"
            + data
            + b"\n"
        )

    @staticmethod
    def _checkpoint_frame(data: bytes, sequence: int) -> bytes:
        if len(data) > _MAX_RECORD_DOC_BYTES:
            raise StorageError("managed worktree owner record document is oversized")
        if not 0 <= sequence <= _CHECKPOINT_SEQUENCE_MASK:
            raise StorageError("managed worktree checkpoint sequence is invalid")
        frame = _CHECKPOINT_HEADER.pack(
            _CHECKPOINT_MAGIC,
            sequence,
            len(data),
            hashlib.sha256(data).digest(),
        ) + data
        if len(frame) > _CHECKPOINT_SLOT_BYTES:
            raise StorageError("managed worktree checkpoint exceeds its fixed slot")
        return frame

    @staticmethod
    def _sequence_after(candidate: int, prior: int) -> bool:
        distance = (candidate - prior) & _CHECKPOINT_SEQUENCE_MASK
        return 0 < distance < (1 << 63)

    @classmethod
    def _latest_checkpoint(
        cls, data: bytes, ctx: str
    ) -> _Checkpoint | None:
        """Choose the newest valid committed fixed-slot checkpoint.

        The one-byte commit marker is written only after the remainder of a
        slot has been fsynced. An exact magic with a bad body is therefore not
        a recoverable partial write and fails closed.
        """
        valid: list[_Checkpoint] = []
        for slot_index in range(_CHECKPOINT_SLOT_COUNT):
            offset = slot_index * _CHECKPOINT_SLOT_BYTES
            if offset >= len(data):
                break
            if data[offset : offset + len(_CHECKPOINT_MAGIC)] != _CHECKPOINT_MAGIC:
                continue
            header_end = offset + _CHECKPOINT_HEADER.size
            if header_end > len(data):
                raise StorageError(f"{ctx}: truncated committed owner checkpoint")
            magic, sequence, length, digest = _CHECKPOINT_HEADER.unpack(
                data[offset:header_end]
            )
            if magic != _CHECKPOINT_MAGIC:
                raise StorageError(f"{ctx}: corrupt owner checkpoint magic")
            if length > _MAX_RECORD_DOC_BYTES:
                raise StorageError(f"{ctx}: owner checkpoint payload is oversized")
            frame_end = header_end + length
            slot_end = offset + _CHECKPOINT_SLOT_BYTES
            if frame_end > slot_end or frame_end > len(data):
                raise StorageError(f"{ctx}: truncated committed owner checkpoint")
            payload = data[header_end:frame_end]
            if hashlib.sha256(payload).digest() != digest:
                raise StorageError(f"{ctx}: corrupt owner checkpoint checksum")
            valid.append(
                _Checkpoint(
                    data=payload,
                    sequence=sequence,
                    slot_index=slot_index,
                    frame_size=frame_end - offset,
                )
            )
        if not valid:
            return None
        latest = valid[0]
        for checkpoint in valid[1:]:
            if checkpoint.sequence == latest.sequence:
                raise StorageError(
                    f"{ctx}: duplicate owner checkpoint sequence "
                    f"{checkpoint.sequence}"
                )
            if cls._sequence_after(checkpoint.sequence, latest.sequence):
                latest = checkpoint
        for checkpoint in valid:
            if checkpoint is latest:
                continue
            if not cls._sequence_after(latest.sequence, checkpoint.sequence):
                raise StorageError(
                    f"{ctx}: ambiguous owner checkpoint sequence ordering"
                )
        return latest

    @staticmethod
    def _latest_legacy_document(
        data: bytes, ctx: str
    ) -> tuple[bytes, int]:
        """Read a current v4 append record for one-time slot migration."""
        marker = data.find(_FRAME_PREFIX)
        initial_end = len(data) if marker < 0 else marker
        initial = data[:initial_end].rstrip()
        if not initial:
            raise StorageError(f"{ctx}: owner record has no initial document")
        latest = initial
        valid_size = initial_end
        if marker < 0:
            return latest, len(data)
        pos = marker
        while pos < len(data):
            remaining = len(data) - pos
            if remaining < _FRAME_HEADER_BYTES:
                return latest, valid_size
            header = data[pos : pos + _FRAME_HEADER_BYTES]
            if header[:1] != _FRAME_PREFIX or header[9:10] != b":" or header[74:75] != b":":
                raise StorageError(f"{ctx}: corrupt owner record frame header")
            try:
                length = int(header[1:9], 16)
            except ValueError as exc:
                raise StorageError(f"{ctx}: corrupt owner record frame length") from exc
            if length > _MAX_RECORD_DOC_BYTES:
                raise StorageError(f"{ctx}: owner record frame is oversized")
            payload_end = pos + _FRAME_HEADER_BYTES + length
            frame_end = payload_end + 1
            if frame_end > len(data):
                return latest, valid_size
            if data[payload_end:frame_end] != b"\n":
                raise StorageError(f"{ctx}: corrupt owner record frame terminator")
            payload = data[pos + _FRAME_HEADER_BYTES : payload_end]
            digest = header[10:74]
            if hashlib.sha256(payload).hexdigest().encode("ascii") != digest:
                raise StorageError(f"{ctx}: corrupt owner record frame checksum")
            latest = payload
            valid_size = frame_end
            pos = frame_end
        return latest, valid_size

    @classmethod
    def _latest_record_document(cls, data: bytes, ctx: str) -> tuple[bytes, int]:
        """Return the newest complete full-state owner document.

        V5 uses a bounded fixed-slot journal. V4 append records remain
        readable and migrate through the same inode on their next transition.
        """
        checkpoint = cls._latest_checkpoint(data[:_MAX_RECORD_BYTES], ctx)
        if checkpoint is not None:
            valid_end = (
                checkpoint.slot_index * _CHECKPOINT_SLOT_BYTES
                + checkpoint.frame_size
            )
            return checkpoint.data, valid_end
        latest, valid_size = cls._latest_legacy_document(data, ctx)
        return latest, valid_size

    @staticmethod
    def _record_name(spec: WorktreeSpec) -> str:
        return spec.target_leaf + ".json"

    def _read_record_snapshot(
        self,
        pinned: _PinnedWorktreesRoot,
        spec: WorktreeSpec,
        ctx: str,
        *,
        lease: _TargetLease | None = None,
    ) -> _RecordSnapshot | None:
        assert pinned.owners_fd is not None
        pinned.prove()
        fd = -1
        try:
            fd = os.open(
                self._record_name(spec),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=pinned.owners_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(
                f"{ctx}: refusing the owner record: not a no-follow regular "
                f"file (symlink?): {exc}"
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(f"{ctx}: owner record is not a regular file")
            identity = (st.st_dev, st.st_ino)
            file_handle = os.fdopen(fd, "rb")
            fd = -1
            with file_handle:
                data = file_handle.read(_MAX_RECORD_BYTES + 1)
        except OSError as exc:
            raise StorageError(f"{ctx}: cannot read owner record: {exc}") from exc
        finally:
            if fd != -1:
                os.close(fd)
        if len(data) > _MAX_RECORD_BYTES:
            raise StorageError(f"{ctx}: owner record is oversized; refusing")
        checkpoint = self._latest_checkpoint(data, ctx)
        recovered_from_lease = False
        if checkpoint is None:
            recovered = (
                self._read_legacy_recovery_witness(lease, identity, ctx)
                if lease is not None
                else None
            )
            try:
                latest, valid_size = self._latest_legacy_document(data, ctx)
            except StorageError:
                if recovered is None:
                    raise
                latest = recovered
                valid_size = 0
                recovered_from_lease = True
            else:
                if recovered is not None and latest != recovered:
                    latest = recovered
                    valid_size = 0
                    recovered_from_lease = True
            sequence: int | None = None
            slot_index: int | None = None
        else:
            latest = checkpoint.data
            valid_size = (
                checkpoint.slot_index * _CHECKPOINT_SLOT_BYTES
                + checkpoint.frame_size
            )
            sequence = checkpoint.sequence
            slot_index = checkpoint.slot_index
        self._prove_record_identity(pinned, spec, identity, ctx)
        return _RecordSnapshot(
            data=latest,
            identity=identity,
            valid_size=valid_size,
            file_size=len(data),
            sequence=sequence,
            slot_index=slot_index,
            recovered_from_lease=recovered_from_lease,
        )

    @staticmethod
    def _parse_record(
        data: bytes,
        expected: dict[str, object],
        ctx: str,
        *,
        expected_sha: str | None,
    ) -> _OwnerRecord:
        def reject_duplicate_keys(
            pairs: list[tuple[str, object]],
        ) -> dict[str, object]:
            loaded: dict[str, object] = {}
            for key, value in pairs:
                if key in loaded:
                    raise ValueError(f"duplicate key {key!r}")
                loaded[key] = value
            return loaded

        try:
            loaded = json.loads(
                data.decode("utf-8"), object_pairs_hook=reject_duplicate_keys
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise StorageError(f"{ctx}: corrupt owner record: {exc}") from exc
        if not isinstance(loaded, dict):
            raise StorageError(f"{ctx}: owner record must be a JSON object")
        missing = _RECORD_KEYS - loaded.keys()
        extra = loaded.keys() - _RECORD_KEYS
        if missing:
            raise StorageError(f"{ctx}: owner record is missing {sorted(missing)}")
        if extra:
            raise StorageError(
                f"{ctx}: owner record carries unknown fields {sorted(extra)}"
            )
        if type(loaded["version"]) is not int:
            raise StorageError(f"{ctx}: owner record version must be an integer")
        if loaded["version"] not in (_LEGACY_RECORD_VERSION, _RECORD_VERSION):
            raise StorageError(
                f"{ctx}: owner record version {loaded['version']!r} is unsupported"
            )
        state = loaded["state"]
        if state not in _STATES:
            raise StorageError(f"{ctx}: owner record state {state!r} is unknown")
        nonce = loaded["nonce"]
        if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
            raise StorageError(f"{ctx}: owner record nonce is invalid")
        location = loaded["location"]
        if location not in _LOCATIONS:
            raise StorageError(
                f"{ctx}: owner record location {location!r} is unknown"
            )
        bound_sha = loaded["sha"]
        if not isinstance(bound_sha, str) or not _SHA40_RE.fullmatch(bound_sha):
            raise StorageError(f"{ctx}: owner record SHA is invalid")
        if expected_sha is not None and bound_sha != expected_sha:
            raise StorageError(
                f"{ctx}: owner record field 'sha' is {bound_sha!r}, "
                f"expected {expected_sha!r}"
            )
        advance_token = loaded["advance_token"]
        if state == "advancing":
            if (
                not isinstance(advance_token, str)
                or not _NONCE_RE.fullmatch(advance_token)
            ):
                raise StorageError(
                    f"{ctx}: advancing owner record token is invalid"
                )
        elif advance_token is not None:
            raise StorageError(
                f"{ctx}: non-advancing owner record carries an advance token"
            )
        for key in (
            "repo_dev",
            "repo_ino",
            "common_dir_dev",
            "common_dir_ino",
            "worktrees_root_dev",
            "worktrees_root_ino",
            "owners_dir_dev",
            "owners_dir_ino",
            "lease_dev",
            "lease_ino",
        ):
            value = loaded[key]
            if type(value) is not int or value < 0 or (key.endswith("_ino") and value == 0):
                raise StorageError(f"{ctx}: owner record field {key!r} is invalid")
        target_dev = loaded["target_dev"]
        target_ino = loaded["target_ino"]
        if state in ("preclaim", "abandoned"):
            if target_dev != 0 or target_ino != 0:
                raise StorageError(
                    f"{ctx}: unbound staging state must not assert a target inode"
                )
        elif (
            type(target_dev) is not int
            or type(target_ino) is not int
            or target_dev < 0
            or target_ino <= 0
        ):
            raise StorageError(f"{ctx}: owner record target identity is invalid")
        if (
            (state in ("preclaim", "abandoned") and location != "staging")
            or (state == "claimed" and location not in ("staging", "public"))
            or (state in ("ready", "advancing") and location != "public")
            or (state == "removing" and location not in ("public", "quarantine"))
            or (state == "removed" and location != "absent")
        ):
            raise StorageError(
                f"{ctx}: owner record state/location combination is invalid"
            )
        for key, want in expected.items():
            if key == "version":
                continue  # v4 is migrated in-place at its next transition.
            if loaded[key] != want:
                raise StorageError(
                    f"{ctx}: owner record field {key!r} is {loaded[key]!r}, "
                    f"expected {want!r}"
                )
        assert isinstance(state, str)
        assert isinstance(nonce, str)
        assert isinstance(location, str)
        return _OwnerRecord(
            state=state,
            nonce=nonce,
            location=location,
            target_identity=(target_dev, target_ino),
            bound_sha=bound_sha,
            advance_token=advance_token,
        )

    def _prove_record_identity(
        self,
        pinned: _PinnedWorktreesRoot,
        spec: WorktreeSpec,
        identity: Identity,
        ctx: str,
    ) -> None:
        assert pinned.owners_fd is not None
        pinned.prove()
        try:
            st = os.stat(
                self._record_name(spec),
                dir_fd=pinned.owners_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise StorageError(f"{ctx}: owner record vanished: {exc}") from exc
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != identity:
            raise StorageError(f"{ctx}: owner record inode changed; CAS refused")

    def _record_doc(
        self,
        session: _LifecycleSession,
        *,
        state: str,
        nonce: str,
        location: str,
        target: Identity,
        sha: str,
        advance_token: str | None,
    ) -> dict[str, object]:
        return {
            **session.expected,
            "state": state,
            "nonce": nonce,
            "location": location,
            "target_dev": target[0],
            "target_ino": target[1],
            "sha": sha,
            "advance_token": advance_token,
        }

    def _write_preclaim(
        self, session: _LifecycleSession, nonce: str
    ) -> _Generation:
        pinned, spec, ctx = session.pinned, session.spec, session.ctx
        assert pinned.owners_fd is not None
        doc = self._record_doc(
            session,
            state="preclaim",
            nonce=nonce,
            location="staging",
            target=(0, 0),
            sha=session.spec.sha,
            advance_token=None,
        )
        prior = session.generation
        if prior is not None:
            if prior.state not in ("removed", "abandoned"):
                raise StorageError(
                    f"{ctx}: cannot start a new generation from {prior.state!r}"
                )
            snapshot = self._require_generation_snapshot(
                session, state=prior.state
            )
            self._checkpoint_record_document(
                session, snapshot, self._record_bytes(doc)
            )
            persisted = self._read_record_snapshot(
                pinned, spec, ctx, lease=session.lease
            )
            if persisted is None:
                raise StorageError(f"{ctx}: checkpointed preclaim record vanished")
            record = self._parse_record(
                persisted.data,
                session.expected,
                ctx,
                expected_sha=spec.sha,
            )
            if (
                record.state != "preclaim"
                or record.nonce != nonce
                or record.location != "staging"
                or record.target_identity != (0, 0)
                or record.advance_token is not None
            ):
                raise StorageError(
                    f"{ctx}: checkpointed preclaim did not persist exactly"
                )
            return _Generation(
                nonce=nonce,
                state="preclaim",
                location="staging",
                target_identity=(0, 0),
                record_identity=persisted.identity,
                bound_sha=spec.sha,
                advance_token=None,
            )
        session.prove()
        record_name = self._record_name(spec)
        temp_name = f".{record_name}.{nonce}.preclaim"
        temp_exists = False
        published = False
        fd = -1
        try:
            fd = os.open(
                temp_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=pinned.owners_fd,
            )
            temp_exists = True
            st = os.fstat(fd)
            record_identity = (st.st_dev, st.st_ino)
            os.ftruncate(fd, _MAX_RECORD_BYTES)
            self._write_checkpoint_slot(
                fd,
                slot_index=0,
                sequence=0,
                data=self._record_bytes(doc),
            )
            # Hard-link publication is an atomic no-replace operation: a crash
            # can expose either no record or the complete fsynced record, never
            # a torn destination file.
            os.link(
                temp_name,
                record_name,
                src_dir_fd=pinned.owners_fd,
                dst_dir_fd=pinned.owners_fd,
                follow_symlinks=False,
            )
            published = True
            os.fsync(pinned.owners_fd)
        except FileExistsError as exc:
            raise StorageError(
                f"{ctx}: owner record appeared concurrently; claim refused"
            ) from exc
        except OSError as exc:
            raise StorageError(f"{ctx}: cannot claim owner record: {exc}") from exc
        finally:
            if fd != -1:
                os.close(fd)
            if temp_exists:
                try:
                    os.unlink(temp_name, dir_fd=pinned.owners_fd)
                    if published:
                        os.fsync(pinned.owners_fd)
                except OSError:
                    pass
        self._prove_record_identity(pinned, spec, record_identity, ctx)
        return _Generation(
            nonce=nonce,
            state="preclaim",
            location="staging",
            target_identity=(0, 0),
            record_identity=record_identity,
            bound_sha=session.spec.sha,
            advance_token=None,
        )

    def _require_generation_snapshot(
        self, session: _LifecycleSession, *, state: str
    ) -> _RecordSnapshot:
        session.prove()
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        snapshot = self._read_record_snapshot(
            session.pinned, session.spec, session.ctx, lease=session.lease
        )
        if snapshot is None:
            raise StorageError(f"{session.ctx}: owner record vanished; CAS refused")
        record = self._parse_record(
            snapshot.data,
            session.expected,
            session.ctx,
            expected_sha=generation.bound_sha,
        )
        if (
            snapshot.identity != generation.record_identity
            or record.nonce != generation.nonce
            or record.state != state
            or record.location != generation.location
            or record.target_identity != generation.target_identity
            or record.advance_token != generation.advance_token
        ):
            raise StorageError(
                f"{session.ctx}: owner record generation/state/inode changed; "
                "CAS refused"
            )
        return snapshot

    @staticmethod
    def _read_legacy_recovery_witness(
        lease: _TargetLease,
        record_identity: Identity,
        ctx: str,
    ) -> bytes | None:
        """Read a committed v4 recovery state from the proven lease inode."""
        try:
            st = os.fstat(lease.fd)
            if not stat.S_ISREG(st.st_mode) or (
                st.st_dev,
                st.st_ino,
            ) != lease.identity:
                raise StorageError(f"{ctx}: lifecycle lease identity changed")
            raw = os.pread(lease.fd, _LEGACY_RECOVERY_BYTES, 0)
        except OSError as exc:
            raise StorageError(
                f"{ctx}: cannot read legacy owner recovery witness: {exc}"
            ) from exc
        if len(raw) < _LEGACY_RECOVERY_HEADER.size:
            return None
        try:
            magic, record_dev, record_ino, length, digest = (
                _LEGACY_RECOVERY_HEADER.unpack(
                    raw[: _LEGACY_RECOVERY_HEADER.size]
                )
            )
        except struct.error:
            return None
        if magic != _LEGACY_RECOVERY_MAGIC:
            return None
        if (record_dev, record_ino) != record_identity:
            return None
        end = _LEGACY_RECOVERY_HEADER.size + length
        if length > _MAX_RECORD_DOC_BYTES or end > len(raw):
            return None
        payload = raw[_LEGACY_RECOVERY_HEADER.size : end]
        if hashlib.sha256(payload).digest() != digest:
            return None
        return payload

    def _ensure_legacy_recovery_witness(
        self,
        session: _LifecycleSession,
        snapshot: _RecordSnapshot,
    ) -> None:
        """Durably preserve v4 authority before overwriting its fixed inode.

        The already-bound, locked lease inode is the recovery cell. A complete
        witness is committed there before slot zero of the legacy record is
        touched. If recovery is already relying on that witness, it is reused
        byte-for-byte and never overwritten.
        """
        observed = self._read_legacy_recovery_witness(
            session.lease, snapshot.identity, session.ctx
        )
        if observed == snapshot.data:
            return
        if snapshot.recovered_from_lease:
            raise StorageError(
                f"{session.ctx}: legacy recovery witness changed; refusing"
            )
        try:
            frame = _LEGACY_RECOVERY_HEADER.pack(
                _LEGACY_RECOVERY_MAGIC,
                snapshot.identity[0],
                snapshot.identity[1],
                len(snapshot.data),
                hashlib.sha256(snapshot.data).digest(),
            ) + snapshot.data
        except (OverflowError, struct.error) as exc:
            raise StorageError(
                f"{session.ctx}: legacy owner identity cannot be checkpointed"
            ) from exc
        if len(frame) > _LEGACY_RECOVERY_BYTES:
            raise StorageError(
                f"{session.ctx}: legacy recovery witness exceeds its limit"
            )
        session.prove()
        self._prove_record_identity(
            session.pinned, session.spec, snapshot.identity, session.ctx
        )
        try:
            os.ftruncate(session.lease.fd, _LEGACY_RECOVERY_BYTES)
            os.fsync(session.lease.fd)
            self._pwrite_all(session.lease.fd, b"\0", 0)
            os.fsync(session.lease.fd)
            self._pwrite_all(session.lease.fd, frame[1:], 1)
            os.fsync(session.lease.fd)
            self._pwrite_all(session.lease.fd, frame[:1], 0)
            os.fsync(session.lease.fd)
        except OSError as exc:
            raise StorageError(
                f"{session.ctx}: cannot persist legacy recovery witness: {exc}"
            ) from exc
        session.prove()
        self._prove_record_identity(
            session.pinned, session.spec, snapshot.identity, session.ctx
        )
        if self._read_legacy_recovery_witness(
            session.lease, snapshot.identity, session.ctx
        ) != snapshot.data:
            raise StorageError(
                f"{session.ctx}: legacy recovery witness did not persist exactly"
            )

    def _checkpoint_record_document(
        self,
        session: _LifecycleSession,
        snapshot: _RecordSnapshot,
        data: bytes,
    ) -> None:
        """Checkpoint one transition through the already-proven record inode.

        The journal is a fixed set of slots inside ``_MAX_RECORD_BYTES``.
        The inactive slot body is fsynced while its one-byte commit marker is
        invalid; the marker is committed and fsynced last. The prior slot (or
        legacy v4 full-state frame during migration) therefore remains exact
        throughout every crash window.
        """
        assert session.pinned.owners_fd is not None
        if snapshot.slot_index is None:
            sequence = 0
            slot_index = 0
            frame_size = len(self._checkpoint_frame(data, sequence))
            self._ensure_legacy_recovery_witness(session, snapshot)
        else:
            assert snapshot.sequence is not None
            sequence = (snapshot.sequence + 1) & _CHECKPOINT_SEQUENCE_MASK
            slot_index = (snapshot.slot_index + 1) % _CHECKPOINT_SLOT_COUNT
            frame_size = len(self._checkpoint_frame(data, sequence))
        offset = slot_index * _CHECKPOINT_SLOT_BYTES
        if offset < 0 or offset + frame_size > _MAX_RECORD_BYTES:
            raise StorageError(
                f"{session.ctx}: owner checkpoint would cross the record limit"
            )
        fd = -1
        try:
            fd = os.open(
                self._record_name(session.spec),
                os.O_WRONLY | os.O_NOFOLLOW,
                dir_fd=session.pinned.owners_fd,
            )
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or (
                st.st_dev,
                st.st_ino,
            ) != snapshot.identity:
                raise StorageError(
                    f"{session.ctx}: owner record inode changed before checkpoint; "
                    "CAS refused"
                )
            self._prove_record_identity(
                session.pinned,
                session.spec,
                snapshot.identity,
                session.ctx,
            )
            if os.fstat(fd).st_size > _MAX_RECORD_BYTES:
                raise StorageError(
                    f"{session.ctx}: owner record already exceeds its limit"
                )
            self._write_checkpoint_slot(
                fd,
                slot_index=slot_index,
                sequence=sequence,
                data=data,
            )
            if os.fstat(fd).st_size < _MAX_RECORD_BYTES:
                os.ftruncate(fd, _MAX_RECORD_BYTES)
                os.fsync(fd)
        except OSError as exc:
            raise StorageError(
                f"{session.ctx}: cannot checkpoint owner record transition: {exc}"
            ) from exc
        finally:
            if fd != -1:
                os.close(fd)
        # A concurrent leaf replacement is preserved and rejected here. No
        # pathname was replaced or unlinked by the transition itself.
        self._prove_record_identity(
            session.pinned,
            session.spec,
            snapshot.identity,
            session.ctx,
        )

    @staticmethod
    def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
        view = memoryview(data)
        position = offset
        while view:
            written = os.pwrite(fd, view, position)
            if written <= 0:
                raise OSError("short owner-checkpoint write")
            view = view[written:]
            position += written

    @classmethod
    def _write_checkpoint_slot(
        cls,
        fd: int,
        *,
        slot_index: int,
        sequence: int,
        data: bytes,
    ) -> None:
        frame = cls._checkpoint_frame(data, sequence)
        if not 0 <= slot_index < _CHECKPOINT_SLOT_COUNT:
            raise StorageError("managed worktree checkpoint slot is invalid")
        offset = slot_index * _CHECKPOINT_SLOT_BYTES
        if offset + len(frame) > _MAX_RECORD_BYTES:
            raise StorageError(
                "managed worktree checkpoint would cross the record limit"
            )
        # Invalidate first. The body then becomes durable while recovery still
        # selects the prior committed checkpoint. Commit one byte last.
        cls._pwrite_all(fd, b"\0", offset)
        os.fsync(fd)
        cls._pwrite_all(fd, frame[1:], offset + 1)
        os.fsync(fd)
        cls._pwrite_all(fd, frame[:1], offset)
        os.fsync(fd)

    def _cas_transition(
        self,
        session: _LifecycleSession,
        *,
        expected_state: str,
        new_state: str,
        expected_location: str | None = None,
        new_location: str | None = None,
        new_target: Identity | None = None,
        new_sha: str | None = None,
        new_advance_token: str | None = None,
        replace_advance_token: bool = False,
    ) -> None:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        prior_snapshot = self._require_generation_snapshot(
            session, state=expected_state
        )
        if expected_location is not None and generation.location != expected_location:
            raise StorageError(
                f"{session.ctx}: generation location changed; CAS refused"
            )
        self._prove_record_identity(
            session.pinned,
            session.spec,
            generation.record_identity,
            session.ctx,
        )
        assert session.pinned.owners_fd is not None
        desired_sha = new_sha if new_sha is not None else generation.bound_sha
        desired_advance_token = (
            new_advance_token if replace_advance_token else generation.advance_token
        )
        doc = self._record_doc(
            session,
            state=new_state,
            nonce=generation.nonce,
            location=new_location or generation.location,
            target=new_target or generation.target_identity,
            sha=desired_sha,
            advance_token=desired_advance_token,
        )
        self._checkpoint_record_document(
            session,
            prior_snapshot,
            self._record_bytes(doc),
        )
        snapshot = self._read_record_snapshot(
            session.pinned, session.spec, session.ctx, lease=session.lease
        )
        if snapshot is None:
            raise StorageError(f"{session.ctx}: transitioned owner record vanished")
        record = self._parse_record(
            snapshot.data,
            session.expected,
            session.ctx,
            expected_sha=desired_sha,
        )
        if (
            record.state != new_state
            or record.nonce != generation.nonce
            or record.location != (new_location or generation.location)
            or record.target_identity != (new_target or generation.target_identity)
            or record.advance_token != desired_advance_token
        ):
            raise StorageError(f"{session.ctx}: owner transition did not persist exactly")
        generation.state = new_state
        if new_location is not None:
            generation.location = new_location
        if new_target is not None:
            generation.target_identity = new_target
        generation.bound_sha = desired_sha
        generation.advance_token = desired_advance_token
        generation.record_identity = snapshot.identity

    def _cas_unlink(self, session: _LifecycleSession) -> None:
        """Durably tombstone a removed generation without pathname unlink.

        POSIX has no unlink-if-inode primitive. Keeping the proven record inode
        as an ``removed/absent`` journal tombstone avoids the check/unlink gap;
        a future generation checkpoints a new preclaim in this same inode.
        """
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._cas_transition(
            session,
            expected_state="removing",
            new_state="removed",
            new_location="absent",
        )

    # -- Git lifecycle -------------------------------------------------------

    def _registration(
        self, session: _LifecycleSession, path: Path
    ) -> PorcelainWorktree | None:
        session.prove()
        raw = self._git.list_z(
            self._repo, session.authority.repo_fd, session.authority.common_fd
        )
        session.prove()
        return parse_worktree_list_z(raw).get(str(path))

    @staticmethod
    def _registration_is_exact(
        entry: PorcelainWorktree | None, sha: str
    ) -> bool:
        return (
            entry is not None
            and not entry.bare
            and entry.detached
            and entry.branch is None
            and entry.head == sha
        )

    @staticmethod
    def _registration_is_discardable(
        entry: PorcelainWorktree | None,
    ) -> bool:
        """Registration shape sufficient to discard a proven owned inode.

        The commit SHA may drift while an untrusted verify/review command is
        running. Destructive authority comes from the live lease, owner-record
        generation and target inode, not from that mutable SHA. Branch/bare
        registrations remain outside the closed disposable protocol.
        """
        return (
            entry is not None
            and not entry.bare
            and entry.detached
            and entry.branch is None
            and entry.head is not None
        )

    @staticmethod
    def _staging_leaf(session: _LifecycleSession) -> str:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        return f".{session.spec.target_leaf}.{generation.nonce}.claim"

    @staticmethod
    def _quarantine_leaf(session: _LifecycleSession) -> str:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        return f".{session.spec.target_leaf}.{generation.nonce}.remove"

    def _location_leaf(self, session: _LifecycleSession) -> str:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        if generation.location == "staging":
            return self._staging_leaf(session)
        if generation.location == "public":
            return session.spec.target_leaf
        if generation.location == "quarantine":
            return self._quarantine_leaf(session)
        raise StorageError(f"{session.ctx}: invalid in-memory generation location")

    def _location_path(self, session: _LifecycleSession) -> Path:
        return self._root / self._location_leaf(session)

    def _prove_owned_at(self, session: _LifecycleSession, leaf: str) -> None:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        session.prove()
        session.pinned.prove_target(leaf, generation.target_identity)

    def _prove_exact_at(
        self, session: _LifecycleSession, leaf: str, *, sha: str
    ) -> None:
        self._prove_owned_at(session, leaf)
        path = self._root / leaf
        entry = self._registration(session, path)
        if not self._registration_is_exact(entry, sha):
            raise StorageError(
                f"{session.ctx}: registration for {path} does not "
                f"exactly match detached/non-bare @ {sha}"
            )

    def _prove_bound_at(self, session: _LifecycleSession, leaf: str) -> None:
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._prove_exact_at(session, leaf, sha=generation.bound_sha)

    def _prove_discardable_at(
        self, session: _LifecycleSession, leaf: str
    ) -> PorcelainWorktree:
        self._prove_owned_at(session, leaf)
        path = self._root / leaf
        entry = self._registration(session, path)
        if not self._registration_is_discardable(entry):
            raise StorageError(
                f"{session.ctx}: registration for owned target {path} is not "
                "a detached/non-bare disposable worktree"
            )
        assert entry is not None
        return entry

    def _prove_clean(self, session: _LifecycleSession) -> None:
        """Check cleanliness while re-proving the same public target inode."""
        self._prove_owned_at(session, session.spec.target_leaf)
        session.prove()
        clean = self._git.is_clean(session.target)
        session.prove()
        self._prove_owned_at(session, session.spec.target_leaf)
        if not clean:
            raise StorageError(
                f"{session.ctx}: candidate commit is not clean after SHA advance"
            )

    def _prove_one_child(
        self, session: _LifecycleSession, *, old_sha: str, new_sha: str
    ) -> None:
        session.prove()
        parents = self._git.commit_parents(
            self._repo,
            session.authority.repo_fd,
            session.authority.common_fd,
            new_sha,
        )
        session.prove()
        if parents != (old_sha,):
            raise StorageError(
                f"{session.ctx}: advanced commit {new_sha} must have exactly "
                f"one parent {old_sha}; got {parents!r}"
            )

    def _prove_advanced_commit(
        self, session: _LifecycleSession, *, old_sha: str, new_sha: str
    ) -> None:
        if new_sha == old_sha:
            raise StorageError(f"{session.ctx}: SHA advance did not advance HEAD")
        self._prove_exact_at(
            session, session.spec.target_leaf, sha=new_sha
        )
        self._prove_clean(session)
        self._prove_one_child(session, old_sha=old_sha, new_sha=new_sha)
        self._prove_exact_at(
            session, session.spec.target_leaf, sha=new_sha
        )

    def _recover_sha_advance(self, session: _LifecycleSession) -> None:
        """Converge an interrupted begin→commit→complete transaction."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="advancing")
        if generation.location != "public" or generation.advance_token is None:
            raise StorageError(f"{session.ctx}: malformed advancing generation")
        self._prove_owned_at(session, session.spec.target_leaf)
        entry = self._registration(session, session.target)
        if self._registration_is_exact(entry, generation.bound_sha):
            # No commit became visible. Preserve any pre-commit writer bytes,
            # return to ready, and let the normal loop retry the commit.
            self._cas_transition(
                session,
                expected_state="advancing",
                new_state="ready",
                expected_location="public",
                new_advance_token=None,
                replace_advance_token=True,
            )
            self._prove_bound_at(session, session.spec.target_leaf)
            return
        if not self._registration_is_discardable(entry):
            raise StorageError(
                f"{session.ctx}: advancing registration is missing or no longer "
                "a detached owned worktree"
            )
        assert entry is not None and entry.head is not None
        recovered_sha = entry.head
        self._prove_advanced_commit(
            session,
            old_sha=generation.bound_sha,
            new_sha=recovered_sha,
        )
        self._cas_transition(
            session,
            expected_state="advancing",
            new_state="ready",
            expected_location="public",
            new_sha=recovered_sha,
            new_advance_token=None,
            replace_advance_token=True,
        )
        self._prove_bound_at(session, session.spec.target_leaf)

    def _finish_preclaim(self, session: _LifecycleSession) -> bool:
        """Bind a live-created staging inode or abandon an ambiguous one.

        ``True`` means the generation reached ``claimed``. ``False`` means an
        existing unbound nonce path was preserved, the generation was journaled
        as abandoned, and a fresh generation was fully created instead.
        """
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="preclaim")
        if generation.location != "staging" or generation.target_identity != (0, 0):
            raise StorageError(f"{session.ctx}: malformed in-memory preclaim")
        staging_leaf = self._staging_leaf(session)
        if session.pinned.stat_target(session.spec.target_leaf) is not None:
            raise StorageError(
                f"{session.ctx}: public target appeared during preclaim; refusing"
            )
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: public registration appeared during preclaim"
            )
        if self._registration(session, self._root / staging_leaf) is not None:
            raise StorageError(
                f"{session.ctx}: private staging registration appeared during preclaim"
            )
        staging_st = session.pinned.stat_target(staging_leaf)
        if staging_st is not None:
            # A crash may have happened after mkdir but before the record bound
            # its inode. No later process can distinguish that inode from a
            # replacement, even when it is empty. Preserve it under the old
            # nonce and continue with a new journaled generation.
            self._cas_transition(
                session,
                expected_state="preclaim",
                new_state="abandoned",
                expected_location="staging",
            )
            self._fresh_create(session)
            return False
        target_identity = session.pinned.create_empty_target(staging_leaf)
        self._require_generation_snapshot(session, state="preclaim")
        session.pinned.prove_target(staging_leaf, target_identity)
        self._cas_transition(
            session,
            expected_state="preclaim",
            new_state="claimed",
            expected_location="staging",
            new_target=target_identity,
        )
        return True

    def _abandon_preclaim_without_create(
        self, session: _LifecycleSession
    ) -> None:
        """Reconcile a preclaim without materializing or adopting anything."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="preclaim")
        if generation.location != "staging" or generation.target_identity != (0, 0):
            raise StorageError(f"{session.ctx}: malformed in-memory preclaim")
        staging_leaf = self._staging_leaf(session)
        if session.pinned.stat_target(session.spec.target_leaf) is not None:
            raise StorageError(
                f"{session.ctx}: public target appeared during preclaim; refusing"
            )
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: public registration appeared during preclaim"
            )
        if self._registration(session, self._root / staging_leaf) is not None:
            raise StorageError(
                f"{session.ctx}: private staging registration appeared during preclaim"
            )
        # Whether the nonce-private leaf is absent or ambiguous, acquisition
        # cannot prove an inode binding and therefore must never adopt it.
        self._cas_transition(
            session,
            expected_state="preclaim",
            new_state="abandoned",
            expected_location="staging",
        )
        self._prove_no_public_target(session)

    def _acquire_claimed_without_create(
        self, session: _LifecycleSession
    ) -> bool:
        """Finish only already-visible claim evidence; never run Git add."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="claimed")
        staging_leaf = self._staging_leaf(session)
        public_leaf = session.spec.target_leaf
        staging_st = session.pinned.stat_target(staging_leaf)
        public_st = session.pinned.stat_target(public_leaf)
        staging_owned = (
            staging_st is not None
            and stat.S_ISDIR(staging_st.st_mode)
            and (staging_st.st_dev, staging_st.st_ino) == generation.target_identity
        )
        public_owned = (
            public_st is not None
            and stat.S_ISDIR(public_st.st_mode)
            and (public_st.st_dev, public_st.st_ino) == generation.target_identity
        )
        if generation.location == "staging":
            if staging_owned and public_st is None:
                return False
            if public_owned and staging_st is None:
                # Rename completed before the location checkpoint. Recording
                # that already-observed fact is reconciliation, not creation.
                self._cas_transition(
                    session,
                    expected_state="claimed",
                    new_state="claimed",
                    expected_location="staging",
                    new_location="public",
                )
            else:
                raise StorageError(
                    f"{session.ctx}: claimed staging/public identity changed; "
                    "refusing recovery"
                )
        elif generation.location != "public" or not public_owned:
            raise StorageError(
                f"{session.ctx}: claimed public target identity changed; refusing"
            )

        self._prove_owned_at(session, public_leaf)
        entry = self._registration(session, session.target)
        if entry is None:
            if not session.pinned.target_is_empty(
                public_leaf, generation.target_identity
            ):
                raise StorageError(
                    f"{session.ctx}: claimed target without registration is not empty"
                )
            return False
        if not self._registration_is_exact(entry, generation.bound_sha):
            raise StorageError(
                f"{session.ctx}: claimed generation has a foreign/mismatched "
                "Git registration; refusing recovery"
            )
        self._prove_bound_at(session, public_leaf)
        self._cas_transition(
            session,
            expected_state="claimed",
            new_state="ready",
            expected_location="public",
        )
        self._prove_bound_at(session, public_leaf)
        return True

    def _publish_claim(self, session: _LifecycleSession) -> None:
        """Publish the durably claimed private inode at the public target."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="claimed")
        staging_leaf = self._staging_leaf(session)
        public_leaf = session.spec.target_leaf
        staging_st = session.pinned.stat_target(staging_leaf)
        public_st = session.pinned.stat_target(public_leaf)
        staging_owned = (
            staging_st is not None
            and stat.S_ISDIR(staging_st.st_mode)
            and (staging_st.st_dev, staging_st.st_ino) == generation.target_identity
        )
        public_owned = (
            public_st is not None
            and stat.S_ISDIR(public_st.st_mode)
            and (public_st.st_dev, public_st.st_ino) == generation.target_identity
        )
        if generation.location == "staging":
            if staging_owned and public_st is None:
                if self._registration(session, self._root / staging_leaf) is not None:
                    raise StorageError(
                        f"{session.ctx}: staging target unexpectedly registered"
                    )
                if self._registration(session, session.target) is not None:
                    raise StorageError(
                        f"{session.ctx}: public target unexpectedly registered"
                    )
                self._require_generation_snapshot(session, state="claimed")
                self._prove_owned_at(session, staging_leaf)
                session.pinned.prove_absent(public_leaf)
                try:
                    rename_noreplace(
                        staging_leaf,
                        public_leaf,
                        src_dir_fd=session.pinned.root_fd,
                        dst_dir_fd=session.pinned.root_fd,
                    )
                    os.fsync(session.pinned.root_fd)
                except OSError as exc:
                    raise StorageError(
                        f"{session.ctx}: cannot publish claimed target: {exc}"
                    ) from exc
                session.pinned.prove_absent(staging_leaf)
                self._prove_owned_at(session, public_leaf)
            elif public_owned and staging_st is None:
                pass  # crash after rename, before location CAS
            else:
                raise StorageError(
                    f"{session.ctx}: claimed staging/public identity changed; "
                    "refusing recovery"
                )
            self._cas_transition(
                session,
                expected_state="claimed",
                new_state="claimed",
                expected_location="staging",
                new_location="public",
            )
        elif generation.location != "public" or not public_owned:
            raise StorageError(
                f"{session.ctx}: claimed public target identity changed; refusing"
            )

    def _fresh_create(self, session: _LifecycleSession) -> None:
        session.prove()
        if session.pinned.stat_target(session.spec.target_leaf) is not None:
            raise StorageError(
                f"{session.ctx}: {session.target} exists without this generation; "
                "refusing to adopt or touch it"
            )
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: {session.target} is already registered with Git "
                "without this generation; refusing to adopt it"
            )
        nonce = secrets.token_hex(16)
        # The durable preclaim reserves the unpredictable staging name before
        # mkdir, so a claim-write failure leaves no filesystem target at all.
        session.generation = self._write_preclaim(session, nonce)
        if not self._finish_preclaim(session):
            raise StorageError(
                f"{session.ctx}: fresh preclaim unexpectedly required abandonment"
            )
        generation = session.generation
        assert generation is not None
        target_identity = generation.target_identity
        self._publish_claim(session)
        self._require_generation_snapshot(session, state="claimed")
        self._prove_owned_at(session, session.spec.target_leaf)
        if not session.pinned.target_is_empty(
            session.spec.target_leaf, target_identity
        ):
            raise StorageError(
                f"{session.ctx}: pre-created owned target is not empty; "
                "refusing Git population"
            )
        if self._registration(session, session.target) is not None:
            raise StorageError(
                f"{session.ctx}: a registration appeared before Git population"
            )
        target_fd = session.pinned.open_target_fd(
            session.spec.target_leaf, generation.target_identity
        )
        try:
            session.prove()
            self._git.add_detached(
                self._repo,
                session.authority.repo_fd,
                session.authority.common_fd,
                target_fd,
                generation.bound_sha,
            )
            session.prove()
            st = os.fstat(target_fd)
            if (st.st_dev, st.st_ino) != generation.target_identity:
                raise StorageError(
                    f"{session.ctx}: fd-bound add target identity changed"
                )
            self._prove_bound_at(session, session.spec.target_leaf)
        finally:
            os.close(target_fd)
        self._cas_transition(
            session,
            expected_state="claimed",
            new_state="ready",
            expected_location="public",
        )

    def _finish_partial_create(self, session: _LifecycleSession) -> None:
        """Recover only the same pre-created target inode and generation."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="claimed")
        self._publish_claim(session)
        self._prove_owned_at(session, session.spec.target_leaf)
        entry = self._registration(session, session.target)
        if entry is None:
            if not session.pinned.target_is_empty(
                session.spec.target_leaf, generation.target_identity
            ):
                raise StorageError(
                    f"{session.ctx}: claimed target is not the empty owned "
                    "pre-Git directory; refusing recovery"
                )
            self._require_generation_snapshot(session, state="claimed")
            target_fd = session.pinned.open_target_fd(
                session.spec.target_leaf, generation.target_identity
            )
            try:
                session.prove()
                self._git.add_detached(
                    self._repo,
                    session.authority.repo_fd,
                    session.authority.common_fd,
                    target_fd,
                    generation.bound_sha,
                )
                session.prove()
                st = os.fstat(target_fd)
                if (st.st_dev, st.st_ino) != generation.target_identity:
                    raise StorageError(
                        f"{session.ctx}: fd-bound add target identity changed"
                    )
                self._prove_bound_at(session, session.spec.target_leaf)
            finally:
                os.close(target_fd)
        elif not self._registration_is_exact(entry, generation.bound_sha):
            raise StorageError(
                f"{session.ctx}: claimed generation has a foreign/mismatched "
                "Git registration; refusing recovery"
            )
        else:
            self._prove_bound_at(session, session.spec.target_leaf)
        self._cas_transition(
            session,
            expected_state="claimed",
            new_state="ready",
            expected_location="public",
        )

    def _quarantine(self, session: _LifecycleSession) -> None:
        """Move the owned registration away from the public pathname first."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        self._require_generation_snapshot(session, state="removing")
        if generation.location == "quarantine":
            return
        if generation.location != "public":
            raise StorageError(
                f"{session.ctx}: removing generation has invalid location"
            )
        public_leaf = session.spec.target_leaf
        quarantine_leaf = self._quarantine_leaf(session)
        public_st = session.pinned.stat_target(public_leaf)
        quarantine_st = session.pinned.stat_target(quarantine_leaf)
        quarantine_entry = self._registration(
            session, self._root / quarantine_leaf
        )
        if (
            public_st is not None
            and stat.S_ISDIR(public_st.st_mode)
            and (public_st.st_dev, public_st.st_ino) == generation.target_identity
            and self._registration_is_discardable(
                self._registration(session, session.target)
            )
            and quarantine_st is None
            and quarantine_entry is None
        ):
            self._require_generation_snapshot(session, state="removing")
            self._prove_discardable_at(session, public_leaf)
            session.pinned.prove_absent(quarantine_leaf)
            source_fd = session.pinned.open_target_fd(
                public_leaf, generation.target_identity
            )
            try:
                session.prove()
                self._git.move_exact(
                    self._repo,
                    session.authority.repo_fd,
                    session.authority.common_fd,
                    source_fd,
                    session.pinned.root_fd,
                    quarantine_leaf,
                )
                session.prove()
                st = os.fstat(source_fd)
                if (st.st_dev, st.st_ino) != generation.target_identity:
                    raise StorageError(
                        f"{session.ctx}: fd-bound move source identity changed"
                    )
            finally:
                os.close(source_fd)
        elif not (
            public_st is None
            and quarantine_st is not None
            and stat.S_ISDIR(quarantine_st.st_mode)
            and (quarantine_st.st_dev, quarantine_st.st_ino)
            == generation.target_identity
            and self._registration_is_discardable(quarantine_entry)
        ):
            raise StorageError(
                f"{session.ctx}: cannot prove exact public-to-quarantine move"
            )
        session.pinned.prove_absent(public_leaf)
        self._prove_discardable_at(session, quarantine_leaf)
        self._cas_transition(
            session,
            expected_state="removing",
            new_state="removing",
            expected_location="public",
            new_location="quarantine",
        )

    def _finish_removal(
        self, session: _LifecycleSession, *, transition_first: bool
    ) -> None:
        """Remove only an exact target inode + exact Git registration."""
        generation = session.generation
        if generation is None:
            raise StorageError(f"{session.ctx}: no active generation")
        if transition_first:
            self._require_generation_snapshot(session, state="ready")
            if generation.location != "public":
                raise StorageError(f"{session.ctx}: ready generation is not public")
            self._prove_discardable_at(session, session.spec.target_leaf)
            self._cas_transition(
                session,
                expected_state="ready",
                new_state="removing",
                expected_location="public",
            )
        else:
            self._require_generation_snapshot(session, state="removing")
        if generation.location == "public":
            self._quarantine(session)

        quarantine_leaf = self._quarantine_leaf(session)
        quarantine_path = self._root / quarantine_leaf
        target_st = session.pinned.stat_target(quarantine_leaf)
        entry = self._registration(session, quarantine_path)
        if target_st is None and entry is None and generation.location == "quarantine":
            pass  # crash after exact quarantine removal, before record unlink
        elif (
            generation.location == "quarantine"
            and target_st is not None
            and stat.S_ISDIR(target_st.st_mode)
            and (target_st.st_dev, target_st.st_ino) == generation.target_identity
            and self._registration_is_discardable(entry)
        ):
            self._require_generation_snapshot(session, state="removing")
            self._prove_discardable_at(session, quarantine_leaf)
            target_fd = session.pinned.open_target_fd(
                quarantine_leaf, generation.target_identity
            )
            try:
                session.prove()
                self._git.remove_forced_fd(
                    self._repo,
                    session.authority.repo_fd,
                    session.authority.common_fd,
                    target_fd,
                )
                session.prove()
                st = os.fstat(target_fd)
                if (st.st_dev, st.st_ino) != generation.target_identity:
                    raise StorageError(
                        f"{session.ctx}: fd-bound removal target identity changed"
                    )
                if st.st_nlink != 0:
                    raise StorageError(
                        f"{session.ctx}: fd-bound removal target remains linked"
                    )
            finally:
                os.close(target_fd)
        else:
            raise StorageError(
                f"{session.ctx}: quarantine identity or exact Git registration "
                "changed; refusing destructive removal"
            )

        if session.pinned.stat_target(quarantine_leaf) is not None:
            raise StorageError(f"{session.ctx}: quarantine still exists after removal")
        if self._registration(session, quarantine_path) is not None:
            raise StorageError(
                f"{session.ctx}: quarantine is still registered after removal"
            )
        try:
            os.fsync(session.pinned.root_fd)
        except OSError as exc:
            raise StorageError(
                f"{session.ctx}: cannot fsync worktrees root after removal: {exc}"
            ) from exc
        self._cas_unlink(session)
