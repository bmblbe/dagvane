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
from dagvane.adapters.worktrees import (
    ManagedWorktrees,
    WorktreePurpose,
    WorktreeSpec,
)
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
from dagvane.domain.identifiers import validate_filesystem_id
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
    # Canonical identity check first, before any Git/conversation/routing/
    # runner/progress effect — every downstream use is the validated result.
    name = validate_filesystem_id(name, ctx="prepare_goal: name")
    if goals.exists(name):
        existing = goals.load(name)
        if existing.status not in (GoalStatus.DRAFT, GoalStatus.PREPARED):
            raise SpecError(
                f"goal {name!r} already exists with status {existing.status.value}"
            )
    # Validate/load/bind the Conversation before any Git inspection, progress
    # output, routing, prompt construction or agent invocation.
    history = conversations.messages(conversation_id)
    if not history:
        raise SpecError(
            f"conversation {conversation_id!r} is empty; chat with the "
            "workspace first, then prepare the goal"
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
    goals.save(name, record)
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
    expected_name: str,
    monotonic: Monotonic,
    progress: Progress,
) -> None:
    """Post-approval baseline evidence at the exact approved base SHA.

    ``expected_name`` is validated and required to match
    ``record.contract.name`` exactly before any progress/Git/shell effect —
    the mutable ``record`` payload never alone decides the identity that is
    saved/logged or that names the disposable baseline worktree.

    Runs the owner-approved acceptance checks and verification gates in a
    disposable *managed* worktree pinned to ``contract.base_sha`` — never in
    the canonical worktree. The worktree lives and dies through the
    ``ManagedWorktrees`` protocol: an unowned or hostile entry at the
    deterministic target path fails closed and is preserved, and the
    baseline is recorded ``completed`` only after the managed cleanup has
    succeeded. Any failure — collection or cleanup — leaves the baseline
    ``pending`` and retryable; a genuinely interrupted collection converges
    on the next call, which removes the owned leftover through the managed
    protocol and recreates it fresh.
    """
    validated_expected = validate_filesystem_id(
        expected_name, ctx="collect_baseline: expected_name"
    )
    contract = record.contract
    if validated_expected != contract.name:
        raise SpecError(
            f"collect_baseline: expected name {expected_name!r} does not "
            f"match contract name {contract.name!r}"
        )
    if record.status is not GoalStatus.APPROVED:
        raise SpecError(
            f"goal {validated_expected!r} is {record.status.value}; baseline "
            "evidence is collected only after approval"
        )
    spec = WorktreeSpec(
        goal_name=validated_expected,
        purpose=WorktreePurpose.BASELINE,
        sha=contract.base_sha,
    )
    timeout = int(str(config.get("goal.agent_timeout_seconds")))
    manager = ManagedWorktrees(
        repo_root=workspace.root, worktrees_root=workspace.worktrees_dir
    )
    worktree = manager.target_path(spec)
    progress(
        f"baseline   disposable worktree {worktree} @ {contract.base_sha[:12]}"
    )
    handle = manager.create(spec)
    # The generation-bearing handle keeps the symlink-safe per-target lease
    # held across every baseline command and through proven cleanup. Even an
    # exception in command execution attempts cleanup before the lease is
    # released; a cleanup failure remains explicit and leaves baseline pending.
    worktree = handle.path
    baseline_checks: dict[str, object] = {}
    baseline_verify: list[dict[str, object]] = []
    try:
        for check in contract.checks:
            with handle.pinned_authority() as (
                _worktree_fd,
                _common_fd,
                identity,
            ):
                result = run_shell(
                    check.command,
                    cwd=worktree,
                    cwd_identity=identity,
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
        for command in contract.verify_commands:
            with handle.pinned_authority() as (
                _worktree_fd,
                _common_fd,
                identity,
            ):
                result = run_shell(
                    command,
                    cwd=worktree,
                    cwd_identity=identity,
                    monotonic=monotonic,
                    timeout_seconds=timeout,
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
    finally:
        # Managed cleanup is part of the baseline itself: only after the exact
        # handle generation has provably been removed may evidence complete.
        manager.remove(handle)
    record.baseline = {
        **record.baseline,
        "status": "completed",
        "base_sha": contract.base_sha,
        "checks": baseline_checks,
        "verify": baseline_verify,
    }
    goals.save(validated_expected, record)
    goals.log_event(
        validated_expected,
        {"event": "baseline.completed", "base_sha": contract.base_sha},
    )


def baseline_completed(record: GoalRecord) -> bool:
    return record.baseline.get("status") == "completed"
