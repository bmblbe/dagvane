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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dagvane.adapters.agents.subprocess_runner import terminate_recorded_process
from dagvane.adapters.localexec import CommandResult, GitOps, run_shell
from dagvane.adapters.worktrees import (
    ManagedWorktreeHandle,
    ManagedWorktrees,
    WorktreePurpose,
    WorktreeSpec,
    validate_worktree_sha,
)
from dagvane.application.goals import (
    GoalRecord,
    GoalStatus,
    GoalStore,
    contract_to_doc,
)
from dagvane.application.localmodel import probe_local_model
from dagvane.application.prepare import baseline_completed, collect_baseline
from dagvane.application.resources import ResourceCatalog, RoutingDecision, route_task
from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError, StorageError
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.agent import AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import Clock, IdSource, Monotonic, parse_iso_ms
from dagvane.protocol.frames import sha256_hex
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.lease import GoalLease
from dagvane.workspace.paths import (
    Workspace,
    atomic_write_json,
    ensure_expected_descendant,
    read_json,
)

Progress = Callable[[str], None]


class ManagedWorktreeLifecycle(Protocol):
    """The immutable managed-worktree lifecycle used by every checkout."""

    def target_path(self, spec: WorktreeSpec) -> Path: ...

    def create(self, spec: WorktreeSpec) -> ManagedWorktreeHandle: ...

    def remove(self, handle: ManagedWorktreeHandle) -> None: ...


class CandidateWorktreeLeaseV2(Protocol):
    """Live candidate lease whose binding is manager-authoritative."""

    @property
    def path(self) -> Path: ...

    @property
    def bound_sha(self) -> str: ...

    @property
    def generation(self) -> str: ...

    @contextmanager
    def pinned_authority(self) -> Iterator[tuple[int, int, tuple[int, int]]]: ...

    def close(self) -> None: ...


@runtime_checkable
class CandidateWorktreeLifecycleV2(Protocol):
    """Required mutable-candidate extension to the managed lifecycle.

    Acquisition is owner/run/purpose based, not raw-path based.  It recovers
    an existing generation without recreating it and returns the manager's
    authoritative ``bound_sha``; a state-file SHA is only an assertion hint.
    Returning ``None`` means the manager proved there is no live generation,
    target, or registration (a durable removed tombstone may remain); a
    collision, corruption, or ambiguous partial lifecycle raises instead.

    A SHA change is two-phase. ``begin_sha_advance`` durably records the old
    binding *before* Git may commit. ``complete_sha_advance`` accepts only the
    same live generation at a clean detached one-child commit of the old SHA,
    then durably binds the new SHA. Recovery of an ``advancing`` generation
    must either complete that exact transition or restore the proven old
    state. This closes both commit→record and record→run-state crash windows.

    Callers test this capability before creating a candidate worktree, so a
    partial/older injected adapter fails closed with no fallback to a raw path,
    ``git worktree prune``, or recursive deletion.
    """

    def acquire_existing(
        self, spec: WorktreeSpec
    ) -> CandidateWorktreeLeaseV2 | None: ...

    def create_candidate(self, spec: WorktreeSpec) -> CandidateWorktreeLeaseV2: ...

    def prove_for_use(
        self,
        handle: CandidateWorktreeLeaseV2,
        *,
        expected_bound_sha: str,
    ) -> Path:
        """Re-prove the live generation immediately around a path effect.

        The manager keeps the lifecycle lease held and proves pinned
        repository/root authority, record nonce/inode, public target inode,
        exact detached registration, and ``expected_bound_sha`` before it
        returns the one canonical path.
        """
        ...

    def begin_sha_advance(
        self,
        handle: CandidateWorktreeLeaseV2,
        *,
        expected_old_sha: str,
    ) -> object: ...

    def complete_sha_advance(
        self,
        handle: CandidateWorktreeLeaseV2,
        token: object,
        *,
        new_sha: str,
    ) -> None: ...


REVIEW_SEVERITIES = ("BLOCKER", "MAJOR", "MINOR", "OPTIONAL")
BLOCKING_SEVERITIES = ("BLOCKER", "MAJOR")

# Independent review is infrastructure: after this many malformed/failed
# review rounds for one run the goal fails closed instead of looping.
_MAX_REVIEW_FAILURES = 2

_EVIDENCE_TAIL_CHARS = 2000


def _validated_resource_id(resource_id: object, *, ctx: str) -> str:
    if not isinstance(resource_id, str) or not resource_id:
        raise SpecError(f"{ctx} must be a non-empty string")
    return resource_id


@dataclass(slots=True)
class RunState:
    """Durable goal-run state: the resume contract.

    ``run_id`` and ``goal_name`` are filesystem-backed identifiers: every
    construction path (direct or parsed from a durable doc) validates them
    with no coercion/normalization, matching ``GoalContract.name`` above.
    """

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
    pending_writer_resource_id: str | None = None
    contributor_resource_ids: list[str] = field(default_factory=list)
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        validate_filesystem_id(self.run_id, ctx="run state: run_id")
        validate_filesystem_id(self.goal_name, ctx="run state: goal_name")
        if self.pending_writer_resource_id == "":
            raise SpecError(
                "run state: pending_writer_resource_id must be a non-empty string "
                "or null"
            )
        if self.pending_writer_resource_id is not None and not isinstance(
            self.pending_writer_resource_id, str
        ):
            raise SpecError(
                "run state: pending_writer_resource_id must be a non-empty string "
                "or null"
            )
        if not isinstance(self.contributor_resource_ids, list):
            raise SpecError(
                "run state: contributor_resource_ids must be a list of strings"
            )
        if any(
            not isinstance(resource_id, str) or not resource_id
            for resource_id in self.contributor_resource_ids
        ):
            raise SpecError(
                "run state: contributor_resource_ids must contain only non-empty "
                "strings"
            )
        if self.contributor_resource_ids != sorted(set(self.contributor_resource_ids)):
            raise SpecError(
                "run state: contributor_resource_ids must be sorted and unique"
            )

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
            "pending_writer_resource_id": self.pending_writer_resource_id,
            "contributor_resource_ids": list(self.contributor_resource_ids),
            "finish_reason": self.finish_reason,
        }

    @staticmethod
    def from_doc(doc: dict[str, Any]) -> RunState:
        # Durable identity/binding fields: a missing key or a malformed type
        # (int/bool/null/container instead of a real string) is corruption
        # and surfaces as a controlled SpecError, never a raw KeyError/
        # TypeError and never a silent str()/None coercion.
        def req_str(key: str) -> str:
            if key not in doc:
                raise SpecError(f"run state: missing required field {key!r}")
            value = doc[key]
            if not isinstance(value, str):
                raise SpecError(
                    f"run state: {key!r} must be a string, got "
                    f"{type(value).__name__}"
                )
            return value

        def opt_str(key: str) -> str | None:
            if key not in doc or doc[key] is None:
                return None
            value = doc[key]
            if not isinstance(value, str):
                raise SpecError(
                    f"run state: {key!r} must be a string or null, got "
                    f"{type(value).__name__}"
                )
            return value

        def opt_sha(key: str) -> str | None:
            value = opt_str(key)
            if value is None:
                return None
            return validate_worktree_sha(value, ctx=f"run state: {key}")

        def opt_resource_id(key: str) -> str | None:
            if key not in doc or doc[key] is None:
                return None
            value = doc[key]
            if not isinstance(value, str) or not value:
                raise SpecError(
                    f"run state: {key!r} must be a non-empty string or null, got "
                    f"{type(value).__name__}"
                )
            return value

        def resource_ids(key: str) -> list[str]:
            if key not in doc:
                return []
            value = doc[key]
            if not isinstance(value, list):
                raise SpecError(
                    f"run state: {key!r} must be a list of non-empty strings, "
                    f"got {type(value).__name__}"
                )
            if any(not isinstance(item, str) or not item for item in value):
                raise SpecError(
                    f"run state: {key!r} must contain only non-empty strings"
                )
            parsed = list(value)
            if parsed != sorted(set(parsed)):
                raise SpecError(
                    f"run state: {key!r} must be sorted and unique"
                )
            return parsed

        candidate_sha = opt_sha("candidate_sha")
        tested_sha = opt_sha("tested_sha")
        review_passed_for = opt_sha("review_passed_for")
        pending_writer_resource_id = opt_resource_id("pending_writer_resource_id")
        contributor_resource_ids = resource_ids("contributor_resource_ids")
        if tested_sha is not None and tested_sha != candidate_sha:
            raise SpecError(
                "run state: tested_sha must equal the persisted candidate_sha"
            )
        if review_passed_for is not None and candidate_sha is None:
            raise SpecError(
                "run state: review_passed_for requires a persisted candidate_sha"
            )
        if (
            review_passed_for is not None
            and review_passed_for == candidate_sha
            and tested_sha is None
        ):
            raise SpecError(
                "run state: review_passed_for cannot mark the current candidate "
                "without matching tested_sha evidence"
            )

        return RunState(
            run_id=req_str("run_id"),
            goal_name=req_str("goal_name"),
            base_sha=req_str("base_sha"),
            status=req_str("status"),
            started_ts=req_str("started_ts"),
            worktree=opt_str("worktree"),
            candidate_sha=candidate_sha,
            tested_sha=tested_sha,
            agent_calls=int(doc.get("agent_calls", 0)),
            attempts=int(doc.get("attempts", 0)),
            consecutive_failures=int(doc.get("consecutive_failures", 0)),
            last_unmet=[str(x) for x in doc.get("last_unmet", [])],
            review_passed_for=review_passed_for,
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
            pending_writer_resource_id=pending_writer_resource_id,
            contributor_resource_ids=contributor_resource_ids,
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
        "- Do not commit, reset, checkout, or otherwise move Git HEAD;",
        "  Dagvane owns the crash-safe candidate commit transition.",
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


@dataclass(slots=True)
class _CandidateWorktreeUse:
    """One live, generation-bound mutable candidate checkout."""

    lifecycle: CandidateWorktreeLifecycleV2
    handle: CandidateWorktreeLeaseV2
    expected_path: Path

    @property
    def path(self) -> Path:
        return self.handle.path

    @property
    def sha(self) -> str:
        return self.handle.bound_sha


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
        worktrees: ManagedWorktreeLifecycle | None = None,
        candidate_worktrees: CandidateWorktreeLifecycleV2 | None = None,
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
        self._worktrees = (
            worktrees
            if worktrees is not None
            else ManagedWorktrees(
                repo_root=workspace.root,
                worktrees_root=workspace.worktrees_dir,
            )
        )
        self._candidate_worktrees = candidate_worktrees
        self._candidate_worktree: _CandidateWorktreeUse | None = None

    # -- durable state -----------------------------------------------------

    def _state_path(self, goal_name: str) -> Path:
        return self._store.run_state_path(goal_name)

    def _save_state(self, record: GoalRecord, state: RunState) -> None:
        """Persist ``state``. The destination directory comes from the
        trusted, already-loaded Goal contract's name — never from
        ``state.goal_name`` (mutable, and only as trustworthy as whatever
        wrote the durable state) — and ``state`` is re-bound to that exact
        contract (identity, base SHA, worktree) immediately before writing
        a single byte. A state whose identity field was changed underneath
        it (e.g. to a different goal) fails closed here and writes nowhere,
        neither at the expected nor at any other Goal directory."""
        expected_name = record.contract.name
        self._bind_state(expected_name, record, state)
        atomic_write_json(
            self._state_path(expected_name),
            state.to_doc(),
            allowed_root=self._workspace.goals_dir,
        )

    def _read_run_state(self, goal_name: str) -> RunState | None:
        path = self._state_path(goal_name)
        if path.is_symlink():
            raise StorageError(f"cannot read {path}: refusing to follow a symlink")
        if not path.exists():
            return None
        return RunState.from_doc(
            read_json(path, allowed_root=self._workspace.goals_dir)
        )

    def _expected_worktree_path(
        self, goal_name: str, run_id: str, sha: str
    ) -> Path:
        """The one deterministic writer-worktree path for a bound run: never
        derived from a persisted string.  The managed lifecycle derives it
        from a complete, validated candidate spec; this helper only asserts
        the path when binding durable run-state and never grants lifecycle or
        cleanup authority."""
        expected = self._worktrees.target_path(
            WorktreeSpec(
                goal_name=goal_name,
                run_id=run_id,
                purpose=WorktreePurpose.CANDIDATE,
                sha=sha,
            )
        )
        ensure_expected_descendant(self._workspace.worktrees_dir, expected)
        return expected

    def _bind_state(
        self, requested_name: str, record: GoalRecord, state: RunState
    ) -> None:
        """The single validation path every state load goes through: a
        loaded state is usable only when the requested Goal name, the
        durable contract identity, the run-state's own identity, the frozen
        contract base SHA, and any persisted worktree claim are exactly
        equal — byte-for-byte, no case-folding or coercion. A valid-but-
        different, case-different, or wrong-type internal value is
        corruption and fails closed before any lease/reconciliation/
        process/Git/shell/agent effect uses the state."""
        if requested_name != record.contract.name:
            raise SpecError(
                f"goal {requested_name!r}: durable contract identity does not "
                "match the requested Goal"
            )
        if state.goal_name != requested_name:
            raise SpecError(
                f"goal {requested_name!r}: run-state.json goal_name "
                f"{state.goal_name!r} does not match the requested Goal — "
                "refusing a tampered or foreign run state"
            )
        if state.base_sha != record.contract.base_sha:
            raise SpecError(
                f"goal {requested_name!r}: run-state.json base_sha "
                f"{state.base_sha!r} does not match the frozen contract "
                f"base_sha {record.contract.base_sha!r}"
            )
        candidate_sha = state.candidate_sha or record.contract.base_sha
        expected_worktree = self._expected_worktree_path(
            state.goal_name, state.run_id, candidate_sha
        )
        if state.worktree is not None and state.worktree != str(expected_worktree):
            raise SpecError(
                f"goal {requested_name!r}: run-state.json worktree "
                f"{state.worktree!r} does not match the deterministic "
                f"expected path {expected_worktree}"
            )

    def _load_bound_state(
        self, goal_name: str, record: GoalRecord
    ) -> RunState | None:
        state = self._read_run_state(goal_name)
        if state is None:
            return None
        self._bind_state(goal_name, record, state)
        return state

    def load_state(self, goal_name: str) -> RunState | None:
        """Load the durable run-state, bound to the exact requested Goal
        identity, frozen contract base SHA, and deterministic worktree path
        (see ``_bind_state``). This is the one loader every caller — CLI
        ``show``, ``start``, and ``resume`` — uses, so pure display can
        never print a mismatched state and start/resume can never build on
        one."""
        record = self._store.load(goal_name)
        return self._load_bound_state(goal_name, record)

    def process_record_path(self, goal_name: str) -> Path:
        """Where the in-flight agent process identity is persisted."""
        return self._store.agent_process_path(goal_name)

    @property
    def process_record_root(self) -> Path:
        """The Goal authority root that every process-record path is
        validated against."""
        return self._workspace.goals_dir

    # -- entry points ------------------------------------------------------

    def _probe_local_model(self) -> None:
        """The Ollama availability probe: only ever called from inside the
        held lease, after the under-lease Goal/RunState re-read, re-bind,
        and every applicable status check have all passed — so a mutation
        injected between the preflight read and the moment the lease is
        actually acquired (caught by that re-read/re-bind) leaves the probe
        call count at zero, exactly like every other lease-body effect.
        Lives on the application runner (not the CLI) so there is exactly
        one load/bind path, never a duplicate, racy CLI-side one."""
        if bool(self._config.get("router.local_enabled")):
            available = probe_local_model(self._catalog)
            self._progress(
                f"local      ollama {'available' if available else 'unavailable'}"
            )

    def start(self, goal_name: str) -> GoalStatus:
        # Preflight: validate/load the exact requested Goal, and bind any
        # existing durable run-state to it, with no lease/side effect. A
        # corrupt existing state must not be hidden by starting a new run.
        # The new run identity is generated and canonically validated here
        # too — still pure preflight, still before the lease — so an
        # invalid generated id leaves no lease or any other effect; if
        # another run wins the lease below, this id is simply discarded,
        # never persisted. Once the lease is actually held, Goal/state are
        # re-read and re-bound from scratch before any lease-body effect
        # (including the availability probe) uses them — this closes the
        # preflight-to-lease race, not just the initial gap.
        preflight_record = self._store.load(goal_name)
        self._load_bound_state(goal_name, preflight_record)
        run_id = self._ids.new_id("goalrun")
        validate_filesystem_id(run_id, ctx="run state: run_id")
        lease = GoalLease(
            self._store.lease_path(goal_name), allowed_root=self._workspace.goals_dir
        )
        lease.acquire(owner=f"goal-start:{goal_name}")
        try:
            record = self._store.load(goal_name)
            existing = self._load_bound_state(goal_name, record)
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
            self._probe_local_model()
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
                    expected_name=goal_name,
                    monotonic=self._monotonic,
                    progress=self._progress,
                )
            state = RunState(
                run_id=run_id,
                goal_name=goal_name,
                base_sha=record.contract.base_sha,
                status="running",
                started_ts=self._clock.now_iso(),
            )
            self._save_state(record, state)
            record.status = GoalStatus.RUNNING
            self._store.save(goal_name, record)
            self._store.log_event(
                goal_name, {"event": "run.started", "run_id": state.run_id}
            )
            return self._loop(record, state)
        finally:
            lease.release()

    def resume(self, goal_name: str) -> GoalStatus:
        # Preflight: validate/load the exact requested Goal and bind its
        # durable run-state with no lease/side effect, then reload/rebind
        # both again once the lease is actually held (mirrors `start`) —
        # before reconciliation, orphan termination, status/evidence/log
        # writes, the availability probe, or any Git/shell/agent work. This
        # closes both the initial effect gap and the preflight-to-lease
        # race.
        preflight_record = self._store.load(goal_name)
        self._load_bound_state(goal_name, preflight_record)
        lease = GoalLease(
            self._store.lease_path(goal_name), allowed_root=self._workspace.goals_dir
        )
        lease.acquire(owner=f"goal-resume:{goal_name}")
        try:
            record = self._store.load(goal_name)
            state = self._load_bound_state(goal_name, record)
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
                self._store.save(goal_name, record)
                self._store.log_event(
                    goal_name,
                    {"event": "run.reconciled", "repair": "approved->running"},
                )
                self._progress(
                    "reconcile  goal repaired to running (crash between start writes)"
                )
            elif record.status is GoalStatus.CANCELLED:
                self._reap_orphan_writer(goal_name)
                # A crash may have occurred after the candidate manager's
                # durable advance began (or completed) but before run-state
                # captured its authoritative SHA. Recover an existing
                # generation before terminalizing cancellation; do not create
                # a new candidate solely for a cancelled run.
                attempt_base_sha = state.candidate_sha or record.contract.base_sha
                candidate = self._open_candidate_worktree(
                    record, state, create_if_missing=False
                )
                if candidate is not None:
                    self._reconcile_pending_attempt(
                        record,
                        state,
                        candidate,
                        attempt_base_sha=attempt_base_sha,
                        enforce_unattributed=False,
                    )
                    self._close_candidate_worktree()
                return self._finish(
                    record, state, GoalStatus.CANCELLED, "cancelled by owner"
                )
            elif record.status is not GoalStatus.RUNNING:
                raise SpecError(
                    f"goal {goal_name!r} is {record.status.value}; run "
                    f"{state.run_id} cannot resume"
                )
            self._reap_orphan_writer(goal_name)
            attempt_base_sha = state.candidate_sha or record.contract.base_sha
            candidate = self._open_candidate_worktree(record, state)
            assert candidate is not None
            try:
                # Candidate recovery/state reconciliation precedes optional
                # availability probes and resume logging. No effect can use a
                # stale RunState SHA after an interrupted managed advance.
                self._reconcile_pending_attempt(
                    record,
                    state,
                    candidate,
                    attempt_base_sha=attempt_base_sha,
                )
                self._probe_local_model()
                self._store.log_event(
                    goal_name, {"event": "run.resumed", "run_id": state.run_id}
                )
                self._progress(f"resume     run {state.run_id} from durable state")
                return self._loop_with_candidate(record, state, candidate)
            finally:
                self._close_candidate_worktree()
        finally:
            lease.release()

    # -- reconciliation helpers ---------------------------------------------

    def _reap_orphan_writer(self, goal_name: str) -> None:
        """Terminate an in-flight agent process left by a crashed run."""
        if terminate_recorded_process(
            self.process_record_path(goal_name), allowed_root=self._workspace.goals_dir
        ):
            self._progress(
                "reconcile  terminated an orphaned in-flight agent process "
                "from a previous run"
            )
            self._store.log_event(goal_name, {"event": "run.orphan_reaped"})

    def _cancel_requested(self, goal_name: str) -> bool:
        return self._store.load(goal_name).status is GoalStatus.CANCELLED

    def _reconcile_pending_attempt(
        self,
        record: GoalRecord,
        state: RunState,
        use: _CandidateWorktreeUse,
        *,
        attempt_base_sha: str,
        enforce_unattributed: bool = True,
    ) -> None:
        """Reconcile an interrupted writer attempt before resuming work.

        ``attempt_base_sha`` is captured before managed lifecycle recovery. It
        lets this method distinguish a recovered commit made by the pending
        attempt from a clean no-op on an already-advanced candidate.
        """
        worktree = self._prove_candidate_for_use(use)
        with self._pinned_git_authority(use.handle) as _identity:
            clean = GitOps.is_clean(worktree)
            head = GitOps.head_sha(worktree)

        pending = state.pending_writer_resource_id
        if pending is not None:
            if not clean:
                # The manager's live binding is authoritative. Reset to that
                # binding, not to a persisted/stale pathname or SHA, then
                # remove ignored/untracked bytes as part of the same discard.
                with self._pinned_git_authority(use.handle) as _identity:
                    GitOps.reset_hard_and_clean(worktree, use.sha)
                    if GitOps.head_sha(worktree) != use.sha or not GitOps.is_clean(
                        worktree
                    ):
                        raise StorageError(
                            "pending writer bytes could not be discarded cleanly"
                        )
                if state.candidate_sha != use.sha:
                    state.candidate_sha = use.sha
                    state.tested_sha = None
                    state.verification = None
                    state.verify_ok = None
                state.pending_writer_resource_id = None
                self._save_state(record, state)
                return

            if head != attempt_base_sha:
                if state.candidate_sha != use.sha:
                    state.candidate_sha = use.sha
                    state.tested_sha = None
                    state.verification = None
                    state.verify_ok = None
                state.contributor_resource_ids = sorted(
                    {*state.contributor_resource_ids, pending}
                )
                state.implement_resource_id = pending
            state.pending_writer_resource_id = None
            self._save_state(record, state)
            return

        if not enforce_unattributed:
            if head != attempt_base_sha or state.candidate_sha != use.sha:
                state.candidate_sha = use.sha
                state.tested_sha = None
                state.verification = None
                state.verify_ok = None
                self._save_state(record, state)
            return
        if not clean:
            raise StorageError(
                "candidate has uncommitted bytes without a pending writer "
                "attribution"
            )
        if head != attempt_base_sha:
            raise StorageError(
                "candidate HEAD advanced without a pending writer attribution; "
                "refusing to continue with an unattributable commit"
            )
        if head != record.contract.base_sha and not state.contributor_resource_ids:
            raise StorageError(
                "candidate commit is not covered by the durable contributor set"
            )

    # -- the fixed workflow ------------------------------------------------

    def _finish(
        self, record: GoalRecord, state: RunState, status: GoalStatus, reason: str
    ) -> GoalStatus:
        state.status = status.value
        state.finish_reason = reason
        self._save_state(record, state)
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
        self._store.save(state.goal_name, record)
        self._store.log_event(
            state.goal_name,
            {"event": "run.finished", "status": status.value, "reason": reason},
        )

    def _wall_seconds(self, state: RunState) -> int:
        started = parse_iso_ms(state.started_ts)
        now = parse_iso_ms(self._clock.now_iso())
        return int((now - started).total_seconds())

    def _candidate_lifecycle(self) -> CandidateWorktreeLifecycleV2:
        """Require V2 before any candidate filesystem/Git/writer effect."""
        lifecycle: object = (
            self._candidate_worktrees
            if self._candidate_worktrees is not None
            else self._worktrees
        )
        if not isinstance(lifecycle, CandidateWorktreeLifecycleV2):
            raise SpecError(
                "candidate worktrees require managed lifecycle V2 "
                "acquire/create plus two-phase SHA advance; "
                "the configured adapter lacks that fail-closed capability"
            )
        return lifecycle

    def _open_candidate_worktree(
        self,
        record: GoalRecord,
        state: RunState,
        *,
        create_if_missing: bool = True,
    ) -> _CandidateWorktreeUse | None:
        """Create/recover and retain the one managed candidate generation."""
        lifecycle = self._candidate_lifecycle()
        sha = state.candidate_sha or record.contract.base_sha
        spec = WorktreeSpec(
            goal_name=state.goal_name,
            run_id=state.run_id,
            purpose=WorktreePurpose.CANDIDATE,
            sha=sha,
        )
        handle = lifecycle.acquire_existing(spec)
        if handle is None:
            if not create_if_missing:
                return None
            if state.worktree is not None or state.candidate_sha is not None:
                raise StorageError(
                    "candidate manager proved no generation exists, but durable "
                    "run state asserts a prior candidate; refusing to recreate "
                    "from state-selected SHA"
                )
            create_spec = WorktreeSpec(
                goal_name=state.goal_name,
                run_id=state.run_id,
                purpose=WorktreePurpose.CANDIDATE,
                sha=record.contract.base_sha,
            )
            handle = lifecycle.create_candidate(create_spec)
        try:
            expected_path = self._expected_worktree_path(
                state.goal_name, state.run_id, handle.bound_sha
            )
            use = _CandidateWorktreeUse(
                lifecycle=lifecycle,
                handle=handle,
                expected_path=expected_path,
            )
            self._candidate_worktree = use
            if handle.path != expected_path:
                raise StorageError(
                    f"candidate lifecycle returned path {handle.path}, expected "
                    f"the canonical managed target {expected_path}"
                )
            with self._pinned_git_authority(handle) as _identity:
                head = GitOps.head_sha(handle.path)
            if head != handle.bound_sha:
                raise StorageError(
                    f"candidate lifecycle returned checkout HEAD {head}, but "
                    f"its authoritative binding is {handle.bound_sha}"
                )
            self._prove_candidate_for_use(use)
            # This persisted string is an assertion/evidence field only.  The
            # live generation handle, never this path, authorizes all use and
            # removal.
            if state.worktree != str(handle.path):
                state.worktree = str(handle.path)
            # Manager truth wins during crash recovery. Persist it before any
            # check/shell/agent effect; the prior state SHA is only an
            # assertion supplied to acquisition, never lifecycle authority.
            prior_candidate_sha = state.candidate_sha or record.contract.base_sha
            if (
                handle.bound_sha == record.contract.base_sha
                and prior_candidate_sha != record.contract.base_sha
            ):
                # The candidate base has been deliberately reset. Provenance
                # belongs to the old candidate and must not leak into this
                # fresh base.
                state.contributor_resource_ids = []
            candidate_sha_changed = state.candidate_sha != handle.bound_sha
            if not candidate_sha_changed:
                self._save_state(record, state)
            else:
                # Do not overwrite the pre-attempt candidate SHA before
                # resume reconciliation can distinguish a recovered commit
                # from a clean no-op. The reconciliation helper persists the
                # manager's newer binding together with its attribution (or
                # the fail-closed result).
                state.tested_sha = None
                state.verification = None
                state.verify_ok = None
            self._progress(
                f"worktree   {handle.path} @ {handle.bound_sha[:12]} (managed)"
            )
            return use
        except BaseException:
            if self._candidate_worktree is not None:
                self._close_candidate_worktree()
            else:
                handle.close()
            raise

    def _close_candidate_worktree(self) -> None:
        use = self._candidate_worktree
        if use is None:
            return
        self._candidate_worktree = None
        # Candidate evidence is retained: releasing the lease never removes
        # its checkout, Git registration, owner record, or commit anchor.
        use.handle.close()

    def _prove_candidate_for_use(
        self,
        use: _CandidateWorktreeUse,
        *,
        expected_sha: str | None = None,
    ) -> Path:
        """Manager proof plus application assertions around each path effect."""
        sha = expected_sha if expected_sha is not None else use.sha
        path = use.lifecycle.prove_for_use(
            use.handle,
            expected_bound_sha=sha,
        )
        if path != use.expected_path or path != use.handle.path:
            raise StorageError(
                f"candidate use proof returned path {path}, expected held "
                f"canonical target {use.expected_path}"
            )
        ensure_expected_descendant(self._workspace.worktrees_dir, path)
        with self._pinned_git_authority(use.handle) as _identity:
            head = GitOps.head_sha(path)
        if head != sha or use.sha != sha:
            raise StorageError(
                f"candidate use proof disagrees: checkout HEAD={head}, "
                f"handle bound_sha={use.sha}, expected={sha}"
            )
        return path

    @staticmethod
    @contextmanager
    def _pinned_git_authority(
        handle: CandidateWorktreeLeaseV2 | ManagedWorktreeHandle,
    ) -> Iterator[tuple[int, int]]:
        """Bind candidate Git's work tree and common directory to fds."""
        with handle.pinned_authority() as (worktree_fd, _common_fd, identity):
            # Candidate Git must retain the linked worktree's own ``.git``
            # file/HEAD authority.  Supplying the common repository as
            # ``--git-dir`` would instead address the main checkout's HEAD.
            # The candidate fd is still the kernel-bound cwd authority and
            # the lifecycle keeps the repository/common identities pinned.
            with GitOps.pinned_worktree_authority(worktree_fd, None):
                yield identity

    def _commit_candidate(
        self,
        record: GoalRecord,
        state: RunState,
        use: _CandidateWorktreeUse,
        message: str,
    ) -> tuple[str, bool]:
        """Two-phase managed commit followed by attributed state persistence."""
        old_sha = use.sha
        worktree = self._prove_candidate_for_use(use, expected_sha=old_sha)
        with self._pinned_git_authority(use.handle) as _identity:
            head_before = GitOps.head_sha(worktree)
        if head_before != old_sha:
            raise StorageError(
                f"candidate worktree HEAD moved to {head_before} outside a "
                f"managed SHA advance; expected {old_sha}"
            )
        with self._pinned_git_authority(use.handle) as _identity:
            clean = GitOps.is_clean(worktree)
        if clean:
            candidate = old_sha
            advanced = False
        else:
            if state.pending_writer_resource_id is None:
                raise StorageError(
                    "candidate has uncommitted bytes without a pending writer "
                    "attribution"
                )
            token = use.lifecycle.begin_sha_advance(
                use.handle, expected_old_sha=old_sha
            )
            with self._pinned_git_authority(use.handle) as _identity:
                worktree = self._prove_candidate_for_use(
                    use, expected_sha=old_sha
                )
                committed = GitOps.commit_all(worktree, message)
                if committed is None:
                    raise StorageError(
                        "candidate became clean without producing the one commit "
                        "required by its durable SHA-advance token"
                    )
                candidate = GitOps.head_sha(worktree)
            use.lifecycle.complete_sha_advance(
                use.handle, token, new_sha=candidate
            )
            if use.sha != candidate:
                raise StorageError(
                    "candidate lifecycle completed SHA advance but its live "
                    "handle did not expose the authoritative new SHA"
                )
            self._prove_candidate_for_use(use, expected_sha=candidate)
            advanced = True
        if not advanced:
            self._prove_candidate_for_use(use, expected_sha=candidate)
        candidate_changed = state.candidate_sha != candidate
        had_pending = state.pending_writer_resource_id is not None
        if advanced:
            pending = state.pending_writer_resource_id
            if pending is None:
                raise StorageError(
                    "candidate advanced without a pending writer attribution"
                )
            state.contributor_resource_ids = sorted(
                {*state.contributor_resource_ids, pending}
            )
            state.implement_resource_id = pending
            state.pending_writer_resource_id = None
        elif state.pending_writer_resource_id is not None:
            # A no-op attempt is not a contribution. Preserve all prior
            # contributors while clearing only this attempt's pending marker.
            state.pending_writer_resource_id = None
        if candidate_changed:
            state.candidate_sha = candidate
            state.tested_sha = None
            state.verification = None
            state.verify_ok = None
        if candidate_changed or advanced or had_pending:
            # Candidate advancement and pending clearing are persisted in the
            # same atomic state write.
            self._save_state(record, state)
        return candidate, advanced

    def _create_immutable_worktree(
        self, spec: WorktreeSpec
    ) -> ManagedWorktreeHandle:
        """Create one exact immutable generation and assert its returned path."""
        expected = self._worktrees.target_path(spec)
        ensure_expected_descendant(self._workspace.worktrees_dir, expected)
        handle = self._worktrees.create(spec)
        try:
            if handle.path != expected:
                raise StorageError(
                    f"managed {spec.purpose.value} lifecycle returned path "
                    f"{handle.path}, expected canonical target {expected}"
                )
            ensure_expected_descendant(self._workspace.worktrees_dir, handle.path)
            with self._pinned_git_authority(handle) as _identity:
                head = GitOps.head_sha(handle.path)
            if head != spec.sha:
                raise StorageError(
                    f"managed {spec.purpose.value} lifecycle returned checkout "
                    f"HEAD {head}, expected exact SHA {spec.sha}"
                )
            return handle
        except BaseException:
            # A mismatched handle is not cleanup authority for the expected
            # target. Release its lease but preserve whatever generation the
            # adapter actually returned for investigation/recovery.
            handle.close()
            raise

    def _run_candidate_shell(
        self,
        command: str,
        candidate_worktree: _CandidateWorktreeUse,
        *,
        timeout_seconds: int,
    ) -> CommandResult:
        worktree = self._prove_candidate_for_use(candidate_worktree)
        try:
            with candidate_worktree.handle.pinned_authority() as (
                _worktree_fd,
                _common_fd,
                identity,
            ):
                return run_shell(
                    command,
                    cwd=worktree,
                    cwd_identity=identity,
                    monotonic=self._monotonic,
                    timeout_seconds=timeout_seconds,
                    scrubber=self._scrubber,
                )
        finally:
            self._prove_candidate_for_use(candidate_worktree)

    def _run_checks(
        self, record: GoalRecord, candidate_worktree: _CandidateWorktreeUse
    ) -> dict[str, CommandResult]:
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        results: dict[str, CommandResult] = {}
        for check in record.contract.checks:
            result = self._run_candidate_shell(
                check.command,
                candidate_worktree,
                timeout_seconds=timeout,
            )
            results[check.check_id] = result
            status = "ok" if result.ok else f"FAIL (exit {result.exit_code})"
            self._progress(f"check      {check.check_id}: {status}")
        return results

    def _review_required(
        self, state: RunState, candidate_worktree: _CandidateWorktreeUse
    ) -> bool:
        policy = str(self._config.get("goal.review_policy"))
        if policy == "never":
            return False
        if policy == "always":
            return True
        worktree = self._prove_candidate_for_use(candidate_worktree)
        try:
            with self._pinned_git_authority(candidate_worktree.handle) as _identity:
                changed = GitOps.changed_files(worktree, state.base_sha)
        finally:
            self._prove_candidate_for_use(candidate_worktree)
        return len(changed) >= 2  # "substantial": multi-file change

    def _invoke_agent(
        self,
        record: GoalRecord,
        state: RunState,
        decision: RoutingDecision,
        prompt: str,
        *,
        cwd: Path,
        write_access: bool,
        candidate_worktree: _CandidateWorktreeUse | None = None,
    ) -> str:
        resource = decision.resource
        state.routing_log.append(decision.reason)
        self._progress(
            f"router     {resource.resource_id} ({resource.model or resource.runtime}"
            f"/{resource.reasoning or '-'}) — {decision.reason}"
        )
        state.agent_calls += 1
        self._save_state(record, state)
        timeout = int(str(self._config.get("goal.agent_timeout_seconds")))
        if candidate_worktree is not None:
            proven_cwd = self._prove_candidate_for_use(candidate_worktree)
            if cwd != proven_cwd:
                raise StorageError(
                    f"candidate agent cwd {cwd} does not match current managed "
                    f"proof {proven_cwd}"
                )
            cwd = proven_cwd
        try:
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
                    process_record_root=self._workspace.goals_dir,
                )
            )
        finally:
            if candidate_worktree is not None:
                self._prove_candidate_for_use(candidate_worktree)
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
        candidate = self._open_candidate_worktree(record, state)
        assert candidate is not None  # create_if_missing defaults to True
        try:
            return self._loop_with_candidate(record, state, candidate)
        finally:
            # Release the lease on every return/exception while preserving the
            # owned checkout and commit anchor as durable owner evidence.
            self._close_candidate_worktree()

    def _loop_with_candidate(
        self,
        record: GoalRecord,
        state: RunState,
        candidate_worktree: _CandidateWorktreeUse,
    ) -> GoalStatus:
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

            # Evaluate: acceptance checks in the writer worktree are the
            # progress signal; ACHIEVED is decided only by the immutable
            # verification of the committed candidate below.
            check_results = self._run_checks(record, candidate_worktree)
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
            self._save_state(record, state)

            verify_failures: list[CommandResult] = []
            blocking: list[dict[str, object]] = []
            if not unmet:
                evaluation = self._evaluate_candidate(
                    record, state, candidate_worktree
                )
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
            writer_resource_id = _validated_resource_id(
                decision.resource.resource_id, ctx="implementation writer resource_id"
            )
            worktree = self._prove_candidate_for_use(candidate_worktree)
            try:
                with self._pinned_git_authority(candidate_worktree.handle) as _identity:
                    head_before = GitOps.head_sha(worktree)
            finally:
                self._prove_candidate_for_use(candidate_worktree)
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
            # This is the durable attribution for the in-flight attempt. It
            # must exist before _invoke_agent can produce any worktree bytes.
            state.pending_writer_resource_id = writer_resource_id
            self._save_state(record, state)
            try:
                self._invoke_agent(
                    record,
                    state,
                    decision,
                    prompt,
                    cwd=worktree,
                    write_access=True,
                    candidate_worktree=candidate_worktree,
                )
            except SpecError as exc:
                self._reconcile_pending_attempt(
                    record,
                    state,
                    candidate_worktree,
                    attempt_base_sha=candidate_worktree.sha,
                )
                state.consecutive_failures += 1
                self._save_state(record, state)
                self._store.log_event(
                    state.goal_name, {"event": "attempt.failed", "error": str(exc)}
                )
                continue

            # Never commit post-cancel work: the writer may have been
            # terminated by `goal cancel`; its partial bytes stay uncommitted.
            if self._cancel_requested(state.goal_name):
                self._reconcile_pending_attempt(
                    record,
                    state,
                    candidate_worktree,
                    attempt_base_sha=candidate_worktree.sha,
                    enforce_unattributed=False,
                )
                return self._finish(
                    record,
                    state,
                    GoalStatus.CANCELLED,
                    "cancelled by owner during implementation; "
                    "uncommitted work discarded",
                )

            candidate, advanced = self._commit_candidate(
                record,
                state,
                candidate_worktree,
                f"goal({state.goal_name}): attempt {state.attempts} ({task_kind})",
            )
            if advanced:
                self._progress(f"candidate  {candidate[:12]}")

            # Progress accounting drives escalation: only objective progress
            # resets the ladder. A previously-unmet check newly passing counts;
            # verification newly passing counts (reset inside the immutable
            # stage). A commit alone — however large — never resets: an agent
            # producing irrelevant commits must still escalate.
            after_results = {
                check.check_id: self._run_candidate_shell(
                    check.command,
                    candidate_worktree,
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
            if improved:
                state.consecutive_failures = 0
            elif state.last_unmet:
                state.consecutive_failures += 1
            elif not advanced:
                worktree = self._prove_candidate_for_use(candidate_worktree)
                try:
                    with self._pinned_git_authority(
                        candidate_worktree.handle
                    ) as _identity:
                        head_unchanged = GitOps.head_sha(worktree) == head_before
                finally:
                    self._prove_candidate_for_use(candidate_worktree)
                if head_unchanged:
                    # Gate/review remediation that changed nothing: no progress.
                    state.consecutive_failures += 1
            # else: gate/review remediation produced a new candidate — its
            # verdict comes from the next immutable verification, not from
            # the mere existence of a commit.
            self._save_state(record, state)

    # -- immutable candidate evaluation --------------------------------------

    def _evaluate_candidate(
        self,
        record: GoalRecord,
        state: RunState,
        candidate_worktree: _CandidateWorktreeUse,
    ) -> _CandidateEvaluation:
        # Freeze: commit any uncommitted writer work. The candidate SHA is
        # derived from Git every time — never trusted from stale run-state.
        candidate, advanced = self._commit_candidate(
            record,
            state,
            candidate_worktree,
            f"goal({state.goal_name}): candidate",
        )
        if advanced:
            self._progress(f"candidate  {candidate[:12]} (frozen)")

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
                self._save_state(record, state)
                return _CandidateEvaluation(verify_failures=failures)
            state.verify_ok = True
            state.tested_sha = candidate
            state.consecutive_failures = 0  # objective progress
            self._save_state(record, state)

        if (
            self._review_required(state, candidate_worktree)
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
            self._save_state(record, state)
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
        spec = WorktreeSpec(
            goal_name=state.goal_name,
            run_id=state.run_id,
            purpose=WorktreePurpose.VERIFY,
            sha=candidate,
        )
        handle = self._create_immutable_worktree(spec)
        worktree = handle.path
        self._progress(
            f"verify-wt  fresh disposable worktree @ {candidate[:12]} "
            "(immutable candidate)"
        )
        results: list[CommandResult] = []
        evidence: list[dict[str, object]] = []
        try:
            labelled = [
                (f"check:{check.check_id}", check.command)
                for check in record.contract.checks
            ] + [("verify", command) for command in record.contract.verify_commands]
            for label, command in labelled:
                with handle.pinned_authority() as (
                    _worktree_fd,
                    _common_fd,
                    identity,
                ):
                    result = run_shell(
                        command,
                        cwd=worktree,
                        cwd_identity=identity,
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
            with self._pinned_git_authority(handle) as _identity:
                mutated = GitOps.tracked_dirty(worktree)
                head_after = GitOps.head_sha(worktree)
        finally:
            # Cleanup authority is the still-live generation handle.  A
            # failure is explicit and leaves the durable owner generation for
            # managed recovery; it is never downgraded to best-effort success.
            self._worktrees.remove(handle)
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
        self._save_state(record, state)
        return ok, [result for result in results if not result.ok]

    # -- exact-SHA independent review ----------------------------------------

    def _record_review_failure(
        self, record: GoalRecord, state: RunState, entry: dict[str, object], error: str
    ) -> None:
        entry["error"] = error
        state.review_failures += 1
        state.reviews.append(entry)
        self._save_state(record, state)
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
        contributor_ids = state.contributor_resource_ids
        if not isinstance(contributor_ids, list) or any(
            not isinstance(resource_id, str) or not resource_id
            for resource_id in contributor_ids
        ) or contributor_ids != sorted(set(contributor_ids)):
            raise SpecError(
                "run state: contributor_resource_ids is not a validated list"
            )
        excluded = set(contributor_ids)
        implement_raw = self._config.get("goal.implement_resource")
        if isinstance(implement_raw, str) and implement_raw:
            excluded.add(implement_raw)
        reviewer_resource_id = _validated_resource_id(
            decision.resource.resource_id, ctx="reviewer resource_id"
        )
        if reviewer_resource_id in excluded:
            return (
                "fatal",
                [],
                f"reviewer resource {reviewer_resource_id!r} is in the "
                "candidate contributor set; independent review requires a "
                "distinct resource outside the candidate's contributor set "
                "(configure goal.review_resource)",
            )
        spec = WorktreeSpec(
            goal_name=state.goal_name,
            run_id=state.run_id,
            purpose=WorktreePurpose.REVIEW,
            sha=candidate,
        )
        handle = self._create_immutable_worktree(spec)
        worktree = handle.path
        entry: dict[str, object] = {
            "ts": self._clock.now_iso(),
            "candidate_sha": candidate,
            "base_sha": state.base_sha,
            "resource": reviewer_resource_id,
            "valid": False,
            "findings": [],
        }
        try:
            with self._pinned_git_authority(handle) as _identity:
                head_before = GitOps.head_sha(worktree)
            entry["head_before"] = head_before
            if head_before != candidate:
                self._record_review_failure(
                    record,
                    state,
                    entry,
                    f"review checkout HEAD {head_before} != candidate {candidate}",
                )
                return "infra", [], "review checkout integrity failure"
            with self._pinned_git_authority(handle) as _identity:
                diff_sha = GitOps.diff_sha256(worktree, state.base_sha)
            entry["diff_sha256"] = diff_sha
            with self._pinned_git_authority(handle) as _identity:
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
                    record, state, decision, prompt, cwd=worktree, write_access=False
                )
            except SpecError as exc:
                self._record_review_failure(
                    record, state, entry, f"review agent failed: {exc}"
                )
                return "infra", [], str(exc)
            with self._pinned_git_authority(handle) as _identity:
                head_after = GitOps.head_sha(worktree)
            entry["head_after"] = head_after
            entry["head_verified"] = head_after == candidate
            if head_after != candidate:
                self._record_review_failure(
                    record,
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
                    record, state, entry, f"unparseable reviewer output: {exc}"
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
            self._save_state(record, state)
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
            self._worktrees.remove(handle)


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
