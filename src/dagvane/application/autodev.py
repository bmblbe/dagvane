"""The fixed autonomous-development state machine (Autonomous Developer MVP).

One deterministic workflow — no LLM strategist. The machine decides
orchestration (evaluate → task → route → one writer in a candidate worktree →
immutable-candidate verification → exact-SHA independent review → remediation
→ evaluate); external agents provide reasoning and code. Every stage
transition persists durable state, so killing the process at any point and
running ``dagvane goal resume`` continues from the last completed stage
without any provider-native session.

Remediated invariants (Codex acceptance review at ``b40b9fb``):

- **One writer.** A per-goal ``flock`` lease covers the whole
  ``start``/``resume`` loop; a second process is refused, and a crashed
  holder's lease dies with it. Resume additionally terminates any orphaned
  in-flight agent process recorded by a previous run before continuing.
- **Immutable candidate.** The candidate SHA is always derived from Git.
  Acceptance checks and verification gates that decide ``ACHIEVED`` run in a
  fresh disposable worktree pinned to that exact SHA — an ignored or
  untracked file in the writer worktree can never satisfy completion.
  Tracked mutations made by check/verify commands, or a moved HEAD, fail
  closed and are discarded, never committed. ``tested_sha`` is set only after
  every required command passed on that exact SHA, with per-command evidence.
- **Exact-SHA review.** Reviews run in their own pinned checkout; HEAD is
  asserted before and after; the prompt and the durable review record carry
  the exact base SHA, candidate SHA, and the SHA-256 of the full diff.
  Implementer and reviewer resource identity must differ or the run fails
  closed. Review history is append-only: a confirmed BLOCKER/MAJOR for a SHA
  stands until a *new* candidate SHA exists — an unchanged candidate is never
  re-reviewed, so a stochastic empty second review cannot erase findings.
  Malformed or integrity-violating reviewer output is recorded as reviewer/
  infrastructure failure (bounded retries, then ``FAILED``) — never as a code
  defect for the writer to "fix".
- **Crash reconciliation.** The two split-write boundaries (start:
  run-state → goal.json; finish: run-state → goal.json) are repaired by
  ``resume`` without manual edits and without losing a terminal result.
- **Real cancellation.** ``goal cancel`` durably records the intent and
  terminates the recorded in-flight process group; the loop honors the intent
  between stages and never commits post-cancel work.
- **Honest escalation.** ``consecutive_failures`` resets only on objective
  progress (a previously-unmet check newly passing, or verification newly
  passing) — an irrelevant commit alone never resets the routing ladder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagvane.adapters.agents.subprocess_runner import terminate_recorded_process
from dagvane.adapters.localexec import CommandResult, GitOps, run_shell
from dagvane.application.goals import (
    GoalRecord,
    GoalStatus,
    GoalStore,
    contract_to_doc,
)
from dagvane.application.prepare import baseline_completed, collect_baseline
from dagvane.application.resources import ResourceCatalog, RoutingDecision, route_task
from dagvane.domain.models import SpecError
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, IdSource, Monotonic, parse_iso_ms
from dagvane.protocol.frames import sha256_hex
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.lease import GoalLease
from dagvane.workspace.paths import Workspace, atomic_write_json, read_json

Progress = Callable[[str], None]

REVIEW_SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "OPTIONAL")
BLOCKING_SEVERITIES = ("BLOCKER", "MAJOR")

# Independent review is infrastructure: after this many malformed/failed
# review rounds for one run the goal fails closed instead of looping.
_MAX_REVIEW_FAILURES = 2

_EVIDENCE_TAIL_CHARS = 2000


@dataclass(slots=True)
class RunState:
    """Durable goal-run state: the resume contract."""

    run_id: str
    goal_name: str
    base_sha: str
    status: str  # "running" or a terminal GoalStatus value
    started_ts: str
    worktree: str | None = None
    candidate_sha: str | None = None
    tested_sha: str | None = None
    agent_calls: int = 0
    attempts: int = 0
    consecutive_failures: int = 0
    last_unmet: list[str] = field(default_factory=list)
    review_passed_for: str | None = None
    review_findings: list[dict[str, object]] = field(default_factory=list)
    reviews: list[dict[str, object]] = field(default_factory=list)  # append-only
    review_failures: int = 0
    routing_log: list[str] = field(default_factory=list)
    check_results: dict[str, bool] = field(default_factory=dict)
    verify_ok: bool | None = None
    verification: dict[str, object] | None = None
    implement_resource_id: str | None = None
    finish_reason: str | None = None

    def to_doc(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "goal_name": self.goal_name,
            "base_sha": self.base_sha,
            "status": self.status,
            "started_ts": self.started_ts,
            "worktree": self.worktree,
            "candidate_sha": self.candidate_sha,
            "tested_sha": self.tested_sha,
            "agent_calls": self.agent_calls,
            "attempts": self.attempts,
            "consecutive_failures": self.consecutive_failures,
            "last_unmet": list(self.last_unmet),
            "review_passed_for": self.review_passed_for,
            "review_findings": list(self.review_findings),
            "reviews": list(self.reviews),
            "review_failures": self.review_failures,
            "routing_log": list(self.routing_log),
            "check_results": dict(self.check_results),
            "verify_ok": self.verify_ok,
            "verification": self.verification,
            "implement_resource_id": self.implement_resource_id,
            "finish_reason": self.finish_reason,
        }

    @staticmethod
    def from_doc(doc: dict[str, Any]) -> RunState:
        return RunState(
            run_id=str(doc["run_id"]),
            goal_name=str(doc["goal_name"]),
            base_sha=str(doc["base_sha"]),
            status=str(doc["status"]),
            started_ts=str(doc["started_ts"]),
            worktree=doc.get("worktree") if isinstance(doc.get("worktree"), str) else None,
            candidate_sha=(
                doc.get("candidate_sha")
                if isinstance(doc.get("candidate_sha"), str)
                else None
            ),
            tested_sha=(
                doc.get("tested_sha") if isinstance(doc.get("tested_sha"), str) else None
            ),
            agent_calls=int(doc.get("agent_calls", 0)),
            attempts=int(doc.get("attempts", 0)),
            consecutive_failures=int(doc.get("consecutive_failures", 0)),
            last_unmet=[str(x) for x in doc.get("last_unmet", [])],
            review_passed_for=(
                doc.get("review_passed_for")
                if isinstance(doc.get("review_passed_for"), str)
                else None
            ),
            review_findings=[
                dict(item)
                for item in doc.get("review_findings", [])
                if isinstance(item, dict)
            ],
            reviews=[
                dict(item) for item in doc.get("reviews", []) if isinstance(item, dict)
            ],
            review_failures=int(doc.get("review_failures", 0)),
            routing_log=[str(x) for x in doc.get("routing_log", [])],
            check_results={
                str(k): bool(v) for k, v in dict(doc.get("check_results", {})).items()
            },
            verify_ok=(
                bool(doc["verify_ok"]) if doc.get("verify_ok") is not None else None
            ),
            verification=(
                dict(doc["verification"])
                if isinstance(doc.get("verification"), dict)
                else None
            ),
            implement_resource_id=(
                doc.get("implement_resource_id")
                if isinstance(doc.get("implement_resource_id"), str)
                else None
            ),
            finish_reason=(
                doc.get("finish_reason")
                if isinstance(doc.get("finish_reason"), str)
                else None
            ),
        )


def blocking_findings(state: RunState, candidate_sha: str) -> list[dict[str, object]]:
    """Durable BLOCKER/MAJOR findings recorded for exactly this candidate SHA.

    Derived from the append-only review history, so a later empty review of
    the unchanged SHA cannot make a confirmed blocking finding disappear.
    """
    blocking: list[dict[str, object]] = []
    for entry in state.reviews:
        if entry.get("candidate_sha") != candidate_sha or entry.get("valid") is not True:
            continue
        findings = entry.get("findings")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if (
                isinstance(finding, dict)
                and finding.get("severity") in BLOCKING_SEVERITIES
            ):
                blocking.append(finding)
    return blocking


def build_implement_prompt(
    record: GoalRecord,
    *,
    unmet: list[tuple[str, str, str]],  # (check_id, description, evidence tail)
    verify_failures: list[CommandResult],
    review_findings: list[dict[str, object]],
) -> str:
    contract = record.contract
    lines = [
        "You are the single implementation writer inside a candidate git",
        "worktree for an approved, frozen Goal Contract. Implement the",
        "smallest correct change that satisfies the unmet conditions below.",
        "",
        f"Objective: {contract.objective}",
        "",
        "Must have:",
        *[f"- {item}" for item in contract.must_have],
        "",
        "Explicit non-goals (do NOT implement):",
        *[f"- {item}" for item in contract.non_goals],
        "",
        "Rules:",
        "- Work only inside this worktree. Do not push, do not merge.",
        "- Do not modify or weaken the acceptance commands or existing tests",
        "  unless a check is itself the deliverable.",
        "- Keep the change bounded; follow the repository's conventions.",
        "- The deliverable must be tracked by git: acceptance is decided in a",
        "  clean checkout of your committed candidate, so ignored or",
        "  untracked files do not exist there.",
        "",
    ]
    if unmet:
        lines.append("## Unmet acceptance conditions")
        for check_id, description, evidence in unmet:
            lines.append(f"### {check_id}: {description}")
            if evidence:
                lines.append("Last evidence:")
                lines.append("```")
                lines.append(evidence)
                lines.append("```")
        lines.append("")
    if verify_failures:
        lines.append("## Failing verification gates")
        for result in verify_failures:
            lines.append(f"### `{result.command}` (exit {result.exit_code})")
            lines.append("```")
            lines.append(result.output_tail[-3000:])
            lines.append("```")
        lines.append("")
    if review_findings:
        lines.append("## Independent review findings to remediate (BLOCKER/MAJOR)")
        for finding in review_findings:
            severity = finding.get("severity", "?")
            lines.append(f"- [{severity}] {finding.get('description', '')}")
        lines.append("")
    lines.append(
        "When done, ensure the acceptance commands pass, then reply with a "
        "one-paragraph summary of what you changed."
    )
    return "\n".join(lines)


def build_review_prompt(
    record: GoalRecord,
    diff_text: str,
    *,
    base_sha: str,
    candidate_sha: str,
    diff_sha256: str,
) -> str:
    contract = record.contract
    return "\n".join(
        [
            "You are an independent reviewer for an autonomous implementation",
            "candidate. You did not write this change. Review the diff below",
            "against the frozen Goal Contract for correctness, security, and",
            "contract violations (including silently weakened tests).",
            "",
            f"Objective: {contract.objective}",
            "Non-goals: " + "; ".join(contract.non_goals),
            "",
            "Your verdict applies to exactly this immutable candidate:",
            f"- base SHA: {base_sha}",
            f"- candidate SHA under review: {candidate_sha}",
            f"- SHA-256 of the full diff: {diff_sha256}",
            "Your checkout is pinned to the candidate SHA; do not modify it.",
            "",
            "Reply with STRICT JSON only (no markdown fence):",
            '{"findings": [{"severity": "BLOCKER|MAJOR|MINOR|OPTIONAL",',
            ' "description": "<finding>", "file": "<path or empty>"}]}',
            'An empty findings list means the candidate is acceptable.',
            "",
            "## Candidate diff",
            "```diff",
            diff_text,
            "```",
        ]
    )


def parse_review_findings(output_text: str) -> list[dict[str, object]]:
    import json

    text = output_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SpecError("review output carries no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SpecError(f"review output is not valid JSON: {exc}") from exc
    raw = doc.get("findings") if isinstance(doc, dict) else None
    if not isinstance(raw, list):
        raise SpecError("review output carries no findings list")
    findings: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).upper()
        if severity not in REVIEW_SEVERITIES:
            severity = "MINOR"
        findings.append(
            {
                "severity": severity,
                "description": str(item.get("description", "")),
                "file": str(item.get("file", "")),
            }
        )
    return findings


@dataclass(slots=True)
class _CandidateEvaluation:
    """Outcome of the immutable-candidate stage for one loop iteration."""

    finished: GoalStatus | None = None
    verify_failures: list[CommandResult] = field(default_factory=list)
    blocking: list[dict[str, object]] = field(default_factory=list)
    skip_writer: bool = False  # review infrastructure retry: no writer work


class GoalRunner:
    """Foreground executor for ``goal run`` / ``goal resume``."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        config: WorkspaceConfig,
        store: GoalStore,
        catalog: ResourceCatalog,
        runner: ExternalAgentRunner,
        clock: Clock,
        monotonic: Monotonic,
        ids: IdSource,
        progress: Progress,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._workspace = workspace
        self._config = config
        self._store = store
        self._catalog = catalog
        self._runner = runner
        self._clock = clock
        self._monotonic = monotonic
        self._ids = ids
        self._progress = progress
        self._scrubber = scrubber if scrubber is not None else process_scrubber()

    # -- durable state -----------------------------------------------------

    def _state_path(self, goal_name: str) -> Path:
        return self._store.goal_dir(goal_name) / "run-state.json"

    def _save_state(self, state: RunState) -> None:
        atomic_write_json(self._state_path(state.goal_name), state.to_doc())

    def load_state(self, goal_name: str) -> RunState | None:
        path = self._state_path(goal_name)
        if not path.exists():
            return None
        return RunState.from_doc(read_json(path))

    def process_record_path(self, goal_name: str) -> Path:
        """Where the in-flight agent process identity is persisted."""
        return self._store.goal_dir(goal_name) / "agent-process.json"

    # -- entry points ------------------------------------------------------

    def start(self, goal_name: str) -> GoalStatus:
        lease = GoalLease(self._store.goal_dir(goal_name) / "lease.lock")
        lease.acquire(owner=f"goal-start:{goal_name}")
        try:
            record = self._store.load(goal_name)
            existing = self.load_state(goal_name)
            if existing is not None and existing.status == "running":
                raise SpecError(
                    f"goal {goal_name!r} already has an active run "
                    f"{existing.run_id}; use `dagvane goal resume {goal_name}`"
                )
            if record.status is not GoalStatus.APPROVED:
                raise SpecError(
                    f"goal {goal_name!r} is {record.status.value}; approve it first "
                    "(or use `goal resume` for a running goal)"
                )
            if not baseline_completed(record):
                self._progress(
                    "baseline   pending — collecting at the approved base SHA "
                    "in a disposable worktree first"
                )
                collect_baseline(
                    workspace=self._workspace,
                    config=self._config,
                    goals=self._store,
                    record=record,
                    monotonic=self._monotonic,
                    progress=self._progress,
                )
            state = RunState(
                run_id=self._ids.new_id("goalrun"),
                goal_name=goal_name,
                base_sha=record.contract.base_sha,
                status="running",
                started_ts=self._clock.now_iso(),
            )
            self._save_state(state)
            record.status = GoalStatus.RUNNING
            self._store.save(record)
            self._store.log_event(
                goal_name, {"event": "run.started", "run_id": state.run_id}
            )
            return self._loop(record, state)
        finally:
            lease.release()

    def resume(self, goal_name: str) -> GoalStatus:
        lease = GoalLease(self._store.goal_dir(goal_name) / "lease.lock")
        lease.acquire(owner=f"goal-resume:{goal_name}")
        try:
            record = self._store.load(goal_name)
            state = self.load_state(goal_name)
            if state is None:
                raise SpecError(f"goal {goal_name!r} has no run to resume")
            if state.status != "running":
                # Terminal run-state. If goal.json disagrees, the process
                # crashed between the two finish writes: replay the finish
                # from durable run-state instead of losing the result.
                try:
                    terminal = GoalStatus(state.status)
                except ValueError as exc:
                    raise SpecError(
                        f"goal {goal_name!r}: malformed run-state status "
                        f"{state.status!r}"
                    ) from exc
                if record.status is GoalStatus.RUNNING:
                    reason = state.finish_reason or "reconciled from durable run-state"
                    self._finalize_record(record, state, terminal, reason)
                    self._progress(
                        f"reconcile  goal repaired to terminal {terminal.value} "
                        "(crash between finish writes)"
                    )
                    return terminal
                raise SpecError(
                    f"goal {goal_name!r} run {state.run_id} already finished "
                    f"({state.status})"
                )
            # Active run-state.
            if record.status is GoalStatus.APPROVED:
                # Crash between run-state creation and the goal status write.
                record.status = GoalStatus.RUNNING
                self._store.save(record)
                self._store.log_event(
                    goal_name,
                    {"event": "run.reconciled", "repair": "approved->running"},
                )
                self._progress(
                    "reconcile  goal repaired to running (crash between start writes)"
                )
            elif record.status is GoalStatus.CANCELLED:
                self._reap_orphan_writer(goal_name)
                return self._finish(
                    record, state, GoalStatus.CANCELLED, "cancelled by owner"
                )
            elif record.status is not GoalStatus.RUNNING:
                raise SpecError(
                    f"goal {goal_name!r} is {record.status.value}; run "
                    f"{state.run_id} cannot resume"
                )
            self._reap_orphan_writer(goal_name)
            self._store.log_event(
                goal_name, {"event": "run.resumed", "run_id": state.run_id}
            )
            self._progress(f"resume     run {state.run_id} from durable state")
            return self._loop(record, state)
        finally:
            lease.release()

    # -- reconciliation helpers ---------------------------------------------

    def _reap_orphan_writer(self, goal_name: str) -> None:
        """Terminate an in-flight agent process left by a crashed run."""
        if terminate_recorded_process(self.process_record_path(goal_name)):
            self._progress(
                "reconcile  terminated an orphaned in-flight agent process "
                "from a previous run"
            )
            self._store.log_event(goal_name, {"event": "run.orphan_reaped"})

    def _cancel_requested(self, goal_name: str) -> bool:
        return self._store.load(goal_name).status is GoalStatus.CANCELLED

    # -- the fixed workflow ------------------------------------------------

    def _finish(
        self, record: GoalRecord, state: RunState, status: GoalStatus, reason: str
    ) -> GoalStatus:
        state.status = status.value
        state.finish_reason = reason
        self._save_state(state)
        self._finalize_record(record, state, status, reason)
        self._progress(f"result     {status.value}: {reason}")
        return status

    def _finalize_record(
        self, record: GoalRecord, state: RunState, status: GoalStatus, reason: str
    ) -> None:
        record.status = status
        record.evidence = {
            "run_id": state.run_id,
            "reason": reason,
            "candidate_sha": state.candidate_sha,
            "tested_sha": state.tested_sha,
            "check_results": dict(state.check_results),
            "verify_ok": state.verify_ok,
            "verification": state.verification,
            "agent_calls": state.agent_calls,
            "attempts": state.attempts,
            "worktree": state.worktree,
            "review_findings": list(state.review_findings),
            "reviews": list(state.reviews),
        }
        self._store.save(record)
        self._store.log_event(
            state.goal_name,
            {"event": "run.finished", "status": status.value, "reason": reason},
        )

    def _wall_seconds(self, state: RunState) -> int:
        started = parse_iso_ms(state.started_ts)
        now = parse_iso_ms(self._clock.now_iso())
        return int((now - started).total_seconds())

    def _ensure_worktree(self, record: GoalRecord, state: RunState) -> Path:
        if state.worktree is not None:
            path = Path(state.worktree)
            if path.exists():
                return path
        path = self._workspace.worktrees_dir / f"{state.goal_name}-{state.run_id}"
        if not path.exists():
            GitOps.worktree_add(self._workspace.root, path, record.contract.base_sha)
            self._progress(f"worktree   {path} @ {record.contract.base_sha[:12]}")
        state.worktree = str(path)
        self._save_state(state)
        return path

    def _run_checks(
        self, record: GoalRecord, worktree: Path
    ) -> dict[str, CommandResult]:
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        results: dict[str, CommandResult] = {}
        for check in record.contract.checks:
            result = run_shell(
                check.command,
                cwd=worktree,
                monotonic=self._monotonic,
                timeout_seconds=timeout,
                scrubber=self._scrubber,
            )
            results[check.check_id] = result
            status = "ok" if result.ok else f"FAIL (exit {result.exit_code})"
            self._progress(f"check      {check.check_id}: {status}")
        return results

    def _review_required(self, state: RunState, worktree: Path) -> bool:
        policy = str(self._config.get("goal.review_policy"))
        if policy == "never":
            return False
        if policy == "always":
            return True
        changed = GitOps.changed_files(worktree, state.base_sha)
        return len(changed) >= 2  # "substantial": multi-file change

    def _invoke_agent(
        self,
        state: RunState,
        decision: RoutingDecision,
        prompt: str,
        *,
        cwd: Path,
        write_access: bool,
    ) -> str:
        resource = decision.resource
        state.routing_log.append(decision.reason)
        self._progress(
            f"router     {resource.resource_id} ({resource.model or resource.runtime}"
            f"/{resource.reasoning or '-'}) — {decision.reason}"
        )
        state.agent_calls += 1
        self._save_state(state)
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        execution = self._runner.run(
            AgentInvocation(
                runtime=resource.runtime,
                prompt=prompt,
                cwd=cwd,
                model=resource.model,
                reasoning=resource.reasoning,
                timeout_seconds=timeout,
                write_access=write_access,
                command_template=resource.command_template,
                env_passthrough=resource.env_passthrough,
                secret_env=resource.secret_env,
                process_record_path=self.process_record_path(state.goal_name),
            )
        )
        self._store.log_event(
            state.goal_name,
            {
                "event": "agent.finished",
                "resource": resource.resource_id,
                "exit_code": execution.exit_code,
                "timed_out": execution.timed_out,
                "duration_ms": execution.duration_ms,
                "prompt_path": execution.prompt_path,
                "output_path": execution.output_path,
            },
        )
        status = "ok" if execution.succeeded else "FAILED"
        self._progress(f"agent      {resource.runtime} finished: {status}")
        if not execution.succeeded:
            raise SpecError(
                f"external agent {resource.resource_id} failed "
                f"(exit={execution.exit_code}, timed_out={execution.timed_out})"
            )
        return execution.output_text

    def _loop(self, record: GoalRecord, state: RunState) -> GoalStatus:
        limits = record.contract.limits
        while True:
            # Owner cancellation is durable: honor it between stages.
            if self._cancel_requested(state.goal_name):
                return self._finish(
                    record, state, GoalStatus.CANCELLED, "cancelled by owner"
                )

            # Anti-runaway bounds.
            wall = self._wall_seconds(state)
            if wall > limits.max_wall_seconds:
                return self._finish(
                    record,
                    state,
                    GoalStatus.BUDGET_EXHAUSTED,
                    f"wall time {wall}s exceeds {limits.max_wall_seconds}s",
                )
            if state.agent_calls >= limits.max_agent_calls:
                return self._finish(
                    record,
                    state,
                    GoalStatus.BUDGET_EXHAUSTED,
                    f"agent calls {state.agent_calls} reached the cap",
                )
            if state.attempts >= limits.max_attempts:
                return self._finish(
                    record,
                    state,
                    GoalStatus.BLOCKED,
                    f"attempts {state.attempts} reached the cap",
                )
            if state.consecutive_failures >= limits.max_consecutive_failures:
                return self._finish(
                    record,
                    state,
                    GoalStatus.BLOCKED,
                    f"{state.consecutive_failures} consecutive failed attempts",
                )

            worktree = self._ensure_worktree(record, state)

            # Evaluate: acceptance checks in the writer worktree are the
            # progress signal; ACHIEVED is decided only by the immutable
            # verification of the committed candidate below.
            check_results = self._run_checks(record, worktree)
            previously_unmet = set(state.last_unmet)
            state.check_results = {
                check_id: result.ok for check_id, result in check_results.items()
            }
            unmet = [
                (
                    check.check_id,
                    check.description,
                    check_results[check.check_id].output_tail[-1500:],
                )
                for check in record.contract.checks
                if not check_results[check.check_id].ok
            ]
            state.last_unmet = [check_id for check_id, _, _ in unmet]
            met = len(record.contract.checks) - len(unmet)
            self._progress(
                f"progress   {met}/{len(record.contract.checks)} acceptance conditions"
            )
            self._save_state(state)

            verify_failures: list[CommandResult] = []
            blocking: list[dict[str, object]] = []
            if not unmet:
                evaluation = self._evaluate_candidate(record, state, worktree)
                if evaluation.finished is not None:
                    return evaluation.finished
                if evaluation.skip_writer:
                    continue
                verify_failures = evaluation.verify_failures
                blocking = evaluation.blocking

            # Implement (or remediate): the one writer, in the worktree.
            remediating = bool(verify_failures or blocking)
            task_kind = "remediate" if remediating else "implement"
            preferred_raw = self._config.get("goal.implement_resource")
            preferred = str(preferred_raw) if isinstance(preferred_raw, str) else None
            decision = route_task(
                self._catalog,
                task_kind,
                attempt=state.consecutive_failures + 1,
                preferred_resource=preferred,
            )
            head_before = GitOps.head_sha(worktree)
            prompt = build_implement_prompt(
                record,
                unmet=unmet,
                verify_failures=verify_failures,
                review_findings=blocking,
            )
            state.attempts += 1
            self._progress(
                f"task       attempt {state.attempts}: "
                f"{task_kind} ({len(unmet)} unmet)"
            )
            try:
                self._invoke_agent(
                    state, decision, prompt, cwd=worktree, write_access=True
                )
            except SpecError as exc:
                state.consecutive_failures += 1
                self._save_state(state)
                self._store.log_event(
                    state.goal_name, {"event": "attempt.failed", "error": str(exc)}
                )
                continue
            state.implement_resource_id = decision.resource.resource_id

            # Never commit post-cancel work: the writer may have been
            # terminated by `goal cancel`; its partial bytes stay uncommitted.
            if self._cancel_requested(state.goal_name):
                return self._finish(
                    record,
                    state,
                    GoalStatus.CANCELLED,
                    "cancelled by owner during implementation; "
                    "uncommitted work discarded",
                )

            committed = GitOps.commit_all(
                worktree,
                f"goal({state.goal_name}): attempt {state.attempts} ({task_kind})",
            )
            if committed is not None:
                state.candidate_sha = committed
                state.tested_sha = None
                state.verification = None
                state.verify_ok = None
                self._progress(f"candidate  {committed[:12]}")

            # Progress accounting drives escalation: only objective progress
            # resets the ladder. A previously-unmet check newly passing counts;
            # verification newly passing counts (reset inside the immutable
            # stage). A commit alone — however large — never resets: an agent
            # producing irrelevant commits must still escalate.
            after_results = {
                check.check_id: run_shell(
                    check.command,
                    cwd=worktree,
                    monotonic=self._monotonic,
                    timeout_seconds=int(
                        str(self._config.get("goal.agent_timeout_seconds"))
                    ),
                    scrubber=self._scrubber,
                ).ok
                for check in record.contract.checks
                if check.check_id in previously_unmet or not previously_unmet
            }
            improved = any(
                after_results.get(check_id, False) for check_id in state.last_unmet
            )
            if improved:
                state.consecutive_failures = 0
            elif state.last_unmet:
                state.consecutive_failures += 1
            elif committed is None and GitOps.head_sha(worktree) == head_before:
                # Gate/review remediation that changed nothing: no progress.
                state.consecutive_failures += 1
            # else: gate/review remediation produced a new candidate — its
            # verdict comes from the next immutable verification, not from
            # the mere existence of a commit.
            self._save_state(state)

    # -- immutable candidate evaluation --------------------------------------

    def _evaluate_candidate(
        self, record: GoalRecord, state: RunState, worktree: Path
    ) -> _CandidateEvaluation:
        # Freeze: commit any uncommitted writer work. The candidate SHA is
        # derived from Git every time — never trusted from stale run-state.
        committed = GitOps.commit_all(
            worktree, f"goal({state.goal_name}): candidate"
        )
        candidate = GitOps.head_sha(worktree)
        if committed is not None:
            self._progress(f"candidate  {candidate[:12]} (frozen)")
        if state.candidate_sha != candidate:
            state.candidate_sha = candidate
            state.tested_sha = None
            state.verification = None
            state.verify_ok = None
            self._save_state(state)

        durable_blocking = blocking_findings(state, candidate)
        if durable_blocking:
            self._progress(
                f"finding    {len(durable_blocking)} durable BLOCKER/MAJOR for "
                f"{candidate[:12]} — remediation required (an unchanged "
                "candidate is not re-reviewed)"
            )
            return _CandidateEvaluation(blocking=durable_blocking)

        if state.tested_sha != candidate:
            ok, failures = self._immutable_verification(record, state, candidate)
            if not ok:
                state.verify_ok = False
                self._save_state(state)
                return _CandidateEvaluation(verify_failures=failures)
            state.verify_ok = True
            state.tested_sha = candidate
            state.consecutive_failures = 0  # objective progress
            self._save_state(state)

        if (
            self._review_required(state, worktree)
            and state.review_passed_for != candidate
        ):
            kind, review_blocking, reason = self._review_candidate(
                record, state, candidate
            )
            if kind == "fatal":
                return _CandidateEvaluation(
                    finished=self._finish(record, state, GoalStatus.FAILED, reason)
                )
            if kind == "infra":
                if state.review_failures >= _MAX_REVIEW_FAILURES:
                    return _CandidateEvaluation(
                        finished=self._finish(
                            record,
                            state,
                            GoalStatus.FAILED,
                            f"independent review failed {state.review_failures} "
                            "times (reviewer/infrastructure failure, not a "
                            "code defect)",
                        )
                    )
                return _CandidateEvaluation(skip_writer=True)
            if kind == "blocking":
                return _CandidateEvaluation(blocking=review_blocking)
            state.review_passed_for = candidate
            self._save_state(state)
            return _CandidateEvaluation(
                finished=self._finish(
                    record,
                    state,
                    GoalStatus.ACHIEVED,
                    "all acceptance conditions, gates, and review passed",
                )
            )
        return _CandidateEvaluation(
            finished=self._finish(
                record,
                state,
                GoalStatus.ACHIEVED,
                "all acceptance conditions and gates passed",
            )
        )

    def _immutable_verification(
        self, record: GoalRecord, state: RunState, candidate: str
    ) -> tuple[bool, list[CommandResult]]:
        """Run every acceptance check and verification gate in a fresh
        disposable worktree pinned to exactly ``candidate``.

        Fails closed when a command mutates tracked bytes or moves HEAD:
        those bytes are discarded with the worktree, never committed. Per-
        command evidence (command, SHA, exit/timeout, duration, bounded
        scrubbed output and its hash) is persisted in run-state.
        """
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        worktree = (
            self._workspace.worktrees_dir
            / f"{state.goal_name}-{state.run_id}-verify"
        )
        self._progress(
            f"verify-wt  fresh disposable worktree @ {candidate[:12]} "
            "(immutable candidate)"
        )
        GitOps.fresh_worktree(self._workspace.root, worktree, candidate)
        results: list[CommandResult] = []
        evidence: list[dict[str, object]] = []
        try:
            labelled = [
                (f"check:{check.check_id}", check.command)
                for check in record.contract.checks
            ] + [("verify", command) for command in record.contract.verify_commands]
            for label, command in labelled:
                result = run_shell(
                    command,
                    cwd=worktree,
                    monotonic=self._monotonic,
                    timeout_seconds=timeout,
                    scrubber=self._scrubber,
                )
                results.append(result)
                evidence.append(
                    {
                        "label": label,
                        "command": command,
                        "sha": candidate,
                        "exit_code": result.exit_code,
                        "timed_out": result.exit_code is None,
                        "duration_ms": result.duration_ms,
                        "output_sha256": sha256_hex(
                            result.output_tail.encode("utf-8")
                        ),
                        "output_tail": result.output_tail[-_EVIDENCE_TAIL_CHARS:],
                    }
                )
                status = "ok" if result.ok else f"FAIL (exit {result.exit_code})"
                self._progress(f"verify     {label} @ {candidate[:12]}: {status}")
            mutated = GitOps.tracked_dirty(worktree)
            head_after = GitOps.head_sha(worktree)
        finally:
            try:
                GitOps.worktree_remove(self._workspace.root, worktree)
            except SpecError:  # pragma: no cover — cleanup is best-effort
                pass
        ok = all(result.ok for result in results)
        if mutated:
            ok = False
            results.append(
                CommandResult(
                    command="<tracked-mutation-guard>",
                    exit_code=1,
                    duration_ms=0,
                    output_tail=(
                        "verification commands mutated tracked files: "
                        + ", ".join(mutated[:20])
                        + " — failing closed; the mutated bytes were discarded "
                        "with the disposable worktree, never committed"
                    ),
                )
            )
            self._progress(
                "verify     FAIL: commands mutated tracked files (failing closed)"
            )
        if head_after != candidate:
            ok = False
            results.append(
                CommandResult(
                    command="<head-pin-guard>",
                    exit_code=1,
                    duration_ms=0,
                    output_tail=(
                        f"verification worktree HEAD moved to {head_after}; "
                        f"expected {candidate} — failing closed"
                    ),
                )
            )
            self._progress(
                "verify     FAIL: verification worktree HEAD moved (failing closed)"
            )
        state.verification = {
            "sha": candidate,
            "ok": ok,
            "commands": evidence,
            "tracked_mutations": mutated[:50],
            "head_after": head_after,
        }
        self._save_state(state)
        return ok, [result for result in results if not result.ok]

    # -- exact-SHA independent review ----------------------------------------

    def _record_review_failure(
        self, state: RunState, entry: dict[str, object], error: str
    ) -> None:
        entry["error"] = error
        state.review_failures += 1
        state.reviews.append(entry)
        self._save_state(state)
        self._store.log_event(
            state.goal_name,
            {"event": "review.failed", "error": error},
        )

    def _review_candidate(
        self, record: GoalRecord, state: RunState, candidate: str
    ) -> tuple[str, list[dict[str, object]], str]:
        """One independent review bound to the exact candidate SHA.

        Returns ``(kind, blocking, reason)`` with kind in ``clean`` |
        ``blocking`` | ``infra`` | ``fatal``. Malformed or integrity-violating
        reviewer output is reviewer/infrastructure failure: it is recorded as
        such and never fabricates a code finding for the writer.
        """
        preferred_raw = self._config.get("goal.review_resource")
        preferred = str(preferred_raw) if isinstance(preferred_raw, str) else None
        decision = route_task(
            self._catalog,
            "review",
            attempt=state.review_failures + 1,
            preferred_resource=preferred,
        )
        implementer = state.implement_resource_id
        if implementer is None:
            implement_raw = self._config.get("goal.implement_resource")
            implementer = (
                implement_raw if isinstance(implement_raw, str) else None
            )
        if implementer is not None and decision.resource.resource_id == implementer:
            return (
                "fatal",
                [],
                f"reviewer resource {decision.resource.resource_id!r} equals "
                "the implementation writer; independent review requires a "
                "distinct resource (configure goal.review_resource)",
            )
        worktree = (
            self._workspace.worktrees_dir
            / f"{state.goal_name}-{state.run_id}-review"
        )
        GitOps.fresh_worktree(self._workspace.root, worktree, candidate)
        entry: dict[str, object] = {
            "ts": self._clock.now_iso(),
            "candidate_sha": candidate,
            "base_sha": state.base_sha,
            "resource": decision.resource.resource_id,
            "valid": False,
            "findings": [],
        }
        try:
            head_before = GitOps.head_sha(worktree)
            entry["head_before"] = head_before
            if head_before != candidate:
                self._record_review_failure(
                    state,
                    entry,
                    f"review checkout HEAD {head_before} != candidate {candidate}",
                )
                return "infra", [], "review checkout integrity failure"
            diff_sha = GitOps.diff_sha256(worktree, state.base_sha)
            entry["diff_sha256"] = diff_sha
            diff = self._scrubber.scrub(GitOps.diff_text(worktree, state.base_sha))
            prompt = build_review_prompt(
                record,
                diff,
                base_sha=state.base_sha,
                candidate_sha=candidate,
                diff_sha256=diff_sha,
            )
            self._progress(
                f"review     independent review of exact candidate {candidate[:12]}"
            )
            try:
                output = self._invoke_agent(
                    state, decision, prompt, cwd=worktree, write_access=False
                )
            except SpecError as exc:
                self._record_review_failure(
                    state, entry, f"review agent failed: {exc}"
                )
                return "infra", [], str(exc)
            head_after = GitOps.head_sha(worktree)
            entry["head_after"] = head_after
            entry["head_verified"] = head_after == candidate
            if head_after != candidate:
                self._record_review_failure(
                    state,
                    entry,
                    f"review checkout HEAD moved to {head_after} during review",
                )
                self._progress(
                    "review     FAIL: reviewer mutated the pinned checkout "
                    "(infrastructure failure)"
                )
                return "infra", [], "reviewer mutated the pinned checkout"
            try:
                findings = parse_review_findings(output)
            except SpecError as exc:
                self._record_review_failure(
                    state, entry, f"unparseable reviewer output: {exc}"
                )
                self._progress(
                    "review     reviewer output unparseable — infrastructure "
                    "failure, not a code defect"
                )
                return "infra", [], str(exc)
            entry["valid"] = True
            entry["findings"] = findings
            state.reviews.append(entry)
            state.review_findings = findings
            self._save_state(state)
            self._store.log_event(
                state.goal_name,
                {
                    "event": "review.finished",
                    "candidate_sha": candidate,
                    "base_sha": state.base_sha,
                    "diff_sha256": diff_sha,
                    "resource": decision.resource.resource_id,
                    "findings": len(findings),
                },
            )
            review_blocking = [
                finding
                for finding in findings
                if finding.get("severity") in BLOCKING_SEVERITIES
            ]
            if review_blocking:
                self._progress(
                    f"finding    {len(review_blocking)} BLOCKER/MAJOR at "
                    f"{candidate[:12]} — remediation required"
                )
                return "blocking", review_blocking, ""
            return "clean", [], ""
        finally:
            try:
                GitOps.worktree_remove(self._workspace.root, worktree)
            except SpecError:  # pragma: no cover — cleanup is best-effort
                pass


def goal_show_doc(record: GoalRecord, state: RunState | None) -> dict[str, object]:
    doc: dict[str, object] = {
        "contract": contract_to_doc(record.contract),
        "status": record.status.value,
        "contract_sha256": record.contract_sha256,
        "baseline": record.baseline,
        "amendments": record.amendments,
        "evidence": record.evidence,
    }
    if state is not None:
        doc["run"] = state.to_doc()
    return doc
