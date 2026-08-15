"""Chat → Goal Contract preparation (Autonomous Developer MVP).

``goal prepare --from-conversation`` turns accepted conversation state plus
actual repository inspection into an owner-reviewable frozen contract draft:
objective, must-have scope, explicit non-goals, objective acceptance checks,
verification gates, and baseline evidence at the exact base SHA. The owner
never writes YAML — they review ``goal show`` output and approve.
"""

from __future__ import annotations

from collections.abc import Callable

from dagvane.adapters.localexec import GitOps, run_shell
from dagvane.application.chat import ConversationStore
from dagvane.application.goals import (
    PREPARE_INSTRUCTIONS,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    GoalStore,
    parse_prepared_contract,
)
from dagvane.application.resources import ResourceCatalog, route_task
from dagvane.domain.models import SpecError
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, Monotonic
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import Workspace

Progress = Callable[[str], None]


def _limits_from_config(config: WorkspaceConfig) -> GoalLimits:
    return GoalLimits(
        max_wall_seconds=int(str(config.get("goal.max_wall_seconds"))),
        max_agent_calls=int(str(config.get("goal.max_agent_calls"))),
        max_attempts=int(str(config.get("goal.max_attempts"))),
        max_consecutive_failures=int(
            str(config.get("goal.max_consecutive_failures"))
        ),
    )


def prepare_goal(
    *,
    workspace: Workspace,
    config: WorkspaceConfig,
    conversations: ConversationStore,
    goals: GoalStore,
    catalog: ResourceCatalog,
    runner: ExternalAgentRunner,
    clock: Clock,
    monotonic: Monotonic,
    name: str,
    conversation_id: str,
    progress: Progress,
) -> GoalRecord:
    if goals.exists(name):
        existing = goals.load(name)
        if existing.status not in (GoalStatus.DRAFT, GoalStatus.PREPARED):
            raise SpecError(
                f"goal {name!r} already exists with status {existing.status.value}"
            )
    if not GitOps.is_repo(workspace.root):
        raise SpecError(f"{workspace.root} is not a git repository")
    base_sha = GitOps.head_sha(workspace.root)
    if not GitOps.is_clean(workspace.root):
        progress("warning    working tree is dirty; base SHA is HEAD anyway")

    history = conversations.messages(conversation_id)
    if not history:
        raise SpecError(
            f"conversation {conversation_id!r} is empty; chat with the "
            "workspace first, then prepare the goal"
        )
    transcript = "\n\n".join(
        f"[{item.get('role', '?')}]\n{item.get('text', '')}" for item in history
    )
    prompt = (
        PREPARE_INSTRUCTIONS
        + f"\nRepository root: {workspace.root}\nBase SHA: {base_sha}\n\n"
        + "## Conversation with the owner\n"
        + transcript[-40000:]
    )

    preferred_raw = config.get("goal.prepare_resource")
    preferred = str(preferred_raw) if isinstance(preferred_raw, str) else None
    decision = route_task(catalog, "prepare", preferred_resource=preferred)
    progress(
        f"router     {decision.resource.resource_id} — {decision.reason}"
    )
    timeout = int(str(config.get("goal.agent_timeout_seconds")))
    execution = runner.run(
        AgentInvocation(
            runtime=decision.resource.runtime,
            prompt=prompt,
            cwd=workspace.root,
            model=decision.resource.model,
            reasoning=decision.resource.reasoning,
            timeout_seconds=timeout,
            write_access=False,
            command_template=decision.resource.command_template,
        )
    )
    if not execution.succeeded:
        raise SpecError(
            f"goal preparation agent failed (exit={execution.exit_code}, "
            f"timed_out={execution.timed_out}); see {execution.log_path}"
        )
    contract = parse_prepared_contract(
        execution.output_text,
        name=name,
        base_sha=base_sha,
        limits=_limits_from_config(config),
    )

    # Baseline evidence: run the drafted checks and gates at the base SHA.
    progress("baseline   running acceptance checks and gates at the base SHA")
    baseline_checks: dict[str, object] = {}
    for check in contract.checks:
        result = run_shell(
            check.command, cwd=workspace.root, monotonic=monotonic, timeout_seconds=timeout
        )
        baseline_checks[check.check_id] = {
            "ok": result.ok,
            "exit_code": result.exit_code,
        }
        progress(
            f"baseline   {check.check_id}: {'already met' if result.ok else 'unmet'}"
        )
    baseline_verify: list[dict[str, object]] = []
    for command in contract.verify_commands:
        result = run_shell(
            command, cwd=workspace.root, monotonic=monotonic, timeout_seconds=timeout
        )
        baseline_verify.append(
            {"command": command, "ok": result.ok, "exit_code": result.exit_code}
        )
        progress(f"baseline   {command}: {'ok' if result.ok else 'failing'}")

    now = clock.now_iso()
    record = GoalRecord(
        contract=contract,
        status=GoalStatus.PREPARED,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
        baseline={
            "base_sha": base_sha,
            "checks": baseline_checks,
            "verify": baseline_verify,
            "prepared_from_conversation": conversation_id,
            "prompt_path": execution.prompt_path,
            "output_path": execution.output_path,
        },
    )
    goals.save(record)
    goals.log_event(name, {"event": "goal.prepared", "base_sha": base_sha})
    return record
