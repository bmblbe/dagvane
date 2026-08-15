"""Dagvane CLI: argparse surface, output sinks, exit-code mapping.

This is the composition root: it wires concrete adapters (filesystem store,
fake or live backends) into the application layer. Credential values are read
from the environment here and travel only into adapter constructors — never
into documents, events, or errors. Stdout carries only command output (NDJSON
frames, canonical JSON documents, or rendered text); every diagnostic goes to
stderr.

Exit codes (G0/G1 subset of the Round 4 table): 0 completed · 2 usage/input
error · 10 run finished failed · 40 internal error.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from dagvane import __version__
from dagvane.adapters.backends.anthropic import AnthropicBackend
from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.backends.openai_compat import OpenAICompatBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import (
    FrameSink,
    plan_council_doc,
    run_council,
    run_council_live,
)
from dagvane.application.replay import derived_status_doc, fold_frames
from dagvane.cli_workspace import (
    add_workspace_parsers,
    cmd_chat,
    cmd_config,
    cmd_conversations,
    cmd_goal,
)
from dagvane.domain.models import (
    DagvaneError,
    EventEnvelope,
    PlanValidationError,
    RunStatus,
    SpecError,
)
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.backend import ChatBackend
from dagvane.protocol.documents import load_fixture_file, load_task_file
from dagvane.protocol.frames import canonical_json_bytes
from dagvane.protocol.profiles import (
    BACKEND_KIND_ANTHROPIC,
    ProfileSpec,
    load_profile_file,
)

EXIT_COMPLETED = 0
EXIT_USAGE = 2
EXIT_RUN_FAILED = 10
EXIT_INTERNAL = 40


def _diag(message: str) -> None:
    print(f"dagvane: {message}", file=sys.stderr)


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dagvane",
        description=(
            "Dagvane engine: deterministic fixture councils and "
            "live multi-provider councils."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"dagvane {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan", help="build and print a plan without executing")
    plan_templates = plan_parser.add_subparsers(dest="template", required=True)
    plan_council = plan_templates.add_parser("council", help="the fixed council-v1 template")
    plan_council.add_argument("task_file", type=Path)
    plan_council.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan without executing (plan never executes)",
    )
    plan_council.add_argument("--output", choices=["json"], default="json")

    council = commands.add_parser("council", help="execute a council run")
    council.add_argument("task_file", type=Path)
    council_source = council.add_mutually_exclusive_group(required=True)
    council_source.add_argument(
        "--fixture",
        type=Path,
        help="fixture file driving the deterministic fake backend",
    )
    council_source.add_argument(
        "--profile",
        type=Path,
        help="TOML live-council profile (connections, routes, role mapping)",
    )
    council.add_argument("--output", choices=["text", "json", "ndjson"], default="text")

    runs = commands.add_parser("runs", help="inspect durable runs")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_show = runs_sub.add_parser("show", help="print a run's manifest and derived status")
    runs_show.add_argument("run_id")
    runs_show.add_argument("--output", choices=["json"], default="json")

    events = commands.add_parser("events", help="emit a run's canonical event frames")
    events.add_argument("run_id")
    events.add_argument(
        "--since",
        type=_nonnegative_int,
        default=0,
        help="emit only frames with seq greater than this value",
    )
    events.add_argument("--output", choices=["ndjson"], default="ndjson")

    add_workspace_parsers(commands)

    return parser


def _render_text(envelope: EventEnvelope) -> str:
    data = envelope.data
    event_type = envelope.type
    if event_type == "run.created":
        return f"run {envelope.run_id}: created ({data['node_count']} nodes)"
    if event_type == "node.started":
        return f"node {envelope.node_id}: started ({data['role']} via {data['route_id']})"
    if event_type == "artifact.written":
        sha = str(data["sha256"])
        return (
            f"node {envelope.node_id}: artifact {data['role']} "
            f"{sha[:12]} ({data['size']} bytes)"
        )
    if event_type == "model.dispatched":
        return f"node {envelope.node_id}: dispatched {data['model']} ({envelope.call_id})"
    if event_type == "model.completed":
        return (
            f"node {envelope.node_id}: model completed (in={data['input_tokens']} "
            f"out={data['output_tokens']} tokens, {data['cost_microusd']} microUSD)"
        )
    if event_type == "node.completed":
        return f"node {envelope.node_id}: completed"
    if event_type == "node.failed":
        return f"node {envelope.node_id}: FAILED [{data['reason']}] {data['message']}"
    if event_type == "budget.rejected":
        return (
            f"node {envelope.node_id}: budget rejected on {data['dimension']} "
            f"(requested {data['requested']}, used {data['used']}, cap {data['cap']})"
        )
    if event_type == "decision.recorded":
        return f"decision: winner {data['winner']}"
    if event_type == "run.finished":
        reason = data["reason"]
        suffix = f" ({reason})" if reason else ""
        return f"run {envelope.run_id}: {data['status']}{suffix}"
    return f"event {event_type}"  # display consumers must ignore unknown types


def _write_stdout_bytes(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def _cmd_plan(args: argparse.Namespace) -> int:
    task = load_task_file(args.task_file)
    _write_stdout_bytes(canonical_json_bytes(plan_council_doc(task)))
    return EXIT_COMPLETED


def _build_live_backends(
    profile: ProfileSpec,
) -> tuple[dict[str, ChatBackend], SecretScrubber]:
    """Construct one adapter per connection the council actually uses.

    Credential values are read from the environment by *name*, registered
    ephemerally in one shared ``SecretScrubber`` (so every adapter scrubs
    every configured credential — cross-provider reflections included), and
    handed only to adapter constructors. ``ensure_ready`` surfaces a missing
    optional dependency as a usage error before any run state exists.
    """
    backends: dict[str, ChatBackend] = {}
    scrubber = SecretScrubber()
    for connection_id, connection in sorted(profile.used_connections().items()):
        value = os.environ.get(connection.credential_env)
        if not value:
            raise SpecError(
                f"connection {connection_id!r} requires the credential environment "
                f"variable {connection.credential_env!r}, which is not set"
            )
        # A credential with whitespace or non-printable bytes cannot form a
        # legal HTTP header; refuse it up front (never echoing the value).
        # Everything in the admitted printable domain — quotes, backslashes,
        # JSON-escapable characters — is covered by the shared scrubber.
        if not all(0x21 <= ord(ch) <= 0x7E for ch in value):
            raise SpecError(
                f"the value of {connection.credential_env!r} contains whitespace "
                "or non-printable characters and cannot be used as an HTTP "
                "credential"
            )
        scrubber.register(value)
        if connection.kind == BACKEND_KIND_ANTHROPIC:
            anthropic_backend = AnthropicBackend(
                connection_id=connection_id,
                api_key=value,
                timeout_seconds=connection.timeout_seconds,
                base_url=connection.base_url,
                scrubber=scrubber,
            )
            anthropic_backend.ensure_ready()
            backends[connection_id] = anthropic_backend
        else:
            base_url = connection.base_url
            assert base_url is not None  # enforced by profile validation
            compat_backend = OpenAICompatBackend(
                connection_id=connection_id,
                base_url=base_url,
                api_key=value,
                timeout_seconds=connection.timeout_seconds,
                max_tokens_field=connection.max_tokens_field,
                scrubber=scrubber,
            )
            compat_backend.ensure_ready()
            backends[connection_id] = compat_backend
    return backends, scrubber


def _cmd_council(args: argparse.Namespace) -> int:
    task = load_task_file(args.task_file)
    store = FilesystemRunStore(Path.cwd())

    sink: FrameSink | None
    if args.output == "ndjson":

        def sink(line: bytes, envelope: EventEnvelope) -> None:
            _write_stdout_bytes(line)

    elif args.output == "text":

        def sink(line: bytes, envelope: EventEnvelope) -> None:
            print(_render_text(envelope))

    else:
        sink = None

    if args.fixture is not None:
        fixture = load_fixture_file(args.fixture)
        backend = FakeBackend(fixture.responses)
        result = run_council(
            task=task, fixture=fixture, store=store, backend=backend, sink=sink
        )
    else:
        profile = load_profile_file(args.profile)
        backends, scrubber = _build_live_backends(profile)
        result = run_council_live(
            task=task,
            profile=profile,
            backends=backends,
            store=store,
            sink=sink,
            scrubber=scrubber,
        )
    if result.sink_error is not None:
        _diag(
            "output stream failed mid-run; the journal is authoritative: "
            f"{result.sink_error}"
        )
    _diag(
        f"run {result.run_id}: {result.status.value} "
        f"({store.run_dir(result.run_id)})"
    )
    if args.output == "json":
        _write_stdout_bytes(canonical_json_bytes(result.report_doc))
    return EXIT_COMPLETED if result.status is RunStatus.COMPLETED else EXIT_RUN_FAILED


def _cmd_runs_show(args: argparse.Namespace) -> int:
    store = FilesystemRunStore(Path.cwd())
    if not store.run_exists(args.run_id):
        raise SpecError(f"unknown run {args.run_id!r}")
    manifest = store.read_manifest(args.run_id)
    view = fold_frames(store.iter_frames(args.run_id), require_terminal=False)
    doc = {"manifest": manifest, "derived": derived_status_doc(view)}
    _write_stdout_bytes(canonical_json_bytes(doc))
    return EXIT_COMPLETED


def _cmd_events(args: argparse.Namespace) -> int:
    store = FilesystemRunStore(Path.cwd())
    if not store.run_exists(args.run_id):
        raise SpecError(f"unknown run {args.run_id!r}")
    for line in store.iter_frames(args.run_id, since=args.since):
        sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()
    return EXIT_COMPLETED


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _cmd_plan(args)
        if args.command == "council":
            return _cmd_council(args)
        if args.command == "runs":
            return _cmd_runs_show(args)
        if args.command == "events":
            return _cmd_events(args)
        if args.command == "chat":
            return cmd_chat(args)
        if args.command == "conversations":
            return cmd_conversations(args)
        if args.command == "config":
            return cmd_config(args)
        if args.command == "goal":
            return cmd_goal(args)
        raise AssertionError(f"unhandled command {args.command!r}")
    except (SpecError, PlanValidationError) as exc:
        _diag(f"error: {exc}")
        return EXIT_USAGE
    except DagvaneError as exc:
        _diag(f"internal error: {exc}")
        return EXIT_INTERNAL
    except Exception:  # noqa: BLE001 — last-resort mapping to the internal exit code
        traceback.print_exc(file=sys.stderr)
        return EXIT_INTERNAL
