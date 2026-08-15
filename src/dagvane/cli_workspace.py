"""Workspace CLI commands: chat, conversations, config, goal (MVP).

Command model: every invocation starts, performs bounded work, streams
concise progress to stderr, persists durable state, writes the final result
to stdout, and exits. No REPL, no daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dagvane.adapters.agents.subprocess_runner import SubprocessAgentRunner
from dagvane.adapters.localexec import GitOps
from dagvane.application.autodev import GoalRunner, goal_show_doc
from dagvane.application.chat import ConversationStore, run_chat
from dagvane.application.goals import GoalStatus, GoalStore, approve
from dagvane.application.localmodel import probe_local_model
from dagvane.application.prepare import prepare_goal
from dagvane.application.resources import ResourceCatalog
from dagvane.domain.models import SpecError
from dagvane.ports.runtime import SystemClock, SystemIds, SystemMonotonic
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import Workspace


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


class Composition:
    """Per-invocation wiring for workspace commands."""

    def __init__(self, root: Path) -> None:
        self.workspace = Workspace(root)
        self.workspace.ensure()
        self.config = WorkspaceConfig(self.workspace)
        self.clock = SystemClock()
        self.monotonic = SystemMonotonic()
        self.ids = SystemIds()
        self.conversations = ConversationStore(self.workspace, self.clock, self.ids)
        self.goals = GoalStore(self.workspace, self.clock)
        resources_config = self.config.effective.get("resources", {})
        self.catalog = ResourceCatalog(
            resources_config if isinstance(resources_config, dict) else {}
        )
        self.runner = SubprocessAgentRunner(
            runs_dir=self.workspace.agent_runs_dir,
            clock=self.clock,
            monotonic=self.monotonic,
            ids=self.ids,
        )


def add_workspace_parsers(commands: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    chat = commands.add_parser(
        "chat", help="non-interactive workspace chat (stderr=progress, stdout=reply)"
    )
    chat.add_argument("message", help="the message; '-' reads it from stdin")
    chat.add_argument("--new", action="store_true", help="start a new conversation")
    chat.add_argument("--conversation", help="continue a specific conversation id")
    chat.add_argument("--resource", help="override the configured chat resource")

    conversations = commands.add_parser(
        "conversations", help="manage workspace conversations"
    )
    conv_sub = conversations.add_subparsers(dest="conversations_command", required=True)
    conv_sub.add_parser("list", help="list conversations")
    conv_show = conv_sub.add_parser("show", help="print a conversation's messages")
    conv_show.add_argument("conversation_id")
    conv_sub.add_parser("current", help="print the current conversation id")
    conv_use = conv_sub.add_parser("use", help="switch the current conversation")
    conv_use.add_argument("conversation_id")

    config = commands.add_parser("config", help="workspace configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("list", help="print the effective configuration")
    config_get = config_sub.add_parser("get", help="print one config value")
    config_get.add_argument("key")
    config_set = config_sub.add_parser("set", help="set one workspace config value")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_sub.add_parser("edit", help="open .dagvane/config.toml in $EDITOR")

    goal = commands.add_parser("goal", help="durable development goals")
    goal_sub = goal.add_subparsers(dest="goal_command", required=True)
    goal_prepare = goal_sub.add_parser(
        "prepare", help="derive a goal contract from a conversation"
    )
    goal_prepare.add_argument("--name", required=True)
    goal_prepare.add_argument(
        "--from-conversation",
        default="current",
        help="conversation id, or 'current' (default)",
    )
    for command_name in ("show", "approve", "run", "resume", "cancel"):
        sub = goal_sub.add_parser(command_name)
        sub.add_argument("name")
    goal_sub.add_parser("list", help="list goals")


def _resolve_conversation(comp: Composition, args: argparse.Namespace) -> str:
    if getattr(args, "conversation", None):
        conversation_id = str(args.conversation)
        if not comp.conversations.exists(conversation_id):
            raise SpecError(f"unknown conversation {conversation_id!r}")
        comp.conversations.set_current(conversation_id)
        return conversation_id
    if getattr(args, "new", False):
        return comp.conversations.create()
    current = comp.conversations.current()
    if current is None:
        return comp.conversations.create()
    return current


def cmd_chat(args: argparse.Namespace) -> int:
    comp = Composition(Path.cwd())
    message = args.message
    if message == "-":
        message = sys.stdin.read()
    if not message.strip():
        raise SpecError("empty chat message")
    conversation_id = _resolve_conversation(comp, args)
    resource_id = args.resource or str(comp.config.get("chat.resource"))
    resource = comp.catalog.get(resource_id)
    git_sha = (
        GitOps.head_sha(comp.workspace.root)
        if GitOps.is_repo(comp.workspace.root)
        else None
    )
    _progress(f"conversation {conversation_id}")
    _progress(
        f"agent      {resource.resource_id} "
        f"({resource.model or resource.runtime}/{resource.reasoning or '-'})"
    )
    outcome = run_chat(
        workspace=comp.workspace,
        config=comp.config,
        store=comp.conversations,
        runner=comp.runner,
        resource=resource,
        conversation_id=conversation_id,
        message=message,
        git_sha=git_sha,
        timeout_seconds=int(str(comp.config.get("chat.timeout_seconds"))),
    )
    print(outcome.reply)
    return 0 if outcome.execution_succeeded else 10


def cmd_conversations(args: argparse.Namespace) -> int:
    comp = Composition(Path.cwd())
    command = args.conversations_command
    if command == "list":
        current = comp.conversations.current()
        for info in comp.conversations.list():
            marker = "*" if info.conversation_id == current else " "
            print(
                f"{marker} {info.conversation_id}  {info.messages:>3} msgs  "
                f"{info.updated_ts}  {info.title}"
            )
        return 0
    if command == "show":
        for item in comp.conversations.messages(args.conversation_id):
            print(f"--- [{item.get('role')}] {item.get('ts')}")
            print(str(item.get("text", "")))
        return 0
    if command == "current":
        current = comp.conversations.current()
        if current is None:
            raise SpecError("no current conversation")
        print(current)
        return 0
    if command == "use":
        comp.conversations.set_current(args.conversation_id)
        print(args.conversation_id)
        return 0
    raise AssertionError(f"unhandled conversations command {command!r}")


def cmd_config(args: argparse.Namespace) -> int:
    comp = Composition(Path.cwd())
    command = args.config_command
    if command == "list":
        for key, value in sorted(comp.config.flat().items()):
            print(f"{key} = {json.dumps(value)}")
        return 0
    if command == "get":
        value = comp.config.get(args.key)
        if isinstance(value, dict):
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            print(json.dumps(value))
        return 0
    if command == "set":
        comp.config.set(args.key, args.value)
        print(f"{args.key} = {json.dumps(comp.config.get(args.key))}")
        return 0
    if command == "edit":
        comp.config.save()  # materialize the file with current workspace values
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        os.execvp(editor, [editor, str(comp.workspace.config_path)])
        raise AssertionError("unreachable")  # pragma: no cover
    raise AssertionError(f"unhandled config command {command!r}")


def cmd_goal(args: argparse.Namespace) -> int:
    comp = Composition(Path.cwd())
    command = args.goal_command
    if command == "prepare":
        conversation_id = args.from_conversation
        if conversation_id == "current":
            current = comp.conversations.current()
            if current is None:
                raise SpecError("no current conversation to prepare from")
            conversation_id = current
        record = prepare_goal(
            workspace=comp.workspace,
            config=comp.config,
            conversations=comp.conversations,
            goals=comp.goals,
            catalog=comp.catalog,
            runner=comp.runner,
            clock=comp.clock,
            monotonic=comp.monotonic,
            name=args.name,
            conversation_id=conversation_id,
            progress=_progress,
        )
        print(json.dumps(goal_show_doc(record, None), indent=2, sort_keys=True))
        _progress(
            f"prepared   goal {args.name}; review with `dagvane goal show "
            f"{args.name}`, then `dagvane goal approve {args.name}`"
        )
        return 0
    if command == "list":
        for name in comp.goals.list_names():
            record = comp.goals.load(name)
            print(f"{name}  {record.status.value}  {record.contract.objective[:70]}")
        return 0

    name = args.name
    if command == "show":
        record = comp.goals.load(name)
        runner = _goal_runner(comp)
        state = runner.load_state(name)
        print(json.dumps(goal_show_doc(record, state), indent=2, sort_keys=True))
        return 0
    if command == "approve":
        record = comp.goals.load(name)
        approve(record)
        comp.goals.save(record)
        comp.goals.log_event(name, {"event": "goal.approved"})
        print(f"goal {name} approved (contract {record.contract_sha256})")
        return 0
    if command == "cancel":
        record = comp.goals.load(name)
        record.status = GoalStatus.CANCELLED
        comp.goals.save(record)
        comp.goals.log_event(name, {"event": "goal.cancelled"})
        print(f"goal {name} cancelled")
        return 0
    if command in ("run", "resume"):
        runner = _goal_runner(comp)
        if bool(comp.config.get("router.local_enabled")):
            available = probe_local_model(comp.catalog)
            _progress(f"local      ollama {'available' if available else 'unavailable'}")
        status = (
            runner.start(name) if command == "run" else runner.resume(name)
        )
        print(status.value)
        return 0 if status is GoalStatus.ACHIEVED else 10
    raise AssertionError(f"unhandled goal command {command!r}")


def _goal_runner(comp: Composition) -> GoalRunner:
    return GoalRunner(
        workspace=comp.workspace,
        config=comp.config,
        store=comp.goals,
        catalog=comp.catalog,
        runner=comp.runner,
        clock=comp.clock,
        monotonic=comp.monotonic,
        ids=comp.ids,
        progress=_progress,
    )
