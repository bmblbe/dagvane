"""Local process execution adapter: git operations and deterministic
verification commands (Autonomous Developer MVP).

With ``adapters/agents/subprocess_runner.py`` this is the only module allowed
to import ``subprocess`` (import contract allowlist). Verification commands
are *deterministic evidence*: Dagvane records command, exit code, duration,
and bounded output — no model interprets whether a command passed.

Lifecycle contract (remediation): every command starts in its own session on
POSIX, so a timeout terminates and reaps the whole spawned process group, not
just the direct shell child. Non-POSIX platforms fall back to killing the
direct child only — a documented limitation, not a silent one. A process that
double-forks out of its session escapes this guarantee on any platform.

When a ``SecretScrubber`` is supplied, the complete captured output is
scrubbed *before* the bounded tail is taken, so a registered credential can
never straddle the truncation boundary into durable evidence.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dagvane.domain.models import SpecError
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.runtime import Monotonic

_OUTPUT_TAIL_CHARS = 8000
_TERM_GRACE_SECONDS = 2.0


def kill_process_group(pid: int, sig: int) -> None:
    """Signal ``pid``'s whole process group (POSIX) or the process alone
    elsewhere. Missing processes are ignored — the goal is 'not running',
    not 'we delivered a signal'."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            pass
    else:  # pragma: no cover — non-POSIX fallback (direct child only)
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def terminate_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """TERM the child's group, wait briefly, KILL, and reap the child."""
    kill_process_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        kill_process_group(proc.pid, signal.SIGKILL)
        proc.wait()


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    exit_code: int | None  # None = timed out
    duration_ms: int
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_shell(
    command: str,
    *,
    cwd: Path,
    monotonic: Monotonic,
    timeout_seconds: int = 900,
    scrubber: SecretScrubber | None = None,
) -> CommandResult:
    """Run one configured shell command and record bounded evidence."""
    started = monotonic.now_ms()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout_seconds)
        exit_code: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        terminate_and_reap(proc)
        exit_code = None
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b""
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    output = (stdout or b"").decode("utf-8", errors="replace")
    if scrubber is not None:
        # Scrub the complete output first; only then truncate. A rendering
        # can therefore never straddle the tail boundary into evidence.
        output = scrubber.scrub(output)
    duration_ms = max(0, monotonic.now_ms() - started)
    return CommandResult(
        command=command,
        exit_code=exit_code,
        duration_ms=duration_ms,
        output_tail=output[-_OUTPUT_TAIL_CHARS:],
    )


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, check=False
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpecError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


class GitOps:
    """The few git operations the goal workflow needs. No push, no merge."""

    @staticmethod
    def head_sha(cwd: Path) -> str:
        return _git(["rev-parse", "HEAD"], cwd)

    @staticmethod
    def is_repo(cwd: Path) -> bool:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=cwd, capture_output=True, check=False
        )
        return completed.returncode == 0

    @staticmethod
    def is_clean(cwd: Path) -> bool:
        return _git(["status", "--porcelain"], cwd) == ""

    @staticmethod
    def worktree_add(repo: Path, path: Path, sha: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "--detach", str(path), sha], repo)

    @staticmethod
    def worktree_remove(repo: Path, path: Path) -> None:
        _git(["worktree", "remove", "--force", str(path)], repo)

    @staticmethod
    def fresh_worktree(repo: Path, path: Path, sha: str) -> None:
        """A disposable worktree pinned at exactly ``sha``.

        Idempotent under crash-retry: any stale directory *and* any stale
        worktree registration from an interrupted earlier attempt are cleared
        first — ``git worktree add`` refuses paths that are merely still
        registered, so removal alone is not enough.
        """
        if path.exists():
            try:
                GitOps.worktree_remove(repo, path)
            except SpecError:
                pass
        shutil.rmtree(path, ignore_errors=True)
        _git(["worktree", "prune"], repo)
        GitOps.worktree_add(repo, path, sha)

    @staticmethod
    def commit_all(worktree: Path, message: str) -> str | None:
        """Stage and commit everything; return the new SHA, or None if clean."""
        _git(["add", "-A"], worktree)
        if _git(["status", "--porcelain"], worktree) == "":
            return None
        _git(["commit", "-m", message, "--no-verify"], worktree)
        return GitOps.head_sha(worktree)

    @staticmethod
    def tracked_dirty(worktree: Path) -> list[str]:
        """Porcelain lines for *tracked* modifications (untracked ``??``
        entries excluded): the fail-closed signal that a check or verify
        command mutated candidate bytes."""
        output = _git(["status", "--porcelain"], worktree)
        return [
            line
            for line in output.splitlines()
            if line.strip() and not line.startswith("??")
        ]

    @staticmethod
    def changed_files(worktree: Path, base_sha: str) -> list[str]:
        output = _git(["diff", "--name-only", f"{base_sha}..HEAD"], worktree)
        return [line for line in output.splitlines() if line.strip()]

    @staticmethod
    def diff_stat(worktree: Path, base_sha: str) -> str:
        return _git(["diff", "--stat", f"{base_sha}..HEAD"], worktree)

    @staticmethod
    def diff_text(worktree: Path, base_sha: str, *, max_chars: int = 60000) -> str:
        output = _git(["diff", f"{base_sha}..HEAD"], worktree)
        if len(output) > max_chars:
            return output[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
        return output

    @staticmethod
    def diff_sha256(worktree: Path, base_sha: str) -> str:
        """SHA-256 over the complete, untruncated diff bytes — the content
        hash that binds a review to the exact candidate."""
        completed = subprocess.run(
            ["git", "diff", f"{base_sha}..HEAD"],
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SpecError(f"git diff for hashing failed in {worktree}: {stderr}")
        return hashlib.sha256(completed.stdout).hexdigest()
