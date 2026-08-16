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
from dagvane.domain.identifiers import CONVERSATION_ID_RESERVED, validate_conversation_id
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, IdSource
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import (
    Workspace,
    append_jsonl,
    atomic_write_bytes,
    atomic_write_json,
    ensure_expected_descendant,
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
    """Durable workspace-owned conversations.

    Every public entry point validates ``conversation_id`` with
    ``validate_conversation_id`` first, then binds the strict expected
    descendant of the trusted ``conversations_dir`` root, and loaders/
    mutators additionally require the durable manifest's own
    ``conversation_id`` to equal the requested/directory identity exactly —
    a missing, malformed, symlinked or mismatched Conversation fails closed
    rather than being reported as absent or silently adopted.
    """

    def __init__(self, workspace: Workspace, clock: Clock, ids: IdSource) -> None:
        self._workspace = workspace
        self._clock = clock
        self._ids = ids

    def _dir(self, conversation_id: str) -> Path:
        validated = validate_conversation_id(conversation_id, ctx="conversation id")
        path = self._workspace.conversations_dir / validated
        ensure_expected_descendant(self._workspace.conversations_dir, path)
        return path

    def _manifest_path(self, conversation_id: str) -> Path:
        path = self._dir(conversation_id) / "manifest.json"
        ensure_expected_descendant(self._workspace.conversations_dir, path)
        return path

    def _messages_path(self, conversation_id: str) -> Path:
        path = self._dir(conversation_id) / "messages.jsonl"
        ensure_expected_descendant(self._workspace.conversations_dir, path)
        return path

    def _pointer_path(self) -> Path:
        path = self._workspace.conversations_dir / CONVERSATION_ID_RESERVED
        ensure_expected_descendant(self._workspace.conversations_dir, path)
        return path

    def _assert_durable_identity(self, conversation_id: str) -> dict[str, object]:
        """Read the durable manifest and require its own ``conversation_id``
        to equal the requested/directory identity exactly (strict string
        comparison). Fails closed on missing, non-string or mismatched
        identity."""
        manifest = read_json(
            self._manifest_path(conversation_id),
            allowed_root=self._workspace.conversations_dir,
        )
        manifest_id = manifest.get("conversation_id")
        if not isinstance(manifest_id, str) or manifest_id != conversation_id:
            raise SpecError(
                f"conversation {conversation_id!r}: durable manifest identity "
                f"{manifest_id!r} does not match the requested/directory identity"
            )
        return manifest

    def _load_and_bind(self, conversation_id: str) -> dict[str, object]:
        """Require the durable manifest to exist and be identity-bound before
        any caller reads or mutates conversation bytes. A bare Conversation
        directory with no ``manifest.json`` is existing malformed storage,
        not absence, so it raises rather than reporting "unknown"."""
        manifest_path = self._manifest_path(conversation_id)
        if manifest_path.is_symlink():
            raise StorageError(f"{manifest_path}: refusing to follow a symlink")
        if not manifest_path.exists():
            conversation_dir = self._dir(conversation_id)
            if conversation_dir.exists():
                raise StorageError(
                    f"{conversation_dir}: conversation directory is missing manifest.json"
                )
            raise SpecError(f"unknown conversation {conversation_id!r}")
        return self._assert_durable_identity(conversation_id)

    def exists(self, conversation_id: str) -> bool:
        """``False`` only for a genuinely absent valid Conversation; malformed,
        symlinked or identity-mismatched storage raises instead."""
        validated_id = validate_conversation_id(conversation_id, ctx="conversation exists: id")
        manifest_path = self._manifest_path(validated_id)
        if manifest_path.is_symlink():
            raise StorageError(f"{manifest_path}: refusing to follow a symlink")
        if not manifest_path.exists():
            conversation_dir = self._dir(validated_id)
            if conversation_dir.exists():
                raise StorageError(
                    f"{conversation_dir}: conversation directory is missing manifest.json"
                )
            return False
        self._assert_durable_identity(validated_id)
        return True

    def create(self, title: str = "") -> str:
        conversation_id = self._ids.new_id("conv")
        # Validate the generated id before any path/clock/durable effect: a
        # broken IdSource must not create a manifest or move the pointer.
        validated_id = validate_conversation_id(
            conversation_id, ctx="conversation create: generated id"
        )
        conversation_dir = self._dir(validated_id)
        if conversation_dir.exists():
            raise StorageError(
                f"conversation {validated_id!r} already exists; refusing to "
                "overwrite or adopt pre-existing storage"
            )
        # Preflight the fixed `current` pointer before any clock advancement
        # or new Conversation bytes: a symlinked/directory/corrupt pointer
        # must refuse creation rather than leave an orphan manifest that a
        # later create silently "repairs" by overwriting the pointer.
        self.current()
        now = self._clock.now_iso()
        atomic_write_json(
            self._manifest_path(validated_id),
            {
                "conversation_id": validated_id,
                "created_ts": now,
                "updated_ts": now,
                "title": title,
                "session_refs": {},
            },
            allowed_root=self._workspace.conversations_dir,
        )
        self.set_current(validated_id)
        return validated_id

    def set_current(self, conversation_id: str) -> None:
        """Fully validate and load/bind the target before atomically
        replacing the guarded pointer; on failure the old pointer is
        byte-identical (validation/binding never touches the pointer file)."""
        validated_id = validate_conversation_id(
            conversation_id, ctx="conversation set_current: id"
        )
        self._load_and_bind(validated_id)
        pointer_path = self._pointer_path()
        if pointer_path.is_symlink():
            raise StorageError(f"{pointer_path}: refusing to follow a symlink")
        atomic_write_bytes(
            pointer_path,
            f"{validated_id}\n".encode(),
            allowed_root=self._workspace.conversations_dir,
        )

    def current(self) -> str | None:
        """``None`` only when the pointer is genuinely absent. Empty,
        multiline, whitespace-padded, invalid, unknown or manifest-mismatched
        content raises rather than being ``.strip()``-ed into a different id."""
        pointer_path = self._pointer_path()
        if pointer_path.is_symlink():
            raise StorageError(f"{pointer_path}: refusing to follow a symlink")
        if not pointer_path.exists():
            return None
        if not pointer_path.is_file():
            raise StorageError(f"{pointer_path}: current pointer must be a regular file")
        raw = pointer_path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageError(f"{pointer_path}: corrupt pointer bytes: {exc}") from exc
        if not text.endswith("\n") or text.count("\n") != 1:
            raise SpecError(f"{pointer_path}: corrupt pointer content {text!r}")
        candidate = text[:-1]
        validated_id = validate_conversation_id(
            candidate, ctx="conversation current: pointer content"
        )
        self._load_and_bind(validated_id)
        return validated_id

    def messages(self, conversation_id: str) -> list[dict[str, object]]:
        validated_id = validate_conversation_id(conversation_id, ctx="conversation messages: id")
        self._load_and_bind(validated_id)
        return read_jsonl(
            self._messages_path(validated_id), allowed_root=self._workspace.conversations_dir
        )

    def append_message(
        self, conversation_id: str, role: str, text: str, **extra: object
    ) -> None:
        # Load and bind first: invalid/unknown storage must not advance the
        # clock, append transcript bytes, or rewrite the manifest.
        validated_id = validate_conversation_id(
            conversation_id, ctx="conversation append_message: id"
        )
        manifest = self._load_and_bind(validated_id)
        # Validate the existing transcript strictly before the first clock
        # call or write: a symlinked or corrupt messages.jsonl must refuse
        # the append rather than advance the clock/manifest first.
        read_jsonl(
            self._messages_path(validated_id), allowed_root=self._workspace.conversations_dir
        )
        doc: dict[str, object] = {
            "ts": self._clock.now_iso(),
            "role": role,
            "text": text,
        }
        doc.update(extra)
        append_jsonl(
            self._messages_path(validated_id), doc, allowed_root=self._workspace.conversations_dir
        )
        manifest["updated_ts"] = self._clock.now_iso()
        if role == "user" and not manifest.get("title"):
            manifest["title"] = text.strip().splitlines()[0][:80] if text.strip() else ""
        atomic_write_json(
            self._manifest_path(validated_id),
            manifest,
            allowed_root=self._workspace.conversations_dir,
        )

    def record_session_ref(
        self, conversation_id: str, runtime: str, session_ref: str
    ) -> None:
        validated_id = validate_conversation_id(
            conversation_id, ctx="conversation record_session_ref: id"
        )
        manifest = self._load_and_bind(validated_id)
        refs = manifest.get("session_refs")
        if not isinstance(refs, dict):
            refs = {}
        refs[runtime] = session_ref
        manifest["session_refs"] = refs
        atomic_write_json(
            self._manifest_path(validated_id),
            manifest,
            allowed_root=self._workspace.conversations_dir,
        )

    def list(self) -> list[ConversationInfo]:
        """Enumerate durable conversations, failing closed on anything that
        is not ordinary absence: the reserved ``current`` pointer entry is
        skipped, but a symlinked, invalidly-named or corrupt candidate
        Conversation directory fails the whole operation rather than being
        silently omitted."""
        conversations_dir = self._workspace.conversations_dir
        if conversations_dir.is_symlink():
            raise StorageError(
                f"{conversations_dir}: symlink is not an allowed conversations root"
            )
        if not conversations_dir.exists():
            return []
        if not conversations_dir.is_dir():
            raise StorageError(f"{conversations_dir}: conversations root must be a directory")
        infos: list[ConversationInfo] = []
        for path in sorted(conversations_dir.iterdir()):
            if path.name == CONVERSATION_ID_RESERVED:
                if path.is_symlink():
                    raise StorageError(f"{path}: symlink is not an allowed pointer entry")
                if not path.is_file():
                    raise StorageError(
                        f"{path}: reserved {CONVERSATION_ID_RESERVED!r} entry must be a "
                        "regular file"
                    )
                continue
            if path.is_symlink():
                raise StorageError(f"{path}: symlink is not an allowed conversation entry")
            if not path.is_dir():
                raise StorageError(
                    f"{path}: conversations root must contain only conversation directories"
                )
            name = validate_conversation_id(path.name, ctx="conversation list: id")
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink():
                raise StorageError(f"{manifest_path}: symlink is not an allowed manifest file")
            if not manifest_path.exists():
                raise StorageError(f"{path}: conversation directory is missing manifest.json")
            manifest = self._load_and_bind(name)
            infos.append(
                ConversationInfo(
                    conversation_id=name,
                    created_ts=str(manifest.get("created_ts", "")),
                    updated_ts=str(manifest.get("updated_ts", "")),
                    messages=len(
                        read_jsonl(self._messages_path(name), allowed_root=conversations_dir)
                    ),
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
        env_passthrough=resource.env_passthrough,
        secret_env=resource.secret_env,
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
