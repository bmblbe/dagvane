"""The fixed autonomous-development state machine (Autonomous Developer MVP).

One deterministic workflow — no LLM strategist. The machine decides
orchestration (evaluate → task → route → one writer in a candidate worktree →
deterministic verification → independent review → remediation → evaluate);
external agents provide reasoning and code. Every stage transition persists
durable state, so killing the process at any point and running
``dagvane goal resume`` continues from the last completed stage without any
provider-native session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dagvane.adapters.localexec import CommandResult, GitOps, run_shell
from dagvane.application.goals import (
    GoalRecord,
    GoalStatus,
    GoalStore,
    contract_to_doc,
)
from dagvane.application.resources import ResourceCatalog, RoutingDecision, route_task
from dagvane.domain.models import SpecError
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, IdSource, Monotonic, parse_iso_ms
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import Workspace, atomic_write_json, read_json

Progress = Callable[[str], None]

REVIEW_SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "OPTIONAL")


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
    routing_log: list[str] = field(default_factory=list)
    check_results: dict[str, bool] = field(default_factory=dict)
    verify_ok: bool | None = None

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
            "routing_log": list(self.routing_log),
            "check_results": dict(self.check_results),
            "verify_ok": self.verify_ok,
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
            routing_log=[str(x) for x in doc.get("routing_log", [])],
            check_results={
                str(k): bool(v) for k, v in dict(doc.get("check_results", {})).items()
            },
            verify_ok=(
                bool(doc["verify_ok"]) if doc.get("verify_ok") is not None else None
            ),
        )


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


def build_review_prompt(record: GoalRecord, diff_text: str) -> str:
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

    # -- entry points ------------------------------------------------------

    def start(self, goal_name: str) -> GoalStatus:
        record = self._store.load(goal_name)
        if record.status is not GoalStatus.APPROVED:
            raise SpecError(
                f"goal {goal_name!r} is {record.status.value}; approve it first "
                "(or use `goal resume` for a running goal)"
            )
        existing = self.load_state(goal_name)
        if existing is not None and existing.status == "running":
            raise SpecError(
                f"goal {goal_name!r} already has an active run "
                f"{existing.run_id}; use `dagvane goal resume {goal_name}`"
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
        self._store.log_event(goal_name, {"event": "run.started", "run_id": state.run_id})
        return self._loop(record, state)

    def resume(self, goal_name: str) -> GoalStatus:
        record = self._store.load(goal_name)
        state = self.load_state(goal_name)
        if state is None:
            raise SpecError(f"goal {goal_name!r} has no run to resume")
        if state.status != "running":
            raise SpecError(
                f"goal {goal_name!r} run {state.run_id} already finished "
                f"({state.status})"
            )
        if record.status is not GoalStatus.RUNNING:
            raise SpecError(
                f"goal {goal_name!r} is {record.status.value}; nothing to resume"
            )
        self._store.log_event(goal_name, {"event": "run.resumed", "run_id": state.run_id})
        self._progress(f"resume     run {state.run_id} from durable state")
        return self._loop(record, state)

    # -- the fixed workflow ------------------------------------------------

    def _finish(
        self, record: GoalRecord, state: RunState, status: GoalStatus, reason: str
    ) -> GoalStatus:
        state.status = status.value
        self._save_state(state)
        record.status = status
        record.evidence = {
            "run_id": state.run_id,
            "reason": reason,
            "candidate_sha": state.candidate_sha,
            "tested_sha": state.tested_sha,
            "check_results": dict(state.check_results),
            "verify_ok": state.verify_ok,
            "agent_calls": state.agent_calls,
            "attempts": state.attempts,
            "worktree": state.worktree,
            "review_findings": list(state.review_findings),
        }
        self._store.save(record)
        self._store.log_event(
            state.goal_name,
            {"event": "run.finished", "status": status.value, "reason": reason},
        )
        self._progress(f"result     {status.value}: {reason}")
        return status

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
            )
            results[check.check_id] = result
            status = "ok" if result.ok else f"FAIL (exit {result.exit_code})"
            self._progress(f"check      {check.check_id}: {status}")
        return results

    def _run_verify(self, record: GoalRecord, worktree: Path) -> list[CommandResult]:
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        results = []
        for command in record.contract.verify_commands:
            result = run_shell(
                command, cwd=worktree, monotonic=self._monotonic, timeout_seconds=timeout
            )
            status = "ok" if result.ok else f"FAIL (exit {result.exit_code})"
            self._progress(f"verify     {command}: {status}")
            results.append(result)
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
        pending_review_remediation: list[dict[str, object]] = []
        while True:
            # Owner cancellation is durable: honor it between stages.
            current = self._store.load(state.goal_name)
            if current.status is GoalStatus.CANCELLED:
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

            # Evaluate: acceptance checks are the objective truth.
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
            if not unmet:
                verify_results = self._run_verify(record, worktree)
                verify_failures = [r for r in verify_results if not r.ok]
                state.verify_ok = not verify_failures
                self._save_state(state)
                if not verify_failures:
                    # Commit any uncommitted agent work as the candidate.
                    committed = GitOps.commit_all(
                        worktree, f"goal({state.goal_name}): candidate"
                    )
                    if committed is not None:
                        state.candidate_sha = committed
                    elif state.candidate_sha is None:
                        state.candidate_sha = GitOps.head_sha(worktree)
                    self._save_state(state)

                    if (
                        self._review_required(state, worktree)
                        and state.review_passed_for != state.candidate_sha
                    ):
                        findings = self._review(record, state, worktree)
                        blocking = [
                            f
                            for f in findings
                            if f.get("severity") in ("BLOCKER", "MAJOR")
                        ]
                        if blocking:
                            pending_review_remediation = blocking
                            self._progress(
                                f"finding    {len(blocking)} BLOCKER/MAJOR — "
                                "remediation required"
                            )
                            # falls through to the implementation stage below
                        else:
                            state.review_passed_for = state.candidate_sha
                            self._save_state(state)
                            state.tested_sha = state.candidate_sha
                            return self._finish(
                                record,
                                state,
                                GoalStatus.ACHIEVED,
                                "all acceptance conditions, gates, and review passed",
                            )
                    else:
                        state.tested_sha = state.candidate_sha
                        return self._finish(
                            record,
                            state,
                            GoalStatus.ACHIEVED,
                            "all acceptance conditions and gates passed",
                        )

            # Implement (or remediate): the one writer, in the worktree.
            remediating = bool(verify_failures or pending_review_remediation)
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
                review_findings=pending_review_remediation,
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

            committed = GitOps.commit_all(
                worktree,
                f"goal({state.goal_name}): attempt {state.attempts} ({task_kind})",
            )
            if committed is not None:
                state.candidate_sha = committed
                self._progress(f"candidate  {committed[:12]}")
            # Progress heuristic: fewer unmet checks than before, or any commit,
            # resets the consecutive-failure ladder; no change escalates it.
            after_results = {
                check.check_id: run_shell(
                    check.command,
                    cwd=worktree,
                    monotonic=self._monotonic,
                    timeout_seconds=int(
                        str(self._config.get("goal.agent_timeout_seconds"))
                    ),
                ).ok
                for check in record.contract.checks
                if check.check_id in previously_unmet or not previously_unmet
            }
            improved = any(
                after_results.get(check_id, False) for check_id in state.last_unmet
            )
            if committed is None and GitOps.head_sha(worktree) == head_before:
                state.consecutive_failures += 1
            elif improved or committed is not None:
                state.consecutive_failures = 0
            pending_review_remediation = []
            self._save_state(state)

    def _review(
        self, record: GoalRecord, state: RunState, worktree: Path
    ) -> list[dict[str, object]]:
        preferred_raw = self._config.get("goal.review_resource")
        preferred = str(preferred_raw) if isinstance(preferred_raw, str) else None
        decision = route_task(
            self._catalog, "review", preferred_resource=preferred
        )
        implement_raw = self._config.get("goal.implement_resource")
        if (
            isinstance(implement_raw, str)
            and decision.resource.resource_id == implement_raw
        ):
            self._progress(
                "review     warning: reviewer equals implementer resource; "
                "prefer configuring a distinct goal.review_resource"
            )
        diff = GitOps.diff_text(worktree, state.base_sha)
        prompt = build_review_prompt(record, diff)
        self._progress("review     independent review of candidate "
                       f"{(state.candidate_sha or '')[:12]}")
        output = self._invoke_agent(
            state, decision, prompt, cwd=worktree, write_access=False
        )
        findings: list[dict[str, object]]
        try:
            findings = parse_review_findings(output)
        except SpecError:
            findings = [
                {
                    "severity": "MAJOR",
                    "description": "review output was not parseable JSON; "
                    "treating as a failed review",
                    "file": "",
                }
            ]
        state.review_findings = findings
        self._save_state(state)
        self._store.log_event(
            state.goal_name,
            {"event": "review.finished", "findings": len(findings)},
        )
        return findings


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
