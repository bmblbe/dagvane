"""Non-interactive workspace chat over Dagvane-owned LogicalConversations.

Canonical history is durable under ``.dagvane/conversations/<id>/`` —
provider-native sessions are optional continuity hints only, and losing them
loses nothing. Each ``dagvane chat`` invocation assembles a compact prompt
(recent history + the new message + workspace facts), records it as the
exchange's input artifact (the ContextSnapshot equivalent for external
agents), runs one bounded external-agent execution, and persists both sides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dagvane.application.resources import ResourceSpec
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, IdSource
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import (
    Workspace,
    append_jsonl,
    atomic_write_json,
    read_json,
    read_jsonl,
)


@dataclass(frozen=True, slots=True)
class ConversationInfo:
    conversation_id: str
    created_ts: str
    updated_ts: str
    messages: int
    title: str


class ConversationStore:
    """Durable workspace-owned conversations."""

    def __init__(self, workspace: Workspace, clock: Clock, ids: IdSource) -> None:
        self._workspace = workspace
        self._clock = clock
        self._ids = ids

    def _dir(self, conversation_id: str) -> Path:
        return self._workspace.conversations_dir / conversation_id

    def _manifest_path(self, conversation_id: str) -> Path:
        return self._dir(conversation_id) / "manifest.json"

    def exists(self, conversation_id: str) -> bool:
        return self._manifest_path(conversation_id).exists()

    def create(self, title: str = "") -> str:
        conversation_id = self._ids.new_id("conv")
        now = self._clock.now_iso()
        atomic_write_json(
            self._manifest_path(conversation_id),
            {
                "conversation_id": conversation_id,
                "created_ts": now,
                "updated_ts": now,
                "title": title,
                "session_refs": {},
            },
        )
        self.set_current(conversation_id)
        return conversation_id

    def set_current(self, conversation_id: str) -> None:
        if not self.exists(conversation_id):
            raise SpecError(f"unknown conversation {conversation_id!r}")
        pointer = self._workspace.conversations_dir / "current"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(conversation_id + "\n", encoding="utf-8")

    def current(self) -> str | None:
        pointer = self._workspace.conversations_dir / "current"
        if not pointer.exists():
            return None
        conversation_id = pointer.read_text(encoding="utf-8").strip()
        return conversation_id if self.exists(conversation_id) else None

    def messages(self, conversation_id: str) -> list[dict[str, object]]:
        if not self.exists(conversation_id):
            raise SpecError(f"unknown conversation {conversation_id!r}")
        return read_jsonl(self._dir(conversation_id) / "messages.jsonl")

    def append_message(
        self, conversation_id: str, role: str, text: str, **extra: object
    ) -> None:
        doc: dict[str, object] = {
            "ts": self._clock.now_iso(),
            "role": role,
            "text": text,
        }
        doc.update(extra)
        append_jsonl(self._dir(conversation_id) / "messages.jsonl", doc)
        manifest = read_json(self._manifest_path(conversation_id))
        manifest["updated_ts"] = self._clock.now_iso()
        if role == "user" and not manifest.get("title"):
            manifest["title"] = text.strip().splitlines()[0][:80] if text.strip() else ""
        atomic_write_json(self._manifest_path(conversation_id), manifest)

    def record_session_ref(
        self, conversation_id: str, runtime: str, session_ref: str
    ) -> None:
        manifest = read_json(self._manifest_path(conversation_id))
        refs = manifest.get("session_refs")
        if not isinstance(refs, dict):
            refs = {}
        refs[runtime] = session_ref
        manifest["session_refs"] = refs
        atomic_write_json(self._manifest_path(conversation_id), manifest)

    def list(self) -> list[ConversationInfo]:
        infos: list[ConversationInfo] = []
        if not self._workspace.conversations_dir.exists():
            return infos
        for path in sorted(self._workspace.conversations_dir.iterdir()):
            manifest_path = path / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = read_json(manifest_path)
            except StorageError:
                continue
            infos.append(
                ConversationInfo(
                    conversation_id=str(manifest.get("conversation_id", path.name)),
                    created_ts=str(manifest.get("created_ts", "")),
                    updated_ts=str(manifest.get("updated_ts", "")),
                    messages=len(read_jsonl(path / "messages.jsonl")),
                    title=str(manifest.get("title", "")),
                )
            )
        return infos


def build_chat_prompt(
    *,
    workspace_root: Path,
    git_sha: str | None,
    history: list[dict[str, object]],
    message: str,
    history_messages: int,
    history_char_budget: int,
) -> str:
    """Compact context assembly — never the whole repository."""
    lines = [
        "You are Dagvane's non-interactive workspace assistant.",
        f"Working repository: {workspace_root}",
    ]
    if git_sha is not None:
        lines.append(f"Current git HEAD: {git_sha}")
    lines += [
        "Use your own repository access for inspection. Follow the user's",
        "instructions exactly; if asked only to analyze, modify nothing.",
        "Answer with the final response only — it is shown to the user as-is.",
        "",
    ]
    tail = history[-history_messages:] if history_messages > 0 else []
    rendered: list[str] = []
    used = 0
    for item in reversed(tail):
        text = str(item.get("text", ""))
        role = str(item.get("role", ""))
        block = f"[{role}]\n{text}"
        if used + len(block) > history_char_budget:
            break
        rendered.append(block)
        used += len(block)
    if rendered:
        lines.append("## Conversation so far (oldest first)")
        lines.extend(reversed(rendered))
        lines.append("")
    lines.append("## Current request")
    lines.append(message)
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ChatOutcome:
    conversation_id: str
    reply: str
    execution_succeeded: bool
    resource_id: str


def run_chat(
    *,
    workspace: Workspace,
    config: WorkspaceConfig,
    store: ConversationStore,
    runner: ExternalAgentRunner,
    resource: ResourceSpec,
    conversation_id: str,
    message: str,
    git_sha: str | None,
    timeout_seconds: int,
) -> ChatOutcome:
    history = store.messages(conversation_id)
    prompt = build_chat_prompt(
        workspace_root=workspace.root,
        git_sha=git_sha,
        history=history,
        message=message,
        history_messages=int(str(config.get("chat.history_messages"))),
        history_char_budget=int(str(config.get("chat.history_char_budget"))),
    )
    invocation = AgentInvocation(
        runtime=resource.runtime,
        prompt=prompt,
        cwd=workspace.root,
        model=resource.model,
        reasoning=resource.reasoning,
        timeout_seconds=timeout_seconds,
        write_access=False,  # chat analyzes; it never writes
        command_template=resource.command_template,
    )
    execution = runner.run(invocation)
    reply = execution.output_text.strip()
    if not execution.succeeded and not reply:
        reply = (
            "[dagvane] the external agent failed "
            f"(exit={execution.exit_code}, timed_out={execution.timed_out}); "
            f"see {execution.log_path}"
        )
    store.append_message(
        conversation_id,
        "user",
        message,
        git_sha=git_sha,
        prompt_path=execution.prompt_path,
    )
    store.append_message(
        conversation_id,
        "assistant",
        reply,
        resource=resource.resource_id,
        output_path=execution.output_path,
        exit_code=execution.exit_code,
        timed_out=execution.timed_out,
        duration_ms=execution.duration_ms,
    )
    if execution.session_ref is not None:
        store.record_session_ref(conversation_id, resource.runtime, execution.session_ref)
    return ChatOutcome(
        conversation_id=conversation_id,
        reply=reply,
        execution_succeeded=execution.succeeded,
        resource_id=resource.resource_id,
    )
