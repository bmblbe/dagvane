"""Chat → Goal Contract preparation (Autonomous Developer MVP).

``goal prepare --from-conversation`` turns accepted conversation state plus
actual repository inspection into an owner-reviewable frozen contract draft:
objective, must-have scope, explicit non-goals, objective acceptance checks,
and verification gates. Preparation is **draft-only**: the model-proposed
acceptance and verification commands are persisted for the owner to read but
none of them is executed before the owner has approved the visible contract
(``goal approve``). The owner never writes YAML — they review ``goal show``
output and approve.

Baseline evidence is collected only *after* approval, by ``collect_baseline``:
the approved commands run in a disposable Git worktree pinned to the exact
contract ``base_sha``, so the canonical target worktree is never mutated and
the evidence can never come from a state different from the recorded base.
The baseline is labeled ``pending`` until the collection completes; an
interrupted collection is safely re-run (fresh disposable worktree each time).
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
    if not GitOps.is_clean(workspace.root):
        raise SpecError(
            "the working tree is dirty; goal preparation requires a clean "
            "repository so the contract base SHA describes the actual source "
            "state — commit or stash your changes first"
        )
    base_sha = GitOps.head_sha(workspace.root)

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
            env_passthrough=decision.resource.env_passthrough,
            secret_env=decision.resource.secret_env,
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

    # Draft-only: the proposed commands are persisted for owner review but
    # deliberately NOT executed here — baseline evidence follows approval.
    now = clock.now_iso()
    record = GoalRecord(
        contract=contract,
        status=GoalStatus.PREPARED,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
        baseline={
            "status": "pending",
            "base_sha": base_sha,
            "prepared_from_conversation": conversation_id,
            "prompt_path": execution.prompt_path,
            "output_path": execution.output_path,
        },
    )
    goals.save(record)
    goals.log_event(name, {"event": "goal.prepared", "base_sha": base_sha})
    progress(
        "prepared   draft only: no proposed command has been executed; "
        "baseline evidence follows owner approval"
    )
    return record


def collect_baseline(
    *,
    workspace: Workspace,
    config: WorkspaceConfig,
    goals: GoalStore,
    record: GoalRecord,
    monotonic: Monotonic,
    progress: Progress,
) -> None:
    """Post-approval baseline evidence at the exact approved base SHA.

    Runs the owner-approved acceptance checks and verification gates in a
    disposable worktree pinned to ``contract.base_sha`` — never in the
    canonical worktree. Idempotent: an interrupted collection leaves the
    baseline labeled ``pending`` and the next call starts over from a fresh
    disposable worktree.
    """
    contract = record.contract
    if record.status is not GoalStatus.APPROVED:
        raise SpecError(
            f"goal {contract.name!r} is {record.status.value}; baseline "
            "evidence is collected only after approval"
        )
    timeout = int(str(config.get("goal.agent_timeout_seconds")))
    worktree = workspace.worktrees_dir / f"{contract.name}-baseline"
    progress(
        f"baseline   disposable worktree {worktree} @ {contract.base_sha[:12]}"
    )
    GitOps.fresh_worktree(workspace.root, worktree, contract.base_sha)
    try:
        baseline_checks: dict[str, object] = {}
        for check in contract.checks:
            result = run_shell(
                check.command,
                cwd=worktree,
                monotonic=monotonic,
                timeout_seconds=timeout,
            )
            baseline_checks[check.check_id] = {
                "ok": result.ok,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
            }
            progress(
                f"baseline   {check.check_id}: "
                f"{'already met' if result.ok else 'unmet'}"
            )
        baseline_verify: list[dict[str, object]] = []
        for command in contract.verify_commands:
            result = run_shell(
                command, cwd=worktree, monotonic=monotonic, timeout_seconds=timeout
            )
            baseline_verify.append(
                {
                    "command": command,
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                }
            )
            progress(f"baseline   {command}: {'ok' if result.ok else 'failing'}")
        record.baseline = {
            **record.baseline,
            "status": "completed",
            "base_sha": contract.base_sha,
            "checks": baseline_checks,
            "verify": baseline_verify,
        }
        goals.save(record)
        goals.log_event(
            contract.name,
            {"event": "baseline.completed", "base_sha": contract.base_sha},
        )
    finally:
        try:
            GitOps.worktree_remove(workspace.root, worktree)
        except SpecError:  # pragma: no cover — cleanup is best-effort
            pass


def baseline_completed(record: GoalRecord) -> bool:
    return record.baseline.get("status") == "completed"
