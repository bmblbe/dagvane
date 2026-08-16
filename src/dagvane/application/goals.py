"""Durable software-development Goal contracts (Autonomous Developer MVP).

A Goal binds an objective to an exact base SHA, explicit non-goals, and
*objective* acceptance checks (shell commands whose exit status decides).
After approval the contract is frozen by hash: implementation workers cannot
silently weaken it — an invalid approved condition must be recorded as
``CONTRACT_AMENDMENT_REQUIRED``, never edited in place.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.runtime import Clock
from dagvane.protocol.frames import canonical_json_bytes, sha256_hex
from dagvane.workspace.paths import (
    Workspace,
    append_jsonl,
    atomic_write_json,
    ensure_expected_descendant,
    read_json,
)


class GoalStatus(StrEnum):
    DRAFT = "draft"
    PREPARED = "prepared"
    APPROVED = "approved"
    RUNNING = "running"
    ACHIEVED = "achieved"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    FAILED = "failed"


GOAL_TERMINAL: frozenset[GoalStatus] = frozenset(
    {
        GoalStatus.ACHIEVED,
        GoalStatus.BLOCKED,
        GoalStatus.BUDGET_EXHAUSTED,
        GoalStatus.CANCELLED,
        GoalStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    """One objective acceptance condition: a command run in the candidate."""

    check_id: str
    description: str
    command: str  # shell command; exit 0 = condition met


@dataclass(frozen=True, slots=True)
class GoalLimits:
    max_wall_seconds: int
    max_agent_calls: int
    max_attempts: int
    max_consecutive_failures: int


@dataclass(slots=True)
class GoalContract:
    name: str
    base_sha: str
    objective: str
    must_have: list[str]
    non_goals: list[str]
    checks: list[AcceptanceCheck]
    verify_commands: list[str]  # global gates (pytest/ruff/mypy equivalents)
    limits: GoalLimits

    def __post_init__(self) -> None:
        # Single enforcement point: every construction path (direct, parsed
        # from a durable doc, or parsed from a preparation agent's output)
        # goes through this dataclass, so the filesystem-ID contract is
        # applied uniformly with no coercion/normalization.
        validate_filesystem_id(self.name, ctx="goal contract: name")


def contract_to_doc(contract: GoalContract) -> dict[str, object]:
    return {
        "name": contract.name,
        "base_sha": contract.base_sha,
        "objective": contract.objective,
        "must_have": list(contract.must_have),
        "non_goals": list(contract.non_goals),
        "checks": [
            {
                "check_id": check.check_id,
                "description": check.description,
                "command": check.command,
            }
            for check in contract.checks
        ],
        "verify_commands": list(contract.verify_commands),
        "limits": {
            "max_wall_seconds": contract.limits.max_wall_seconds,
            "max_agent_calls": contract.limits.max_agent_calls,
            "max_attempts": contract.limits.max_attempts,
            "max_consecutive_failures": contract.limits.max_consecutive_failures,
        },
    }


def contract_from_doc(doc: dict[str, Any]) -> GoalContract:
    def req_str(key: str) -> str:
        value = doc.get(key)
        if not isinstance(value, str) or not value:
            raise SpecError(f"goal contract: {key!r} must be a non-empty string")
        return value

    def str_list(key: str) -> list[str]:
        value = doc.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SpecError(f"goal contract: {key!r} must be a list of strings")
        return list(value)

    raw_checks = doc.get("checks", [])
    if not isinstance(raw_checks, list) or not raw_checks:
        raise SpecError("goal contract: at least one acceptance check is required")
    checks: list[AcceptanceCheck] = []
    for index, raw in enumerate(raw_checks):
        if not isinstance(raw, dict):
            raise SpecError("goal contract: each check must be an object")
        check_id = raw.get("check_id") or f"check-{index + 1}"
        description = raw.get("description", "")
        command = raw.get("command")
        if not isinstance(command, str) or not command.strip():
            raise SpecError(
                f"goal contract: check {check_id!r} needs a non-empty command"
            )
        checks.append(
            AcceptanceCheck(
                check_id=str(check_id), description=str(description), command=command
            )
        )
    limits_raw = doc.get("limits")
    limits_doc = limits_raw if isinstance(limits_raw, dict) else {}

    def limit(key: str, default: int) -> int:
        value = limits_doc.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise SpecError(f"goal contract: limits.{key} must be a positive integer")
        return value

    return GoalContract(
        name=req_str("name"),
        base_sha=req_str("base_sha"),
        objective=req_str("objective"),
        must_have=str_list("must_have"),
        non_goals=str_list("non_goals"),
        checks=checks,
        verify_commands=str_list("verify_commands"),
        limits=GoalLimits(
            max_wall_seconds=limit("max_wall_seconds", 4 * 3600),
            max_agent_calls=limit("max_agent_calls", 40),
            max_attempts=limit("max_attempts", 6),
            max_consecutive_failures=limit("max_consecutive_failures", 3),
        ),
    )


@dataclass(slots=True)
class GoalRecord:
    contract: GoalContract
    status: GoalStatus
    created_ts: str
    updated_ts: str
    contract_sha256: str | None  # frozen at approval
    baseline: dict[str, object] = field(default_factory=dict)
    amendments: list[dict[str, object]] = field(default_factory=list)
    evidence: dict[str, object] = field(default_factory=dict)


class GoalStore:
    """Durable goals under ``.dagvane/goals/<name>/``."""

    def __init__(self, workspace: Workspace, clock: Clock) -> None:
        self._workspace = workspace
        self._clock = clock

    def goal_dir(self, name: str) -> Path:
        """The one place a Goal name becomes a path: validated first, then
        confirmed as a strict, symlink-free descendant of the goals root, so
        every reader/writer downstream inherits the same closed guard."""
        validated = validate_filesystem_id(name, ctx="goal name")
        path = self._workspace.goals_dir / validated
        ensure_expected_descendant(self._workspace.goals_dir, path)
        return path

    def _goal_path(self, name: str) -> Path:
        path = self.goal_dir(name) / "goal.json"
        ensure_expected_descendant(self._workspace.goals_dir, path)
        return path

    def _log_path(self, name: str) -> Path:
        path = self.goal_dir(name) / "log.jsonl"
        ensure_expected_descendant(self._workspace.goals_dir, path)
        return path

    def _sidecar_path(self, name: str, leaf: str) -> Path:
        """One guarded join for a fixed sidecar leaf: validated Goal ID,
        guarded Goal dir, then a strict, symlink-free descendant check on the
        exact leaf — the only three leaves this method is called with are
        ``lease.lock``, ``run-state.json``, and ``agent-process.json``; there
        is no path from a caller-supplied leaf string."""
        path = self.goal_dir(name) / leaf
        ensure_expected_descendant(self._workspace.goals_dir, path)
        return path

    def lease_path(self, name: str) -> Path:
        """Guarded path of the per-goal exclusive-run lease file."""
        return self._sidecar_path(name, "lease.lock")

    def run_state_path(self, name: str) -> Path:
        """Guarded path of the durable goal-run resume state."""
        return self._sidecar_path(name, "run-state.json")

    def agent_process_path(self, name: str) -> Path:
        """Guarded path of the in-flight external-agent process record."""
        return self._sidecar_path(name, "agent-process.json")

    def exists(self, name: str) -> bool:
        """A goal name "exists" only if its durable manifest is present and
        its contract identity matches ``name`` exactly. A present-but-absent
        goal.json is ordinary absence (``False``); a present-but-corrupt or
        mismatched one is a fail-closed error, never a silent ``False``."""
        validated_name = validate_filesystem_id(name, ctx="goal exists: name")
        goal_path = self._goal_path(validated_name)
        if not goal_path.exists():
            return False
        self._assert_durable_identity(validated_name)
        return True

    def save(self, expected_name: str, record: GoalRecord) -> None:
        """Persist ``record`` at ``expected_name``.

        ``expected_name`` and ``record.contract.name`` are each validated,
        then required to match exactly — the update destination is never
        derived solely from the mutable payload. If a goal directory already
        exists, it must already carry a durable manifest whose identity
        matches exactly before any byte or ``updated_ts`` is touched: a bare
        directory (no goal.json) or a malformed/mismatched one is refused,
        not silently adopted or repaired.
        """
        validated_expected = validate_filesystem_id(expected_name, ctx="goal save: expected_name")
        validated_contract_name = validate_filesystem_id(
            record.contract.name, ctx="goal save: contract.name"
        )
        if validated_expected != validated_contract_name:
            raise SpecError(
                f"goal save: expected name {expected_name!r} does not match "
                f"contract name {record.contract.name!r}"
            )
        goal_dir = self.goal_dir(validated_expected)
        goal_path = self._goal_path(validated_expected)
        if goal_dir.exists():
            if not goal_path.exists():
                raise StorageError(
                    f"goal {validated_expected!r}: existing goal directory has no "
                    "goal.json to update"
                )
            self._assert_durable_identity(validated_expected)
        record.updated_ts = self._clock.now_iso()
        atomic_write_json(
            self._goal_path(validated_expected),
            {
                "contract": contract_to_doc(record.contract),
                "status": record.status.value,
                "created_ts": record.created_ts,
                "updated_ts": record.updated_ts,
                "contract_sha256": record.contract_sha256,
                "baseline": record.baseline,
                "amendments": record.amendments,
                "evidence": record.evidence,
            },
            allowed_root=self._workspace.goals_dir,
        )

    def _assert_durable_identity(self, name: str) -> dict[str, Any]:
        """Read the durable goal doc and require its contract name to equal
        the requested/directory identity ``name`` exactly (strict string
        comparison). Fails closed on mismatch or malformed identity."""
        doc = read_json(self._goal_path(name), allowed_root=self._workspace.goals_dir)
        contract_doc = doc.get("contract")
        if not isinstance(contract_doc, dict):
            raise SpecError(f"goal {name!r}: malformed contract")
        contract_name = contract_doc.get("name")
        if not isinstance(contract_name, str) or contract_name != name:
            raise SpecError(
                f"goal {name!r}: durable contract name {contract_name!r} does "
                "not match the requested/directory identity"
            )
        return doc

    def load(self, name: str) -> GoalRecord:
        validated_name = validate_filesystem_id(name, ctx="goal load: name")
        if not self.exists(validated_name):
            raise SpecError(f"unknown goal {validated_name!r}")
        doc = self._assert_durable_identity(validated_name)
        contract_doc = doc["contract"]
        try:
            status = GoalStatus(str(doc.get("status")))
        except ValueError as exc:
            raise SpecError(
                f"goal {validated_name!r}: unknown status {doc.get('status')!r}"
            ) from exc
        baseline_raw = doc.get("baseline")
        amendments_raw = doc.get("amendments")
        evidence_raw = doc.get("evidence")
        record = GoalRecord(
            contract=contract_from_doc(contract_doc),
            status=status,
            created_ts=str(doc.get("created_ts", "")),
            updated_ts=str(doc.get("updated_ts", "")),
            contract_sha256=(
                str(doc["contract_sha256"])
                if isinstance(doc.get("contract_sha256"), str)
                else None
            ),
            baseline=dict(baseline_raw) if isinstance(baseline_raw, dict) else {},
            amendments=[
                dict(item)
                for item in (amendments_raw if isinstance(amendments_raw, list) else [])
                if isinstance(item, dict)
            ],
            evidence=dict(evidence_raw) if isinstance(evidence_raw, dict) else {},
        )
        if record.contract_sha256 is not None:
            actual = contract_hash(record.contract)
            if actual != record.contract_sha256:
                raise SpecError(
                    f"goal {validated_name!r}: the approved contract was modified "
                    "after approval (hash mismatch) — record a "
                    "CONTRACT_AMENDMENT_REQUIRED instead of editing the contract"
                )
        return record

    def list_names(self) -> list[str]:
        """Enumerate durable goal names, failing closed on anything that is
        not an ordinary absence: an invalid directory name, a symlinked
        candidate, or a durable object that does not load/validate is a
        corruption/tampering signal, not something to hide by omission."""
        goals_dir = self._workspace.goals_dir
        if goals_dir.is_symlink():
            raise StorageError(f"{goals_dir}: symlink is not an allowed goals root")
        if not goals_dir.exists():
            return []
        if not goals_dir.is_dir():
            raise StorageError(f"{goals_dir}: goals root must be a directory")
        names: list[str] = []
        for path in sorted(goals_dir.iterdir()):
            if path.is_symlink():
                raise StorageError(f"{path}: symlink is not an allowed goal entry")
            if not path.is_dir():
                raise StorageError(f"{path}: goals root must contain only goal directories")
            name = validate_filesystem_id(path.name, ctx="goal list: name")
            goal_path = path / "goal.json"
            if goal_path.is_symlink():
                raise StorageError(f"{goal_path}: symlink is not an allowed goal file")
            if not goal_path.exists():
                raise StorageError(f"{path}: goal directory is missing goal.json")
            self.load(name)
            names.append(name)
        return sorted(names)

    def log_event(self, name: str, event: dict[str, object]) -> None:
        validated_name = validate_filesystem_id(name, ctx="goal log: name")
        if not self.exists(validated_name):
            raise SpecError(f"unknown goal {validated_name!r}")
        self._assert_durable_identity(validated_name)
        doc = {"ts": self._clock.now_iso(), **event}
        append_jsonl(self._log_path(validated_name), doc, allowed_root=self._workspace.goals_dir)


def contract_hash(contract: GoalContract) -> str:
    return sha256_hex(canonical_json_bytes(contract_to_doc(contract)))


def approve(record: GoalRecord) -> None:
    if record.status is not GoalStatus.PREPARED:
        raise SpecError(
            f"goal {record.contract.name!r} is {record.status.value}; only a "
            "prepared goal can be approved"
        )
    record.contract_sha256 = contract_hash(record.contract)
    record.status = GoalStatus.APPROVED


def record_amendment_required(record: GoalRecord, check_id: str, reason: str) -> None:
    """An approved condition proved invalid: record, never silently modify."""
    record.amendments.append(
        {
            "type": "CONTRACT_AMENDMENT_REQUIRED",
            "check_id": check_id,
            "reason": reason,
        }
    )


PREPARE_INSTRUCTIONS = """\
You are preparing a frozen Goal Contract for autonomous software development.

Using the conversation below and your own inspection of this repository,
produce STRICT JSON (no markdown fence, no commentary) with exactly these
keys:

{
  "objective": "<one-paragraph objective>",
  "must_have": ["<required capability>", ...],
  "non_goals": ["<explicitly deferred item>", ...],
  "checks": [
    {"check_id": "<kebab-case-id>",
     "description": "<what this proves>",
     "command": "<shell command, exit 0 = met, runnable from the repo root>"},
    ...
  ],
  "verify_commands": ["<global gate command>", ...]
}

Rules:
- Checks must be OBJECTIVE commands (tests, greps, build steps) — no prose
  conditions. Prefer the project's existing test/lint commands.
- Include every mandatory capability from the conversation in must_have.
- List anything the owner deferred in non_goals.
- verify_commands are the project's quality gates that must stay green.
"""


def parse_prepared_contract(
    output_text: str,
    *,
    name: str,
    base_sha: str,
    limits: GoalLimits,
) -> GoalContract:
    """Parse the preparation agent's STRICT-JSON contract draft."""
    text = output_text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SpecError("goal preparation output carries no JSON object")
    try:
        doc = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SpecError(f"goal preparation output is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SpecError("goal preparation output must be a JSON object")
    doc["name"] = name
    doc["base_sha"] = base_sha
    doc["limits"] = {
        "max_wall_seconds": limits.max_wall_seconds,
        "max_agent_calls": limits.max_agent_calls,
        "max_attempts": limits.max_attempts,
        "max_consecutive_failures": limits.max_consecutive_failures,
    }
    return contract_from_doc(doc)
