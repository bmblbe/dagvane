"""External coding-agent port (Autonomous Developer MVP).

An ExternalAgent is a non-interactive subprocess runtime (Codex, Antigravity,
a custom command) that receives a prompt and returns a final text answer plus
recorded provenance. It is deliberately separate from ``ChatBackend``: the
runtime owns its own tools and repository access; Dagvane owns orchestration,
budgets, and durable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    """One bounded, non-interactive external agent execution request."""

    runtime: str  # "codex" | "agy" | "command"
    prompt: str
    cwd: Path
    model: str | None = None
    reasoning: str | None = None
    timeout_seconds: int = 1800
    write_access: bool = False  # False = read-only analysis sandbox
    # For runtime="command": argv template; "{prompt_file}" and
    # "{output_file}" placeholders are substituted (test double / custom CLI).
    command_template: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AgentExecution:
    """Recorded outcome of one external agent run (usage stays unknown)."""

    runtime: str
    model: str | None
    reasoning: str | None
    cwd: str
    started_ts: str
    finished_ts: str
    duration_ms: int
    exit_code: int | None  # None when the process timed out and was killed
    timed_out: bool
    output_text: str  # the agent's final message (may be empty on failure)
    prompt_path: str  # durable input artifact
    output_path: str  # durable output artifact
    log_path: str  # full stream/diagnostic log
    session_ref: str | None = None  # provider-native continuity hint only

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ExternalAgentRunner(Protocol):
    def run(self, invocation: AgentInvocation) -> AgentExecution: ...
