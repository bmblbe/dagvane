"""Per-goal exclusive execution lease (Autonomous Developer MVP remediation).

The one-writer invariant is enforced with a POSIX ``flock`` held on an open
file description for the entire ``goal run``/``goal resume`` loop. ``flock``
semantics are exactly what the invariant needs:

- a second acquisition attempt — from another process *or* another open of
  the same file in this process — fails immediately instead of overlapping;
- the kernel releases the lock the moment the holding process dies, so a
  crashed writer never leaves a stale lease to detect or break;
- the lock file itself is never unlinked: removing and recreating it could
  hand two waiters locks on two different inodes of the same path.

Honest limitations: this is POSIX-only (non-POSIX platforms are refused with
a configuration error, not a traceback) and, like all ``flock`` uses, it is
not reliable on NFS mounts. The lease excludes concurrent *Dagvane* writers;
it is not a security boundary against arbitrary processes.
"""

from __future__ import annotations

import os
from pathlib import Path

from dagvane.domain.models import SpecError

try:  # POSIX only — see the module docstring.
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX platform
    fcntl = None  # type: ignore[assignment]


class GoalLease:
    """Exclusive per-goal lease around the autonomous run loop."""

    __slots__ = ("_path", "_fd")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, owner: str) -> None:
        """Take the exclusive lease or raise ``SpecError`` without waiting."""
        if fcntl is None:  # pragma: no cover — non-POSIX platform
            raise SpecError(
                "the one-writer goal lease requires POSIX flock; this "
                "platform cannot run `goal run`/`goal resume` safely"
            )
        if self._fd is not None:
            raise SpecError(f"lease {self._path} is already held by this runner")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = self._read_holder(fd)
            os.close(fd)
            raise SpecError(
                "another process is already executing this goal "
                f"(lease {self._path}{f', holder: {holder}' if holder else ''}); "
                "one goal admits exactly one writer"
            ) from None
        # Diagnostics only — the flock above is the actual exclusion.
        os.ftruncate(fd, 0)
        os.write(fd, f"{owner} pid={os.getpid()}\n".encode())
        self._fd = fd

    @staticmethod
    def _read_holder(fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, 256).decode("utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover — diagnostics are best-effort
            return ""

    def release(self) -> None:
        """Release the lease (idempotent). The lock file is kept on disk."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
