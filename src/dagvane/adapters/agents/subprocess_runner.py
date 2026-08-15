"""Generic subprocess ExternalAgent runner (Autonomous Developer MVP).

The single place Dagvane spawns processes for agent execution (the import
contract allowlists ``subprocess`` here alone). Machine-oriented,
non-interactive invocation only: the prompt goes in over stdin from a durable
prompt artifact, the final message comes back through a runtime-appropriate
output file, and the full stream is captured as a log artifact. Usage is
recorded as unknown — anti-runaway accounting counts calls and wall-clock.

Runtime command shapes:

- ``codex``  — ``codex exec`` non-interactive; ``-s read-only`` for analysis,
  ``-s workspace-write`` for the one writer inside a candidate worktree;
  ``-o`` captures the final message exactly.
- ``agy``    — Antigravity CLI in print mode (optional runtime).
- ``command``— explicit argv template with ``{prompt_file}``/``{output_file}``
  placeholders: custom CLIs and the offline test double.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from dagvane.domain.models import SpecError
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.ports.runtime import Clock, IdSource, Monotonic
from dagvane.workspace.paths import atomic_write_bytes


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


class SubprocessAgentRunner:
    """Runs one AgentInvocation to completion with a hard timeout."""

    def __init__(
        self,
        *,
        runs_dir: Path,
        clock: Clock,
        monotonic: Monotonic,
        ids: IdSource,
    ) -> None:
        self._runs_dir = runs_dir
        self._clock = clock
        self._monotonic = monotonic
        self._ids = ids

    def run(self, invocation: AgentInvocation) -> AgentExecution:
        execution_id = self._ids.new_id("agent")
        run_dir = self._runs_dir / execution_id
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = run_dir / "prompt.md"
        output_file = run_dir / "output.md"
        log_file = run_dir / "stream.log"
        atomic_write_bytes(prompt_file, invocation.prompt.encode("utf-8"))

        command = build_command(invocation, prompt_file, output_file)
        started_ts = self._clock.now_iso()
        started_ms = self._monotonic.now_ms()
        timed_out = False
        exit_code: int | None = None
        try:
            with open(log_file, "wb") as log_handle:
                completed = subprocess.run(
                    command,
                    stdin=prompt_file.open("rb"),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    timeout=invocation.timeout_seconds,
                    cwd=invocation.cwd,
                    check=False,
                )
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        except FileNotFoundError as exc:
            raise SpecError(
                f"external agent runtime {invocation.runtime!r} is not "
                f"installed (command {command[0]!r} not found)"
            ) from exc
        finished_ts = self._clock.now_iso()
        duration_ms = max(0, self._monotonic.now_ms() - started_ms)

        output_text = ""
        if output_file.exists():
            output_text = output_file.read_text(encoding="utf-8", errors="replace")
        else:
            atomic_write_bytes(output_file, b"")

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
            session_ref=_extract_session_ref(log_file),
        )


def _extract_session_ref(log_file: Path) -> str | None:
    """Best-effort provider-native session id (continuity hint only)."""
    if not log_file.exists():
        return None
    try:
        head = log_file.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return None
    for line in head.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("session id:") or lowered.startswith("session_id:"):
            return line.split(":", 1)[1].strip() or None
    return None
