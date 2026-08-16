"""Security regressions: Conversation identity binding must be enforced
before any Git/runner/routing effect in `run_chat`, `prepare_goal`, and the
workspace CLI.

Offline only: no real model/network. External agents/Git inspection are
either never reachable (asserted via spies) or a trivial local ``command``
runtime.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dagvane.adapters.localexec import GitOps
from dagvane.application.chat import ConversationStore, run_chat
from dagvane.application.goals import GoalStore
from dagvane.application.prepare import prepare_goal
from dagvane.application.resources import ResourceCatalog, ResourceSpec
from dagvane.cli import main
from dagvane.cli_workspace import Composition
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.ports.runtime import FixedClock, SequentialIds
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import Workspace


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "t@example.test"], root)
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)
    workspace = Workspace(root)
    workspace.ensure()
    return workspace


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


class _NeverCalledRunner:
    """An ExternalAgentRunner double that fails the test if ever invoked."""

    def run(self, invocation: AgentInvocation) -> AgentExecution:  # pragma: no cover
        raise AssertionError(f"external agent unexpectedly invoked: {invocation}")


def _resource() -> ResourceSpec:
    return ResourceSpec(
        resource_id="fake-agent",
        kind="external_agent",
        runtime="command",
        tier="STANDARD",
    )


def _tamper_manifest_identity(workspace: Workspace, conversation_id: str) -> bytes:
    manifest_path = workspace.conversations_dir / conversation_id / "manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["conversation_id"] = "conv-other"
    tampered = json.dumps(doc)
    manifest_path.write_text(tampered, encoding="utf-8")
    return tampered.encode("utf-8")


def _symlink_manifest(workspace: Workspace, conversation_id: str, tmp_path: Path) -> None:
    manifest_path = workspace.conversations_dir / conversation_id / "manifest.json"
    manifest_path.unlink()
    target = tmp_path / "outside-manifest.json"
    target.write_bytes(b'{"conversation_id": "conv-a"}')
    manifest_path.symlink_to(target)


# =============================================================================
# run_chat: unknown/tampered/symlinked Conversation before prompt/runner
# =============================================================================


def _run_chat_with(
    workspace: Workspace, clock: FixedClock, conversation_id: str
) -> None:
    store = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    run_chat(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        store=store,
        runner=_NeverCalledRunner(),
        resource=_resource(),
        conversation_id=conversation_id,
        message="hello",
        git_sha=None,
        timeout_seconds=30,
    )


def test_run_chat_unknown_conversation_fails_before_runner(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    with pytest.raises(SpecError):
        _run_chat_with(workspace, clock, "conv-ghost")
    assert not (workspace.conversations_dir / "conv-ghost").exists()


def test_run_chat_tampered_conversation_fails_before_runner(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    conversation_id = store.create()
    tampered_bytes = _tamper_manifest_identity(workspace, conversation_id)

    with pytest.raises(SpecError):
        _run_chat_with(workspace, clock, conversation_id)

    manifest_path = workspace.conversations_dir / conversation_id / "manifest.json"
    assert manifest_path.read_bytes() == tampered_bytes
    messages_path = workspace.conversations_dir / conversation_id / "messages.jsonl"
    assert not messages_path.exists()


def test_run_chat_symlinked_manifest_fails_before_runner(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    conversation_id = store.create()
    _symlink_manifest(workspace, conversation_id, tmp_path)

    with pytest.raises(StorageError):
        _run_chat_with(workspace, clock, conversation_id)

    messages_path = workspace.conversations_dir / conversation_id / "messages.jsonl"
    assert not messages_path.exists()


def test_run_chat_corrupt_transcript_fails_before_runner(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    conversation_id = store.create()
    messages_path = workspace.conversations_dir / conversation_id / "messages.jsonl"
    messages_path.write_bytes(b"not json\n")

    with pytest.raises(StorageError):
        _run_chat_with(workspace, clock, conversation_id)

    assert messages_path.read_bytes() == b"not json\n"


# =============================================================================
# prepare_goal: unknown/tampered Conversation before Git inspection/routing/
# agent invocation
# =============================================================================


def _prepare_goal_with(
    workspace: Workspace,
    clock: FixedClock,
    conversations: ConversationStore,
    conversation_id: str,
    progress: list[str],
) -> None:
    prepare_goal(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        conversations=conversations,
        goals=GoalStore(workspace, clock),
        catalog=ResourceCatalog({}),
        runner=_NeverCalledRunner(),
        clock=clock,
        name="goal-a",
        conversation_id=conversation_id,
        progress=progress.append,
    )


def test_prepare_goal_unknown_conversation_fails_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    conversations = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    progress: list[str] = []

    def _poisoned_is_repo(cwd: Path) -> bool:  # pragma: no cover
        raise AssertionError("GitOps.is_repo unexpectedly called")

    monkeypatch.setattr(GitOps, "is_repo", staticmethod(_poisoned_is_repo))

    with pytest.raises(SpecError):
        _prepare_goal_with(workspace, clock, conversations, "conv-ghost", progress)

    assert progress == []
    assert not (workspace.goals_dir / "goal-a").exists()


def test_prepare_goal_tampered_conversation_fails_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    conversations = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    conversation_id = conversations.create()
    conversations.append_message(conversation_id, "user", "please do the thing")
    tampered_bytes = _tamper_manifest_identity(workspace, conversation_id)
    progress: list[str] = []

    def _poisoned_is_repo(cwd: Path) -> bool:  # pragma: no cover
        raise AssertionError("GitOps.is_repo unexpectedly called")

    monkeypatch.setattr(GitOps, "is_repo", staticmethod(_poisoned_is_repo))

    with pytest.raises(SpecError):
        _prepare_goal_with(workspace, clock, conversations, conversation_id, progress)

    assert progress == []
    assert not (workspace.goals_dir / "goal-a").exists()
    manifest_path = workspace.conversations_dir / conversation_id / "manifest.json"
    assert manifest_path.read_bytes() == tampered_bytes


def test_prepare_goal_symlinked_conversation_fails_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    conversations = ConversationStore(workspace, clock, SequentialIds(seed="t"))
    conversation_id = conversations.create()
    conversations.append_message(conversation_id, "user", "please do the thing")
    _symlink_manifest(workspace, conversation_id, tmp_path)
    progress: list[str] = []

    def _poisoned_is_repo(cwd: Path) -> bool:  # pragma: no cover
        raise AssertionError("GitOps.is_repo unexpectedly called")

    monkeypatch.setattr(GitOps, "is_repo", staticmethod(_poisoned_is_repo))

    with pytest.raises(StorageError):
        _prepare_goal_with(workspace, clock, conversations, conversation_id, progress)

    assert progress == []
    assert not (workspace.goals_dir / "goal-a").exists()


# =============================================================================
# CLI: unknown/tampered conversation is a controlled nonzero error, no
# misleading stdout
# =============================================================================


def test_cli_chat_unknown_conversation_is_controlled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    code = main(["chat", "hello", "--conversation", "conv-ghost"])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "conv-ghost" in captured.err
    comp = Composition(root)
    assert comp.conversations.list() == []


def test_cli_conversations_show_tampered_conversation_is_controlled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    comp = Composition(root)
    conversation_id = comp.conversations.create()
    tampered_bytes = _tamper_manifest_identity(comp.workspace, conversation_id)
    capsys.readouterr()

    code = main(["conversations", "show", conversation_id])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    manifest_path = comp.workspace.conversations_dir / conversation_id / "manifest.json"
    assert manifest_path.read_bytes() == tampered_bytes


def test_cli_chat_empty_conversation_option_is_controlled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real argparse parse of ``--conversation ''`` must not be coerced by
    truthiness/``str()`` into "option absent": it is a distinct invalid,
    explicit id and must fail before creating a Conversation or invoking the
    external agent."""
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)

    def _poisoned_run(self: object, invocation: AgentInvocation) -> AgentExecution:
        raise AssertionError("external agent unexpectedly invoked")

    monkeypatch.setattr(
        "dagvane.adapters.agents.subprocess_runner.SubprocessAgentRunner.run",
        _poisoned_run,
    )

    code = main(["chat", "hello", "--conversation", ""])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.out == ""

    comp = Composition(root)
    assert comp.conversations.list() == []
    assert comp.conversations.current() is None


def test_resolve_conversation_rejects_non_string_programmatic_value(
    tmp_path: Path,
) -> None:
    """A direct ``Namespace``/programmatic non-string ``conversation`` value
    (e.g. an integer) must raise ``SpecError`` without ``str()`` coercion or
    any durable/current-pointer effect."""
    from argparse import Namespace

    from dagvane.cli_workspace import _resolve_conversation

    root = tmp_path / "project"
    root.mkdir()
    comp = Composition(root)
    with pytest.raises(SpecError):
        _resolve_conversation(comp, Namespace(conversation=123, new=False))
    assert comp.conversations.list() == []
    assert comp.conversations.current() is None


def test_cli_chat_valid_explicit_conversation_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    comp = Composition(root)
    conversation_id = comp.conversations.create()
    capsys.readouterr()

    def _fake_run(self: object, invocation: AgentInvocation) -> AgentExecution:
        return AgentExecution(
            runtime=invocation.runtime,
            model=invocation.model,
            reasoning=invocation.reasoning,
            cwd=str(invocation.cwd),
            started_ts="2026-08-16T00:00:00.000Z",
            finished_ts="2026-08-16T00:00:01.000Z",
            duration_ms=1,
            exit_code=0,
            timed_out=False,
            output_text="hi there",
            prompt_path="/dev/null",
            output_path="/dev/null",
            log_path="/dev/null",
            session_ref=None,
        )

    monkeypatch.setattr(
        "dagvane.adapters.agents.subprocess_runner.SubprocessAgentRunner.run",
        _fake_run,
    )
    monkeypatch.setattr(
        "dagvane.application.localmodel.probe_local_model", lambda catalog: False
    )

    code = main(["chat", "hello", "--conversation", conversation_id])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "hi there"
    assert comp.conversations.current() == conversation_id


def test_cli_conversations_use_unknown_id_preserves_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.chdir(root)
    comp = Composition(root)
    conversation_id = comp.conversations.create()
    pointer_path = comp.workspace.conversations_dir / "current"
    before = pointer_path.read_bytes()
    capsys.readouterr()

    code = main(["conversations", "use", "conv-ghost"])
    assert code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert pointer_path.read_bytes() == before
    assert comp.conversations.current() == conversation_id
