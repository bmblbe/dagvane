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
  security sandbox.
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

import os
import selectors
import signal
import subprocess
from pathlib import Path

from dagvane.adapters.localexec import kill_process_group, terminate_and_reap
from dagvane.domain.models import SpecError, StorageError
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.ports.runtime import Clock, IdSource, Monotonic
from dagvane.workspace.paths import atomic_write_bytes, atomic_write_json, read_json

# Deterministic child environment baseline: enough for a CLI to run (PATH),
# authenticate from its own config (HOME), and write scratch files. Nothing
# else crosses the boundary implicitly.
_BASELINE_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")

_READ_CHUNK = 65536
_MAX_STREAM_CHARS = 400_000
_MAX_OUTPUT_CHARS = 400_000


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
        self._runs_dir = runs_dir
        self._clock = clock
        self._monotonic = monotonic
        self._ids = ids
        self._scrubber = scrubber if scrubber is not None else process_scrubber()
        self._max_stream_chars = max_stream_chars
        self._max_output_chars = max_output_chars

    def run(self, invocation: AgentInvocation) -> AgentExecution:
        execution_id = self._ids.new_id("agent")
        run_dir = self._runs_dir / execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = run_dir / "prompt.md"
        output_file = run_dir / "output.md"
        raw_output_file = run_dir / "output.raw"
        log_file = run_dir / "stream.log"

        scrubber = self._scrubber
        env = child_environment(invocation, scrubber)  # registers secrets first
        atomic_write_bytes(
            prompt_file, scrubber.scrub(invocation.prompt).encode("utf-8")
        )

        command = build_command(invocation, prompt_file, raw_output_file)
        started_ts = self._clock.now_iso()
        started_ms = self._monotonic.now_ms()
        with open(prompt_file, "rb") as stdin_handle:
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
            if invocation.process_record_path is not None:
                atomic_write_json(
                    invocation.process_record_path,
                    {
                        "pid": proc.pid,
                        "pgid": proc.pid if os.name == "posix" else None,
                        "execution_id": execution_id,
                        "started_ts": started_ts,
                        "command": command[0],
                    },
                )
            try:
                timed_out, exit_code, retained, total = self._pump(
                    proc, invocation.timeout_seconds
                )
            finally:
                if invocation.process_record_path is not None:
                    invocation.process_record_path.unlink(missing_ok=True)
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
        atomic_write_bytes(log_file, scrubbed_stream.encode("utf-8"))

        output_text = ""
        if raw_output_file.exists():
            with open(raw_output_file, encoding="utf-8", errors="replace") as handle:
                raw_kept = handle.read(self._max_output_chars)
                output_truncated = handle.read(1) != ""
            output_text = scrubber.scrub(raw_kept)
            if output_truncated:
                output_text = (
                    _edge_trimmed(output_text, scrubber, tail_kept=False)
                    + "\n[output truncated]"
                )
            raw_output_file.unlink(missing_ok=True)
        atomic_write_bytes(output_file, output_text.encode("utf-8"))

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


def terminate_recorded_process(record_path: Path) -> bool:
    """Terminate the process group persisted at ``record_path`` if it is
    still alive; returns True when a live process was found and stopped.

    Used by owner cancellation and resume-time orphan reconciliation, from a
    process that did not spawn the child: it cannot ``wait()`` a non-child,
    so death is polled (the orphan is reaped by init). Bounded: TERM, poll,
    KILL, poll. A process that ignores KILL (unkillable D-state) is reported
    honestly by leaving the record in place and raising.
    """
    if not record_path.exists():
        return False
    try:
        doc = read_json(record_path)
    except StorageError:
        record_path.unlink(missing_ok=True)
        return False
    pid = doc.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        record_path.unlink(missing_ok=True)
        return False
    command = str(doc.get("command", ""))
    if not _pid_alive(pid) or not _pid_matches_record(pid, command):
        record_path.unlink(missing_ok=True)
        return False
    kill_process_group(pid, signal.SIGTERM)
    for _ in range(_KILL_POLL_ROUNDS):
        if not _pid_alive(pid):
            record_path.unlink(missing_ok=True)
            return True
        _sleep_ms(_KILL_POLL_STEP_MS)
    kill_process_group(pid, signal.SIGKILL)
    for _ in range(_KILL_POLL_ROUNDS):
        if not _pid_alive(pid):
            record_path.unlink(missing_ok=True)
            return True
        _sleep_ms(_KILL_POLL_STEP_MS)
    raise SpecError(
        f"process {pid} (recorded at {record_path}) survived SIGKILL; "
        "cannot guarantee the writer is stopped"
    )
