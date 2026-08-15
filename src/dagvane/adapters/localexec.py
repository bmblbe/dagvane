"""Local process execution adapter: git operations and deterministic
verification commands (Autonomous Developer MVP).

With ``adapters/agents/subprocess_runner.py`` this is the only module allowed
to import ``subprocess`` (import contract allowlist). Verification commands
are *deterministic evidence*: Dagvane records command, exit code, duration,
and bounded output — no model interprets whether a command passed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from dagvane.domain.models import SpecError
from dagvane.ports.runtime import Monotonic

_OUTPUT_TAIL_CHARS = 8000


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
) -> CommandResult:
    """Run one configured shell command and record bounded evidence."""
    started = monotonic.now_ms()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_code: int | None = completed.returncode
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        raw = (exc.stdout or b"") + (exc.stderr or b"")
        output = raw.decode("utf-8", errors="replace")
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
    def commit_all(worktree: Path, message: str) -> str | None:
        """Stage and commit everything; return the new SHA, or None if clean."""
        _git(["add", "-A"], worktree)
        if _git(["status", "--porcelain"], worktree) == "":
            return None
        _git(["commit", "-m", message, "--no-verify"], worktree)
        return GitOps.head_sha(worktree)

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
