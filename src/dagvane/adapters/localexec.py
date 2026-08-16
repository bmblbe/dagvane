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
import signal
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from dagvane.domain.models import SpecError, StorageError
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.runtime import Monotonic

_OUTPUT_TAIL_CHARS = 8000
_TERM_GRACE_SECONDS = 2.0
_GIT_FD_BINDINGS: ContextVar[tuple[tuple[str, str, int], ...]] = ContextVar(
    "dagvane_git_fd_bindings", default=()
)
_GIT_FD_AUTHORITY: ContextVar[tuple[int, int | None] | None] = ContextVar(
    "dagvane_git_fd_authority", default=None
)


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
    cwd_identity: tuple[int, int],
    monotonic: Monotonic,
    timeout_seconds: int = 900,
    scrubber: SecretScrubber | None = None,
) -> CommandResult:
    """Run one configured shell command and record bounded evidence.

    ``cwd_identity`` is supplied by the managed-worktree authority that
    proved the directory immediately before this call.  The pathname is only
    used to locate that inode with ``O_NOFOLLOW``; the child receives a
    kernel-bound ``/proc/self/fd`` cwd and never a replaceable pathname.
    """
    started = monotonic.now_ms()
    if not Path("/proc/self/fd").is_dir():
        raise StorageError("shell execution requires Linux /proc/self/fd")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        cwd_fd = os.open(cwd, flags)
    except OSError as exc:
        raise StorageError(f"cannot pin shell cwd {cwd}: {exc}") from exc
    try:
        cwd_st = os.fstat(cwd_fd)
        if not stat.S_ISDIR(cwd_st.st_mode) or (
            cwd_st.st_dev,
            cwd_st.st_ino,
        ) != cwd_identity:
            raise StorageError(
                f"shell cwd {cwd} no longer names its proven directory identity"
            )
        try:
            path_st = os.lstat(cwd)
        except OSError as exc:
            raise StorageError(f"shell cwd {cwd} vanished: {exc}") from exc
        if not stat.S_ISDIR(path_st.st_mode) or (
            path_st.st_dev,
            path_st.st_ino,
        ) != cwd_identity:
            raise StorageError(
                f"shell cwd {cwd} was replaced before execution"
            )
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=f"/proc/self/fd/{cwd_fd}",
            pass_fds=(cwd_fd,),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        os.close(cwd_fd)
        raise StorageError(f"cannot spawn shell in pinned cwd {cwd}: {exc}") from exc
    except BaseException:
        os.close(cwd_fd)
        raise
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
        os.close(cwd_fd)
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
    """Run Git, replacing scoped diagnostic operands with pinned fd paths.

    ``GitOps`` installs bindings only around add/move. Every binding must be
    consumed by the argv; otherwise this helper refuses before Git instead of
    silently falling back to the display pathname.
    """
    bindings = _GIT_FD_BINDINGS.get()
    replacements = {display: actual for display, actual, _fd in bindings}
    if len(replacements) != len(bindings):
        raise SpecError("fd-bound git operands must have unique display paths")
    missing = replacements.keys() - set(args)
    if missing:
        raise SpecError(
            f"fd-bound git operands were not consumed: {sorted(missing)}"
        )
    actual_args = [replacements.get(arg, arg) for arg in args]
    prefix, actual_cwd, authority_fds = GitOps._authority_invocation(cwd)
    pass_fds = tuple(
        dict.fromkeys(
            (*authority_fds, *(fd for _display, _actual, fd in bindings))
        )
    )
    completed = subprocess.run(
        ["git", *prefix, *actual_args],
        cwd=actual_cwd,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpecError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _git_bytes(args: list[str], cwd: Path) -> bytes:
    """``_git``, but returning the raw, unstripped, undecoded stdout bytes —
    for machine formats (``--porcelain -z``) where trimming would corrupt."""
    prefix, actual_cwd, pass_fds = GitOps._authority_invocation(cwd)
    completed = subprocess.run(
        ["git", *prefix, *args],
        cwd=actual_cwd,
        capture_output=True,
        check=False,
        pass_fds=pass_fds,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SpecError(f"git {' '.join(args)} failed in {cwd}: {stderr}")
    return completed.stdout


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
    def common_dir(repo: Path) -> Path:
        """The absolute, canonical Git common directory of ``repo`` — the one
        registry every linked worktree of the repository shares."""
        out = _git(["rev-parse", "--git-common-dir"], repo)
        authority = _GIT_FD_AUTHORITY.get()
        base = (
            Path(GitOps._inherited_directory_path(authority[0]))
            if authority is not None
            else repo
        )
        try:
            return (base / out).resolve(strict=True)
        except OSError as exc:
            raise SpecError(
                f"cannot resolve git common dir {out!r} of {repo}: {exc}"
            ) from exc

    @staticmethod
    def worktree_list_porcelain_z(repo: Path) -> bytes:
        """Raw ``git worktree list --porcelain -z`` bytes, unparsed: the
        strict parser lives in ``adapters/worktrees.py``."""
        return _git_bytes(["worktree", "list", "--porcelain", "-z"], repo)

    @staticmethod
    def worktree_add_detached_exact(
        repo: Path, target_fd: int, sha: str
    ) -> None:
        """Populate the directory pinned by ``target_fd`` as a worktree.

        The inherited ``/proc/self/fd`` operand remains bound to the directory
        inode that the managed-worktree adapter already proved.  A concurrent
        pathname replacement therefore cannot redirect Git into the
        replacement.  Linux ``/proc`` is required deliberately; there is no
        ordinary-path fallback.
        """
        target = GitOps._inherited_directory_path(target_fd)
        display_target = str(Path(target).resolve(strict=True))
        GitOps._run_with_fds(
            repo,
            ["worktree", "add", "--detach", "--", display_target, sha],
            ((display_target, target, target_fd),),
            operation="worktree add --detach via pinned fd",
        )

    @staticmethod
    def worktree_move_exact(
        repo: Path,
        source_fd: int,
        target_parent_fd: int,
        target_leaf: str,
    ) -> None:
        """Move a pinned worktree beneath a pinned parent directory.

        Both path authorities are inherited descriptors. ``target_leaf`` is a
        single, internally derived name; accepting a caller path here would
        reintroduce the replacement race this boundary exists to close.
        """
        if (
            not target_leaf
            or target_leaf in (".", "..")
            or os.path.basename(target_leaf) != target_leaf
            or os.sep in target_leaf
            or (os.altsep is not None and os.altsep in target_leaf)
        ):
            raise SpecError("git worktree move target must be one leaf name")
        source = GitOps._inherited_directory_path(source_fd)
        target_parent = GitOps._inherited_directory_path(target_parent_fd)
        display_source = str(Path(source).resolve(strict=True))
        display_parent = Path(target_parent).resolve(strict=True)
        display_target = str(display_parent / target_leaf)
        target = f"{target_parent}/{target_leaf}"
        GitOps._run_with_fds(
            repo,
            ["worktree", "move", "--", display_source, display_target],
            (
                (display_source, source, source_fd),
                (display_target, target, target_parent_fd),
            ),
            operation="worktree move via pinned fds",
        )

    @staticmethod
    def _inherited_directory_path(fd: int) -> str:
        if type(fd) is not int or fd < 0:
            raise SpecError("fd-bound git worktree operation requires a valid fd")
        proc_fd = Path("/proc/self/fd")
        if not proc_fd.is_dir():
            raise SpecError(
                "fd-bound git worktree operation requires Linux /proc/self/fd"
            )
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise SpecError(f"cannot inspect git worktree directory fd: {exc}") from exc
        if not stat.S_ISDIR(st.st_mode):
            raise SpecError("git worktree descriptor does not name a directory")
        return f"/proc/self/fd/{fd}"

    @staticmethod
    @contextmanager
    def pinned_worktree_authority(
        repo_fd: int, common_fd: int | None
    ) -> Iterator[None]:
        """Bind Git's repository authority to inherited directory fds.

        ``common_fd=None`` is permitted only while discovering the common
        directory through the already-pinned repository root. Mutating
        worktree operations require both descriptors and never fall back to
        the display pathname.
        """
        GitOps._inherited_directory_path(repo_fd)
        if common_fd is not None:
            GitOps._inherited_directory_path(common_fd)
        token = _GIT_FD_AUTHORITY.set((repo_fd, common_fd))
        try:
            yield
        finally:
            _GIT_FD_AUTHORITY.reset(token)

    @staticmethod
    def _authority_invocation(cwd: Path) -> tuple[list[str], Path, tuple[int, ...]]:
        authority = _GIT_FD_AUTHORITY.get()
        if authority is None:
            return [], cwd, ()
        repo_fd, common_fd = authority
        repo = GitOps._inherited_directory_path(repo_fd)
        if common_fd is None:
            return [], Path(repo), (repo_fd,)
        common = GitOps._inherited_directory_path(common_fd)
        return (
            [f"--git-dir={common}", f"--work-tree={repo}"],
            Path(repo),
            (repo_fd, common_fd),
        )

    @staticmethod
    def _require_pinned_worktree_authority() -> tuple[int, int]:
        authority = _GIT_FD_AUTHORITY.get()
        if authority is None or authority[1] is None:
            raise SpecError(
                "fd-bound git worktree operation requires pinned repository "
                "and common-directory authority"
            )
        return authority[0], authority[1]

    @staticmethod
    def _run_with_fds(
        repo: Path,
        args: list[str],
        bindings: tuple[tuple[str, str, int], ...],
        *,
        operation: str,
    ) -> None:
        GitOps._require_pinned_worktree_authority()
        token = _GIT_FD_BINDINGS.set(bindings)
        try:
            _git(args, repo)
        except SpecError as exc:
            raise SpecError(f"git {operation} failed in {repo}: {exc}") from exc
        finally:
            _GIT_FD_BINDINGS.reset(token)

    @staticmethod
    def worktree_remove_forced_fd(repo: Path, target_fd: int) -> None:
        """Remove the worktree directory pinned by ``target_fd`` on Linux.

        The inherited ``/proc/self/fd`` reference follows the opened inode,
        not a replaceable public pathname. If that inode is renamed after it
        was opened, Git refuses rather than deleting a replacement at the old
        name. There is deliberately no pathname fallback.
        """
        target = GitOps._inherited_directory_path(target_fd)
        repo_fd, common_fd = GitOps._require_pinned_worktree_authority()
        repo_authority = GitOps._inherited_directory_path(repo_fd)
        common_authority = GitOps._inherited_directory_path(common_fd)
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={common_authority}",
                f"--work-tree={repo_authority}",
                "worktree",
                "remove",
                "--force",
                "--",
                target,
            ],
            cwd=repo_authority,
            capture_output=True,
            check=False,
            pass_fds=(repo_fd, common_fd, target_fd),
        )
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SpecError(
                "git worktree remove --force via pinned fd failed in "
                f"{repo}: {stderr}"
            )

    @staticmethod
    def commit_parents(repo: Path, sha: str) -> tuple[str, ...]:
        """Return the exact parent list for one commit object.

        The managed-worktree adapter validates ``sha`` before calling this
        plumbing helper. ``rev-list`` is used instead of a revision range so
        the result describes exactly one object and is straightforward to
        validate fail-closed at the lifecycle boundary.
        """
        fields = _git(["rev-list", "--parents", "--max-count=1", sha], repo).split()
        if not fields or fields[0] != sha:
            raise SpecError(
                f"git rev-list returned no exact record for commit {sha!r}"
            )
        return tuple(fields[1:])

    @staticmethod
    def commit_all(worktree: Path, message: str) -> str | None:
        """Stage and commit everything; return the new SHA, or None if clean."""
        _git(["add", "-A"], worktree)
        if _git(["status", "--porcelain"], worktree) == "":
            return None
        _git(["commit", "-m", message, "--no-verify"], worktree)
        return GitOps.head_sha(worktree)

    @staticmethod
    def reset_hard_and_clean(worktree: Path, sha: str) -> None:
        """Discard candidate bytes using the already-pinned Git authority."""
        _git(["reset", "--hard", sha], worktree)
        _git(["clean", "-fd"], worktree)

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
