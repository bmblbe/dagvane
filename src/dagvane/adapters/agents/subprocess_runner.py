"""Generic subprocess ExternalAgent runner (Autonomous Developer MVP).

The single place Dagvane spawns processes for agent execution (the import
contract allowlists ``subprocess`` here and in ``adapters/localexec.py``).
Machine-oriented, non-interactive invocation only: the prompt goes in over
stdin from a durable prompt artifact, the final message comes back through a
runtime-appropriate output file, and the combined stdout/stderr stream is
captured bounded and scrubbed. Usage is recorded as unknown — anti-runaway
accounting counts calls and wall-clock.

Sanitization and lifecycle contract (remediation):

- **Scrub before persistence.** The prompt is scrubbed before the durable
  prompt artifact is written (a registered credential is therefore also never
  forwarded to the child). The stream is captured in memory — bounded, never
  as a raw file — then scrubbed, then persisted. The runtime writes its final
  message itself; Dagvane reads it back bounded, scrubs it, persists only the
  scrubbed artifact, and deletes the raw temporary. While the child runs,
  bytes the child itself writes (the raw output temporary, repository files)
  are outside this boundary — a Git worktree is checkout isolation, not a
  security sandbox. After the child is reaped, the prompt artifact is
  verified byte-for-byte against the scrubbed prompt kept in parent memory
  and restored on any deviation before the execution can be accepted;
  mutation after that acceptance point (a surviving descendant sharing the
  directory) is later process-lifecycle scope, not a storage guarantee.
- **Scrub before truncation.** Retained windows are scrubbed first and then
  edge-trimmed by ``longest_variant_chars - 1`` at every capture cut, so a
  partial rendering cannot survive at a boundary. Bytes beyond the retention
  cap are discarded entirely, never persisted raw.
- **Minimal deterministic environment.** The child never inherits the host
  environment. It receives a fixed baseline (PATH, HOME, TMPDIR, LANG,
  LC_ALL, TERM when set) plus the invocation's explicit ``env_passthrough``
  names and ``secret_env`` names; every ``secret_env`` value is registered
  ephemerally with the process scrubber and never persisted.
- **Process-group lifecycle.** POSIX: the child starts its own session; on
  timeout the whole group is TERMed, then KILLed, and the child is reaped.
  Non-POSIX platforms kill the direct child only, and a child that
  double-forks out of its session escapes the guarantee — documented
  limitations, not claims. While the child runs its identity (pid = pgid) is
  persisted at ``process_record_path`` when the caller provides one, so
  another process (owner cancel, resume-time orphan reconciliation) can
  terminate the same group; a non-child cannot be ``wait()``ed, so that path
  polls liveness and verifies ``/proc`` cmdline where available (PID reuse is
  mitigated, not eliminated).

Runtime command shapes:

- ``codex``  — ``codex exec`` non-interactive; ``-s read-only`` for analysis,
  ``-s workspace-write`` for the one writer inside a candidate worktree;
  ``-o`` captures the final message exactly.
- ``agy``    — Antigravity CLI in print mode (optional runtime).
- ``command``— explicit argv template with ``{prompt_file}``/``{output_file}``
  placeholders: custom CLIs and the offline test double.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import stat
import subprocess
from pathlib import Path
from typing import IO

from dagvane.adapters.localexec import kill_process_group, terminate_and_reap
from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError, StorageError
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.ports.runtime import Clock, IdSource, Monotonic
from dagvane.workspace.paths import (
    atomic_write_bytes_at,
    ensure_expected_descendant,
    require_canonical_root,
)

# Deterministic child environment baseline: enough for a CLI to run (PATH),
# authenticate from its own config (HOME), and write scratch files. Nothing
# else crosses the boundary implicitly.
_BASELINE_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")

_READ_CHUNK = 65536
_MAX_STREAM_CHARS = 400_000
_MAX_OUTPUT_CHARS = 400_000

_PROCESS_RECORD_LEAF = "agent-process.json"
_MAX_PROCESS_RECORD_BYTES = 65536


def validate_process_record_location(root: object, path: object, *, ctx: str) -> Path:
    """The single shared validator for a process-record location: ``path``
    must be exactly ``<root>/<valid filesystem id>/agent-process.json`` under
    a canonical, real, absolute ``root``. Rejects non-``Path`` types,
    traversal, outside/root/parent targets, same-prefix siblings, extra
    depth, a wrong leaf name, symlinks anywhere below the root, and an
    existing non-regular leaf. Returns the owner directory pathname as a
    lexical result only — it carries no authority and is never re-opened by
    a path walk; authority is established solely by ``_PinnedOwnerDir``,
    which pins the root by fd and opens the owner relative to that fd."""
    if not isinstance(root, Path) or not isinstance(path, Path):
        raise SpecError(
            f"{ctx}: process record root and path must both be Path objects"
        )
    canonical_root = require_canonical_root(root, ctx=ctx)
    ensure_expected_descendant(canonical_root, path)
    rel_parts = path.parts[len(canonical_root.parts) :]
    if len(rel_parts) != 2:
        raise StorageError(
            f"{ctx}: {path} must be exactly "
            f"<root>/<owner>/{_PROCESS_RECORD_LEAF} under {canonical_root}"
        )
    owner, leaf = rel_parts
    if leaf != _PROCESS_RECORD_LEAF:
        raise StorageError(
            f"{ctx}: {path} must use the fixed leaf name {_PROCESS_RECORD_LEAF!r}"
        )
    validate_filesystem_id(owner, ctx=f"{ctx}: owner directory")
    if path.exists() and not path.is_file():
        raise StorageError(f"{ctx}: {path} exists but is not a regular file")
    return canonical_root / owner


class _PinnedOwnerDir:
    """The validated process-record location, pinned as one authority object.

    There is no validation-return-to-root-open gap: the root is pinned
    *before* ``validate_process_record_location`` ever runs, not after. Only
    strict runtime type checks and pure-lexical root checks (absolute, no
    ``..``) — neither touches the filesystem — precede the pin. The root is
    then opened ``O_DIRECTORY|O_NOFOLLOW`` from the caller-supplied pathname,
    its ``(dev, ino)`` identity captured, and the pathname immediately proven
    to still name that exact directory. Only then does the full canonical
    root/hierarchy/owner-id/leaf validation run, still holding the root fd;
    the pathname identity is re-proven immediately afterward against the
    *same* captured identity — never replaced by whatever the pathname names
    post-validation. A root renamed aside and replaced (ordinary directory or
    symlink) at any point from before the pin through the end of validation
    is therefore caught, not silently adopted. The owner directory is then
    opened strictly relative to the pinned root fd — never by re-walking its
    own pathname — with ``O_DIRECTORY|O_NOFOLLOW`` and pinned the same way.
    ``prove()`` re-proves BOTH identities (root pathname, and the owner entry
    resolved relative to the pinned root fd) and is re-run before every
    record effect and immediately before each signal decision, so a root or
    owner swapped for a replacement after validation is detected and never
    adopted; no path re-walk ever establishes new authority. All record I/O
    is resolved strictly relative to the owner fd. Both fds are released by
    ``close()`` and on every construction failure.
    """

    def __init__(self, root: object, path: object, *, ctx: str) -> None:
        self._ctx = ctx
        if not isinstance(root, Path) or not isinstance(path, Path):
            raise SpecError(
                f"{ctx}: process record root and path must both be Path objects"
            )
        if not root.is_absolute():
            raise StorageError(f"{ctx}: {root} must be an absolute path")
        if ".." in root.parts:
            raise StorageError(f"{ctx}: {root} must not contain '..' components")
        self._root = root
        try:
            self._root_fd = os.open(
                root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as exc:
            raise StorageError(
                f"{ctx}: cannot pin process-record root {root}: {exc}"
            ) from exc
        try:
            root_st = os.fstat(self._root_fd)
            self._root_identity = (root_st.st_dev, root_st.st_ino)
            self._prove_root()
            owner_dir = validate_process_record_location(root, path, ctx=ctx)
            # Immediately re-prove: validation above walked the pathname
            # (resolve(), stat calls) and took real time — a root swapped for
            # a replacement during that window, even one that itself
            # validates cleanly, must never be adopted as the pinned
            # identity. The captured identity is never reassigned here.
            self._prove_root()
            self._owner_dir = owner_dir
            self._owner_name = owner_dir.name
            try:
                self._owner_fd = os.open(
                    self._owner_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=self._root_fd,
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise StorageError(
                    f"{ctx}: cannot pin process-record owner directory "
                    f"{owner_dir}: {exc}"
                ) from exc
            try:
                owner_st = os.fstat(self._owner_fd)
                self._owner_identity = (owner_st.st_dev, owner_st.st_ino)
                self.prove()
            except BaseException:
                os.close(self._owner_fd)
                raise
        except BaseException:
            os.close(self._root_fd)
            raise

    def _prove_root(self) -> None:
        """Prove the root pathname still names the pinned root directory."""
        try:
            st = os.lstat(self._root)
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: process-record root {self._root} vanished: {exc}"
            ) from exc
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self._root_identity:
            raise StorageError(
                f"{self._ctx}: process-record root {self._root} no longer "
                "names the pinned directory"
            )

    def prove(self) -> None:
        """Re-prove BOTH pinned identities: the root pathname, and the owner
        entry resolved strictly relative to the pinned root fd."""
        self._prove_root()
        try:
            st = os.stat(
                self._owner_name, dir_fd=self._root_fd, follow_symlinks=False
            )
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: process-record owner directory "
                f"{self._owner_dir} vanished: {exc}"
            ) from exc
        if not stat.S_ISDIR(st.st_mode) or (st.st_dev, st.st_ino) != self._owner_identity:
            raise StorageError(
                f"{self._ctx}: process-record owner directory "
                f"{self._owner_dir} no longer names the pinned directory"
            )

    def write_record(self, doc: dict[str, object]) -> None:
        self.prove()
        atomic_write_bytes_at(
            self._owner_fd,
            _PROCESS_RECORD_LEAF,
            json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )

    def read_record(self) -> dict[str, object] | None:
        """Bounded, fd-relative record read: ``None`` when missing; a
        malformed record (oversized, invalid JSON, non-object) comes back as
        an empty dict so the caller's field validation handles cleanup."""
        self.prove()
        fd = -1
        try:
            fd = os.open(
                _PROCESS_RECORD_LEAF,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self._owner_fd,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: cannot read {_PROCESS_RECORD_LEAF}: "
                "refusing to follow a symlink"
            ) from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(
                    f"{self._ctx}: {_PROCESS_RECORD_LEAF} is not a regular file"
                )
            handle = os.fdopen(fd, "rb")
            fd = -1  # ownership transferred to the file object
            with handle:
                data = handle.read(_MAX_PROCESS_RECORD_BYTES + 1)
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: cannot read {_PROCESS_RECORD_LEAF}: {exc}"
            ) from exc
        finally:
            if fd != -1:
                os.close(fd)
        if len(data) > _MAX_PROCESS_RECORD_BYTES:
            return {}
        try:
            loaded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def unlink_record(self) -> None:
        self.prove()
        try:
            os.unlink(_PROCESS_RECORD_LEAF, dir_fd=self._owner_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise StorageError(
                f"{self._ctx}: cannot remove {_PROCESS_RECORD_LEAF}: {exc}"
            ) from exc

    def close(self) -> None:
        try:
            os.close(self._owner_fd)
        finally:
            os.close(self._root_fd)


def build_command(
    invocation: AgentInvocation, prompt_file: Path, output_file: Path
) -> list[str]:
    if invocation.runtime == "codex":
        command = ["codex", "exec"]
        if invocation.model is not None:
            command += ["-m", invocation.model]
        if invocation.reasoning is not None:
            command += ["-c", f'model_reasoning_effort="{invocation.reasoning}"']
        command += [
            "-s",
            "workspace-write" if invocation.write_access else "read-only",
            "--skip-git-repo-check",
            "-C",
            str(invocation.cwd),
            "-o",
            str(output_file),
            "-",
        ]
        return command
    if invocation.runtime == "agy":
        command = ["agy"]
        if invocation.model is not None:
            command += ["--model", invocation.model]
        if invocation.reasoning is not None:
            command += ["--effort", invocation.reasoning]
        command += ["--print", "--output-file", str(output_file)]
        return command
    if invocation.runtime == "command":
        if not invocation.command_template:
            raise SpecError("runtime 'command' requires a command template")
        return [
            part.replace("{prompt_file}", str(prompt_file)).replace(
                "{output_file}", str(output_file)
            )
            for part in invocation.command_template
        ]
    raise SpecError(f"unknown external agent runtime {invocation.runtime!r}")


def child_environment(
    invocation: AgentInvocation, scrubber: SecretScrubber
) -> dict[str, str]:
    """The minimal deterministic child environment (see module docstring)."""
    env = {name: os.environ[name] for name in _BASELINE_ENV if name in os.environ}
    for name in invocation.env_passthrough:
        if name in os.environ:
            env[name] = os.environ[name]
    for name in invocation.secret_env:
        value = os.environ.get(name)
        if value:
            scrubber.register(value)  # ephemeral; never persisted
            env[name] = value
    return env


def _edge_trimmed(scrubbed: str, scrubber: SecretScrubber, *, tail_kept: bool) -> str:
    """Drop the possibly-contaminated cut edge of an already-scrubbed window.

    ``tail_kept=True`` means the retained window is a *suffix* of the stream
    (the cut is at the start); otherwise it is a prefix (the cut is at the
    end). See ``SecretScrubber.longest_variant_chars`` for the proof sketch.
    """
    guard = scrubber.longest_variant_chars
    if guard <= 1:
        return scrubbed
    return scrubbed[guard - 1 :] if tail_kept else scrubbed[: -(guard - 1)]


class SubprocessAgentRunner:
    """Runs one AgentInvocation to completion with a hard timeout."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        clock: Clock,
        monotonic: Monotonic,
        ids: IdSource,
        scrubber: SecretScrubber | None = None,
        max_stream_chars: int = _MAX_STREAM_CHARS,
        max_output_chars: int = _MAX_OUTPUT_CHARS,
    ) -> None:
        self._runs_dir = require_canonical_root(runs_dir, ctx="agent runner runs_dir")
        # Captured once, at construction, from the already-canonicalized
        # root: the (device, inode) identity every later access re-proves
        # the live ``runs_dir`` against, so a post-construction replacement
        # (symlink or ordinary directory swapped in at the same pathname)
        # is detected rather than silently adopted as the trusted root.
        root_st = os.stat(self._runs_dir)
        self._runs_dir_identity = (root_st.st_dev, root_st.st_ino)
        self._clock = clock
        self._monotonic = monotonic
        self._ids = ids
        self._scrubber = scrubber if scrubber is not None else process_scrubber()
        self._max_stream_chars = max_stream_chars
        self._max_output_chars = max_output_chars

    def run(self, invocation: AgentInvocation) -> AgentExecution:
        # The process-record pair (path/root) is validated and its owner
        # directory pinned by fd as the very first operation of this call —
        # before the id is generated, the clock is read, the environment is
        # assembled, the prompt is scrubbed, the run directory is created,
        # or anything is spawned. A malformed pair or an invalid/hostile
        # process-record location must fail closed with zero other effects.
        process_owner = self._validate_and_pin_process_record(invocation)
        try:
            return self._run_with_pinned_process_owner(invocation, process_owner)
        finally:
            if process_owner is not None:
                process_owner.close()

    def _validate_and_pin_process_record(
        self, invocation: AgentInvocation
    ) -> _PinnedOwnerDir | None:
        has_path = invocation.process_record_path is not None
        has_root = invocation.process_record_root is not None
        if has_path != has_root:
            raise SpecError(
                "agent invocation: process_record_path and "
                "process_record_root must be both present or both absent"
            )
        if not has_path:
            return None
        return _PinnedOwnerDir(
            invocation.process_record_root,
            invocation.process_record_path,
            ctx="agent runner: process record",
        )

    def _run_with_pinned_process_owner(
        self,
        invocation: AgentInvocation,
        process_owner: _PinnedOwnerDir | None,
    ) -> AgentExecution:
        # The generated id is validated against the shared canonical
        # filesystem-id policy before any other effect (clock reading,
        # environment assembly, prompt scrubbing, directory creation, or
        # process spawn): a malformed id must never reach a path.
        execution_id = self._ids.new_id("agent")
        validate_filesystem_id(execution_id, ctx="agent runner: execution_id")
        run_dir = self._runs_dir / execution_id
        ensure_expected_descendant(self._runs_dir, run_dir)
        # Bind to the trusted root by file descriptor, not by re-walking its
        # pathname: opened with O_NOFOLLOW so a root replaced by a symlink
        # after construction fails here outright, then its fstat identity is
        # compared against the (device, inode) captured at construction so a
        # root replaced by an ordinary directory is caught too. Every access
        # below — the run-directory creation included — is resolved relative
        # to this fd, never by walking ``self._runs_dir`` again.
        try:
            root_fd = os.open(
                self._runs_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as exc:
            raise StorageError(
                f"cannot pin agent runs directory {self._runs_dir}: {exc}"
            ) from exc
        try:
            root_st = os.fstat(root_fd)
            if (root_st.st_dev, root_st.st_ino) != self._runs_dir_identity:
                raise StorageError(
                    f"agent runs directory {self._runs_dir} no longer names "
                    "the directory pinned at construction"
                )
            # The pathname itself must still name that same identity too:
            # a swap staged between the fd open above and this check would
            # otherwise go undetected because the fd already holds the old
            # (still-valid) identity.
            try:
                path_st = os.lstat(self._runs_dir)
            except OSError as exc:
                raise StorageError(
                    f"agent runs directory {self._runs_dir} vanished: {exc}"
                ) from exc
            if (
                not stat.S_ISDIR(path_st.st_mode)
                or (path_st.st_dev, path_st.st_ino) != self._runs_dir_identity
            ):
                raise StorageError(
                    f"agent runs directory {self._runs_dir} no longer names "
                    "the directory pinned at construction"
                )
            # Ownership of the run directory is acquired by a single
            # exclusive mkdir, resolved relative to the pinned root fd: a
            # pre-existing directory or link entry at this name is a
            # collision, not something to adopt, reuse, or clobber — it is
            # left untouched and no child is ever spawned for this call.
            try:
                os.mkdir(execution_id, dir_fd=root_fd)
            except FileExistsError as exc:
                raise StorageError(
                    f"agent run directory {run_dir} already exists"
                ) from exc
            except OSError as exc:
                raise StorageError(
                    f"cannot create agent run directory {run_dir}: {exc}"
                ) from exc
            # Pin the just-created, exclusively-owned run directory by file
            # descriptor before anything else touches it: every subsequent
            # access to a leaf inside it (the raw output read, its removal)
            # is resolved relative to this fd (O_NOFOLLOW, no symlink
            # components, and — via dir_fd — no re-walk of the root
            # pathname either), not by re-walking the pathname — so a later
            # attempt to replace the run_dir path itself, or the root above
            # it, cannot redirect those accesses anywhere else.
            try:
                run_dir_fd = os.open(
                    execution_id,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise StorageError(
                    f"cannot pin agent run directory {run_dir}: {exc}"
                ) from exc
            try:
                # A root swap staged in the window between the root-fd
                # identity check above and the mkdir just performed would
                # otherwise land the new run directory inside a replacement
                # root undetected (dir_fd-relative mkdir succeeds against
                # whatever the fd already names). Re-prove the root's
                # pathname identity one more time before any clock read,
                # environment assembly, prompt write, or spawn — a
                # mismatch here must fail closed with the run directory
                # already pinned by fd but never used.
                try:
                    post_mkdir_st = os.lstat(self._runs_dir)
                except OSError as exc:
                    raise StorageError(
                        f"agent runs directory {self._runs_dir} vanished: {exc}"
                    ) from exc
                if (
                    not stat.S_ISDIR(post_mkdir_st.st_mode)
                    or (post_mkdir_st.st_dev, post_mkdir_st.st_ino)
                    != self._runs_dir_identity
                ):
                    raise StorageError(
                        f"agent runs directory {self._runs_dir} no longer "
                        "names the directory pinned at construction"
                    )
                return self._run_in_pinned_dir(
                    invocation, execution_id, run_dir, run_dir_fd, process_owner
                )
            finally:
                os.close(run_dir_fd)
        finally:
            os.close(root_fd)

    def _run_in_pinned_dir(
        self,
        invocation: AgentInvocation,
        execution_id: str,
        run_dir: Path,
        run_dir_fd: int,
        process_owner: _PinnedOwnerDir | None,
    ) -> AgentExecution:
        prompt_file = run_dir / "prompt.md"
        output_file = run_dir / "output.md"
        raw_output_file = run_dir / "output.raw"
        log_file = run_dir / "stream.log"

        scrubber = self._scrubber
        env = child_environment(invocation, scrubber)  # registers secrets first
        # Every Dagvane-side write/open/remove of a fixed leaf below is
        # resolved strictly relative to the pinned ``run_dir_fd``, never by
        # re-walking ``run_dir``'s pathname — so a later swap of the run
        # directory itself (to an ordinary directory or an outside symlink)
        # cannot redirect these accesses. The pathnames below are still
        # needed to hand the external runtime CLI its own arguments (it has
        # no access to our fd), but Dagvane never opens through them again.
        # The exact scrubbed prompt bytes are kept in parent memory for the
        # post-exit provenance check below: prompt.md is the durable record
        # of what the child was actually asked, so what is published as
        # ``prompt_path`` must be provably these bytes, not whatever the
        # child left behind.
        prompt_bytes = scrubber.scrub(invocation.prompt).encode("utf-8")
        atomic_write_bytes_at(run_dir_fd, prompt_file.name, prompt_bytes)

        command = build_command(invocation, prompt_file, raw_output_file)
        started_ts = self._clock.now_iso()
        started_ms = self._monotonic.now_ms()
        with self._open_pinned_leaf(run_dir_fd, prompt_file.name) as stdin_handle:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=stdin_handle,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=invocation.cwd,
                    env=env,
                    start_new_session=(os.name == "posix"),
                )
            except FileNotFoundError as exc:
                raise SpecError(
                    f"external agent runtime {invocation.runtime!r} is not "
                    f"installed (command {command[0]!r} not found)"
                ) from exc
            if process_owner is not None:
                try:
                    process_owner.write_record(
                        {
                            "pid": proc.pid,
                            "pgid": proc.pid if os.name == "posix" else None,
                            "execution_id": execution_id,
                            "started_ts": started_ts,
                            "command": command[0],
                        }
                    )
                except StorageError:
                    # The authority root or the owner directory was swapped
                    # out from under us between pinning and this write: the record was
                    # never persisted, so no other process can ever cancel
                    # this child through it. Fail closed by killing the
                    # already-spawned process group ourselves rather than
                    # leaving an unrecorded, uncancellable writer running.
                    terminate_and_reap(proc)
                    raise
            try:
                timed_out, exit_code, retained, total = self._pump(
                    proc, invocation.timeout_seconds
                )
            finally:
                if process_owner is not None:
                    process_owner.unlink_record()
        finished_ts = self._clock.now_iso()
        duration_ms = max(0, self._monotonic.now_ms() - started_ms)

        stream_text = retained.decode("utf-8", errors="replace")
        stream_truncated = total > len(retained)
        scrubbed_stream = scrubber.scrub(stream_text)
        if stream_truncated:
            scrubbed_stream = (
                f"[stream truncated: kept the scrubbed tail of {total} bytes]\n"
                + _edge_trimmed(scrubbed_stream, scrubber, tail_kept=True)
            )
        atomic_write_bytes_at(run_dir_fd, log_file.name, scrubbed_stream.encode("utf-8"))

        raw_kept, output_truncated = self._read_raw_output(run_dir_fd, raw_output_file.name)
        output_text = ""
        if raw_kept is not None:
            output_text = scrubber.scrub(raw_kept)
            if output_truncated:
                output_text = (
                    _edge_trimmed(output_text, scrubber, tail_kept=False)
                    + "\n[output truncated]"
                )
            try:
                os.unlink(raw_output_file.name, dir_fd=run_dir_fd)
            except FileNotFoundError:
                pass
        atomic_write_bytes_at(run_dir_fd, output_file.name, output_text.encode("utf-8"))

        # The child ran as a normal, unprivileged command with pathname
        # access to its own run directory: before this execution is
        # accepted, prove the prompt artifact still holds exactly the
        # scrubbed bytes Dagvane wrote — a child that overwrote, deleted,
        # or replaced prompt.md must never have that forgery published as
        # provenance under ``prompt_path``. Mutation *after* this call
        # returns (by a surviving descendant sharing the directory) is
        # process-lifecycle scope — group termination and orphan
        # reconciliation — not a storage checkpoint's to solve, and is
        # deliberately not addressed here.
        self._verify_prompt_provenance(run_dir_fd, prompt_file.name, prompt_bytes)

        # Prove the public run_dir pathname still names the same directory
        # inode as the pinned fd before any artifact path is handed back: if
        # the run directory itself was swapped for a replacement (ordinary
        # directory or outside symlink) at any point, the artifacts above
        # were still correctly written through run_dir_fd, but the returned
        # pathnames must never be allowed to appear to resolve into that
        # replacement.
        try:
            path_st = os.lstat(run_dir)
        except OSError as exc:
            raise StorageError(f"agent run directory {run_dir} vanished: {exc}") from exc
        fd_st = os.fstat(run_dir_fd)
        if (
            not stat.S_ISDIR(path_st.st_mode)
            or path_st.st_dev != fd_st.st_dev
            or path_st.st_ino != fd_st.st_ino
        ):
            raise StorageError(
                f"agent run directory {run_dir} no longer names the owned directory"
            )
        # Prove the public runs_dir pathname — the parent every caller
        # trusts — still names the identity pinned at construction too: the
        # run_dir check above alone would miss a root swapped out from
        # under an otherwise-untouched, still-correctly-named run directory.
        try:
            runs_dir_st = os.lstat(self._runs_dir)
        except OSError as exc:
            raise StorageError(
                f"agent runs directory {self._runs_dir} vanished: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(runs_dir_st.st_mode)
            or (runs_dir_st.st_dev, runs_dir_st.st_ino) != self._runs_dir_identity
        ):
            raise StorageError(
                f"agent runs directory {self._runs_dir} no longer names the "
                "directory pinned at construction"
            )

        return AgentExecution(
            runtime=invocation.runtime,
            model=invocation.model,
            reasoning=invocation.reasoning,
            cwd=str(invocation.cwd),
            started_ts=started_ts,
            finished_ts=finished_ts,
            duration_ms=duration_ms,
            exit_code=exit_code,
            timed_out=timed_out,
            output_text=output_text,
            prompt_path=str(prompt_file),
            output_path=str(output_file),
            log_path=str(log_file),
            session_ref=_extract_session_ref(scrubbed_stream),
        )

    @staticmethod
    def _open_pinned_leaf(run_dir_fd: int, name: str) -> IO[bytes]:
        """Open a fixed leaf strictly relative to the pinned run-directory
        fd (``O_NOFOLLOW``: never follows a symlink planted at the leaf),
        proving it is a regular file before handing the fd to the caller.

        ``O_NONBLOCK`` keeps a leaf replaced with a FIFO from hanging the
        open call itself (no writer yet would otherwise block indefinitely);
        it is a no-op for the regular file this must prove out as. Any
        ``OSError`` — including the symlink refusal — is translated to
        ``StorageError`` rather than escaping raw.
        """
        fd = -1
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_dir_fd
            )
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(f"cannot open {name}: not a regular file")
            handle = os.fdopen(fd, "rb")
            fd = -1  # ownership transferred to the file object
            return handle
        except OSError as exc:
            raise StorageError(f"cannot open {name}: {exc}") from exc
        finally:
            if fd != -1:
                os.close(fd)

    @staticmethod
    def _verify_prompt_provenance(run_dir_fd: int, name: str, expected: bytes) -> None:
        """Prove the durable prompt leaf still holds exactly ``expected``
        (content and length), reading it strictly relative to the pinned,
        owned run-directory fd — ``O_NOFOLLOW`` refuses a planted symlink
        without ever touching its target, ``O_NONBLOCK`` keeps a planted
        FIFO from hanging the open, and the opened fd must ``fstat`` as a
        regular file before a byte is read.

        On any deviation — missing leaf, symlink, wrong type, altered
        bytes — the original scrubbed prompt is restored atomically through
        the same pinned fd (an empty replacement directory is removed
        first; anything unremovable fails closed) and ``StorageError`` is
        raised: success is never returned with forged provenance. A
        mutation landing *after* this check (a surviving descendant of the
        reaped child) is later process-lifecycle scope, not covered here.
        """
        failure: str | None = None
        fd = -1
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_dir_fd
            )
        except FileNotFoundError:
            failure = "is missing"
        except OSError:
            failure = "was replaced (refusing to follow a symlink)"
        else:
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    failure = "is no longer a regular file"
                else:
                    handle = os.fdopen(fd, "rb")
                    fd = -1  # ownership transferred to the file object
                    with handle:
                        data = handle.read(len(expected) + 1)
                    if data != expected:
                        failure = "no longer holds the bytes Dagvane wrote"
            except OSError as exc:
                failure = f"could not be read back ({exc})"
            finally:
                if fd != -1:
                    os.close(fd)
        if failure is None:
            return
        try:
            atomic_write_bytes_at(run_dir_fd, name, expected)
        except StorageError:
            # The leaf may have been replaced by a directory: an atomic
            # rename cannot land on one. Remove it if (and only if) empty,
            # then retry once; anything else stays failed closed.
            try:
                os.rmdir(name, dir_fd=run_dir_fd)
            except OSError as exc:
                raise StorageError(
                    f"agent prompt artifact {name} {failure} and the "
                    f"original could not be restored: {exc}"
                ) from exc
            atomic_write_bytes_at(run_dir_fd, name, expected)
        raise StorageError(
            f"agent prompt artifact {name} {failure}; the original scrubbed "
            "prompt was restored and this execution is refused"
        )

    def _read_raw_output(
        self, run_dir_fd: int, name: str
    ) -> tuple[str | None, bool]:
        """Read the fixed raw-output leaf strictly relative to the pinned,
        owned run directory fd: never by re-walking a pathname.

        ``O_NOFOLLOW`` refuses a symlink at the leaf outright (no separate
        check-then-open window to race); ``O_NONBLOCK`` keeps a planted FIFO
        from hanging the open. The opened fd is then ``fstat``ed and must
        prove out as a regular file before a single byte is read — anything
        else (missing entry aside) raises ``StorageError`` without reading.
        Returns ``(None, False)`` when the leaf does not exist (a valid,
        empty-output run), otherwise ``(kept_text, truncated)``.
        """
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=run_dir_fd,
            )
        except FileNotFoundError:
            return None, False
        except OSError as exc:
            raise StorageError(f"cannot read {name}: refusing to follow a symlink") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StorageError(f"cannot read {name}: not a regular file")
            with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
                fd = -1  # ownership transferred to the file object
                raw_kept = handle.read(self._max_output_chars)
                truncated = handle.read(1) != ""
            return raw_kept, truncated
        finally:
            if fd != -1:
                os.close(fd)

    def _pump(
        self, proc: subprocess.Popen[bytes], timeout_seconds: int
    ) -> tuple[bool, int | None, bytes, int]:
        """Read the child's stream with a hard deadline and bounded retention.

        Returns ``(timed_out, exit_code, retained_tail_bytes, total_bytes)``.
        The retained window is a suffix of the stream (diagnostics live at the
        end); discarded bytes are counted, never stored.
        """
        assert proc.stdout is not None
        deadline = self._monotonic.now_ms() + timeout_seconds * 1000
        os.set_blocking(proc.stdout.fileno(), False)
        chunks: list[bytes] = []
        retained = 0
        total = 0
        timed_out = False
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining_ms = deadline - self._monotonic.now_ms()
                if remaining_ms <= 0:
                    timed_out = True
                    break
                if not sel.select(timeout=min(remaining_ms / 1000.0, 1.0)):
                    continue
                try:
                    chunk = proc.stdout.read(_READ_CHUNK)
                except BlockingIOError:  # pragma: no cover — spurious readiness
                    continue
                if chunk is None:  # pragma: no cover — non-blocking, no data
                    continue
                if chunk == b"":
                    break  # EOF: every stream writer in the group is gone
                total += len(chunk)
                chunks.append(chunk)
                retained += len(chunk)
                while retained > self._max_stream_chars and len(chunks) > 1:
                    retained -= len(chunks.pop(0))
        finally:
            sel.unregister(proc.stdout)
            sel.close()

        exit_code: int | None = None
        if timed_out:
            terminate_and_reap(proc)
        else:
            remaining_ms = deadline - self._monotonic.now_ms()
            try:
                exit_code = proc.wait(timeout=max(0.001, remaining_ms / 1000.0))
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_and_reap(proc)
        # Best-effort non-blocking drain of bytes buffered before the kill.
        for _ in range(100):
            try:
                chunk = proc.stdout.read(_READ_CHUNK)
            except (BlockingIOError, OSError, ValueError):
                break
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
        proc.stdout.close()
        data = b"".join(chunks)
        if len(data) > self._max_stream_chars:
            data = data[-self._max_stream_chars :]
        return timed_out, exit_code, data, total


def _extract_session_ref(stream_text: str) -> str | None:
    """Best-effort provider-native session id (continuity hint only)."""
    for line in stream_text[:4000].splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("session id:") or lowered.startswith("session_id:"):
            return line.split(":", 1)[1].strip() or None
    return None


# -- cross-process termination (owner cancel, orphan reconciliation) ---------

_KILL_POLL_ROUNDS = 20
_KILL_POLL_STEP_MS = 100


def _sleep_ms(ms: int) -> None:
    """Sleep without the ``time`` module (restricted to the runtime port):
    an empty selector honors its timeout on every supported platform."""
    sel = selectors.DefaultSelector()
    try:
        sel.select(timeout=ms / 1000.0)
    finally:
        sel.close()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — exists, other user
        return True
    return True


def _pid_matches_record(pid: int, recorded_command: str) -> bool:
    """Best-effort PID-reuse guard: with procfs, require the recorded argv[0];
    without it, err toward *not* killing an unverifiable process."""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if not proc_cmdline.exists():
        return not Path("/proc").exists()  # no procfs → cannot verify; be safe
    try:
        argv0 = proc_cmdline.read_bytes().split(b"\x00", 1)[0].decode(
            "utf-8", errors="replace"
        )
    except OSError:  # pragma: no cover — process vanished mid-read
        return False
    return argv0 == recorded_command


def terminate_recorded_process(record_path: Path, *, allowed_root: Path) -> bool:
    """Terminate the process group persisted at ``record_path`` if it is
    still alive; returns True when a live process was found and stopped.

    ``record_path`` must be exactly ``<allowed_root>/<owner>/agent-process.json``
    (see ``validate_process_record_location``); ``allowed_root`` itself is
    pinned by fd first, then the owner directory is opened relative to that
    pinned root fd — both before the record is ever read — and every effect
    below (read, TERM, KILL, unlink) re-proves both pinned identities
    immediately before acting. A root or owner directory swapped for a
    replacement (ordinary directory or symlink) is detected, the
    corresponding effect is refused rather than landing anywhere else, and
    ``StorageError`` is raised: invalid authority sends no signal.

    Used by owner cancellation and resume-time orphan reconciliation, from a
    process that did not spawn the child: it cannot ``wait()`` a non-child,
    so death is polled (the orphan is reaped by init). Bounded: TERM, poll,
    KILL, poll. A process that ignores KILL (unkillable D-state) is reported
    honestly by leaving the record in place and raising. A missing owner
    directory or missing/malformed/dead/reused record returns False with
    contained cleanup; those contained shapes never raise (invalid
    hierarchy or invalidated pinned authority does, as above).
    """
    try:
        owner = _PinnedOwnerDir(
            allowed_root, record_path, ctx="terminate_recorded_process"
        )
    except FileNotFoundError:
        return False
    try:
        doc = owner.read_record()
        if doc is None:
            return False
        pid = doc.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            owner.unlink_record()
            return False
        command = str(doc.get("command", ""))
        if not _pid_alive(pid) or not _pid_matches_record(pid, command):
            owner.unlink_record()
            return False
        owner.prove()
        kill_process_group(pid, signal.SIGTERM)
        for _ in range(_KILL_POLL_ROUNDS):
            if not _pid_alive(pid):
                owner.unlink_record()
                return True
            _sleep_ms(_KILL_POLL_STEP_MS)
        owner.prove()
        kill_process_group(pid, signal.SIGKILL)
        for _ in range(_KILL_POLL_ROUNDS):
            if not _pid_alive(pid):
                owner.unlink_record()
                return True
            _sleep_ms(_KILL_POLL_STEP_MS)
        raise SpecError(
            f"process {pid} (recorded at {record_path}) survived SIGKILL; "
            "cannot guarantee the writer is stopped"
        )
    finally:
        owner.close()
