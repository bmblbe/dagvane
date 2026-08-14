"""Boundary documents: task files, fixture files, decisions, and doc builders.

Hand-rolled strict validation at the external boundary (no Pydantic). Persisted
documents never contain filesystem paths — external files enter the record as
content hashes plus normalized content.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dagvane.domain.models import (
    Budget,
    BudgetOverrides,
    Decision,
    ModelRoute,
    Plan,
    SpecError,
    TaskSpec,
    Usage,
)
from dagvane.protocol.frames import sha256_hex

TASK_VERSION = 1
FIXTURE_VERSION = 1
DECISION_VERSION = 1
MANIFEST_VERSION = 1
REPORT_VERSION = 1

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _load_json_object(path: Path, what: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read {what} file {path}: {exc}") from exc
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpecError(f"{what} file {path} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SpecError(f"{what} file {path} must contain a JSON object")
    return obj


def _raw_sha256(path: Path, what: str) -> str:
    try:
        return sha256_hex(path.read_bytes())
    except OSError as exc:
        raise SpecError(f"cannot read {what} file {path}: {exc}") from exc


def _req_str(obj: Mapping[str, object], key: str, ctx: str) -> str:
    if key not in obj:
        raise SpecError(f"{ctx}: missing required key {key!r}")
    value = obj[key]
    if not isinstance(value, str) or not value:
        raise SpecError(f"{ctx}: key {key!r} must be a non-empty string")
    return value


def _req_int(obj: Mapping[str, object], key: str, ctx: str, *, minimum: int = 0) -> int:
    if key not in obj:
        raise SpecError(f"{ctx}: missing required key {key!r}")
    value = obj[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SpecError(f"{ctx}: key {key!r} must be an integer >= {minimum}")
    return value


def _reject_unknown_keys(obj: Mapping[str, object], allowed: frozenset[str], ctx: str) -> None:
    unknown = sorted(set(obj.keys()) - allowed)
    if unknown:
        raise SpecError(f"{ctx}: unknown keys {unknown!r}")


# ---------------------------------------------------------------------------
# Task files
# ---------------------------------------------------------------------------

_TASK_KEYS = frozenset(
    {"task_version", "task_id", "title", "statement", "acceptance_criteria", "budget"}
)
_BUDGET_KEYS = frozenset({"max_calls", "max_total_tokens", "max_cost_microusd"})


@dataclass(frozen=True, slots=True)
class LoadedTask:
    spec: TaskSpec
    doc: dict[str, object]
    sha256: str


def load_task_file(path: Path) -> LoadedTask:
    ctx = f"task file {path}"
    obj = _load_json_object(path, "task")
    _reject_unknown_keys(obj, _TASK_KEYS, ctx)
    version = _req_int(obj, "task_version", ctx, minimum=1)
    if version != TASK_VERSION:
        raise SpecError(f"{ctx}: unsupported task_version {version}")
    task_id = _req_str(obj, "task_id", ctx)
    title = _req_str(obj, "title", ctx)
    statement = _req_str(obj, "statement", ctx)

    criteria: tuple[str, ...] = ()
    if "acceptance_criteria" in obj:
        raw_criteria = obj["acceptance_criteria"]
        if not isinstance(raw_criteria, list) or not all(
            isinstance(item, str) and item for item in raw_criteria
        ):
            raise SpecError(f"{ctx}: acceptance_criteria must be a list of non-empty strings")
        criteria = tuple(raw_criteria)

    overrides = BudgetOverrides()
    if "budget" in obj:
        raw_budget = obj["budget"]
        if not isinstance(raw_budget, dict):
            raise SpecError(f"{ctx}: budget must be an object")
        _reject_unknown_keys(raw_budget, _BUDGET_KEYS, f"{ctx}: budget")
        values: dict[str, int | None] = {}
        for key in sorted(_BUDGET_KEYS):
            values[key] = _req_int(raw_budget, key, f"{ctx}: budget", minimum=1) \
                if key in raw_budget else None
        overrides = BudgetOverrides(
            max_calls=values["max_calls"],
            max_total_tokens=values["max_total_tokens"],
            max_cost_microusd=values["max_cost_microusd"],
        )

    spec = TaskSpec(
        task_id=task_id,
        title=title,
        statement=statement,
        acceptance_criteria=criteria,
        budget_overrides=overrides,
    )
    doc: dict[str, object] = {
        "task_version": TASK_VERSION,
        "task_id": task_id,
        "title": title,
        "statement": statement,
        "acceptance_criteria": list(criteria),
        "budget": {
            "max_calls": overrides.max_calls,
            "max_total_tokens": overrides.max_total_tokens,
            "max_cost_microusd": overrides.max_cost_microusd,
        },
    }
    return LoadedTask(spec=spec, doc=doc, sha256=_raw_sha256(path, "task"))


# ---------------------------------------------------------------------------
# Fixture files
# ---------------------------------------------------------------------------

_FIXTURE_KEYS = frozenset({"fixture_version", "run_id", "clock", "ids", "responses"})
_CLOCK_KEYS = frozenset({"start", "step_ms"})
_IDS_KEYS = frozenset({"seed"})
_RESPONSE_KEYS = frozenset({"text", "usage"})
_USAGE_KEYS = frozenset({"input_tokens", "output_tokens"})


@dataclass(frozen=True, slots=True)
class FixtureResponse:
    text: str
    usage: Usage | None


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    responses: Mapping[str, FixtureResponse]
    run_id: str | None
    clock_start: str | None
    clock_step_ms: int | None
    ids_seed: str | None
    sha256: str

    def determinism_doc(self) -> dict[str, object]:
        clock: dict[str, object] | None = None
        if self.clock_start is not None and self.clock_step_ms is not None:
            clock = {"start": self.clock_start, "step_ms": self.clock_step_ms}
        return {
            "run_id_pinned": self.run_id is not None,
            "clock": clock,
            "ids_seed": self.ids_seed,
        }


def load_fixture_file(path: Path) -> FixtureSpec:
    ctx = f"fixture file {path}"
    obj = _load_json_object(path, "fixture")
    _reject_unknown_keys(obj, _FIXTURE_KEYS, ctx)
    version = _req_int(obj, "fixture_version", ctx, minimum=1)
    if version != FIXTURE_VERSION:
        raise SpecError(f"{ctx}: unsupported fixture_version {version}")

    run_id: str | None = None
    if "run_id" in obj:
        run_id = _req_str(obj, "run_id", ctx)
        if not _RUN_ID_RE.match(run_id):
            raise SpecError(f"{ctx}: run_id {run_id!r} must match {_RUN_ID_RE.pattern}")

    clock_start: str | None = None
    clock_step_ms: int | None = None
    if "clock" in obj:
        raw_clock = obj["clock"]
        if not isinstance(raw_clock, dict):
            raise SpecError(f"{ctx}: clock must be an object")
        _reject_unknown_keys(raw_clock, _CLOCK_KEYS, f"{ctx}: clock")
        clock_start = _req_str(raw_clock, "start", f"{ctx}: clock")
        clock_step_ms = _req_int(raw_clock, "step_ms", f"{ctx}: clock", minimum=0)

    ids_seed: str | None = None
    if "ids" in obj:
        raw_ids = obj["ids"]
        if not isinstance(raw_ids, dict):
            raise SpecError(f"{ctx}: ids must be an object")
        _reject_unknown_keys(raw_ids, _IDS_KEYS, f"{ctx}: ids")
        ids_seed = _req_str(raw_ids, "seed", f"{ctx}: ids")

    raw_responses = obj.get("responses")
    if not isinstance(raw_responses, dict) or not raw_responses:
        raise SpecError(f"{ctx}: responses must be a non-empty object keyed by model id")
    responses: dict[str, FixtureResponse] = {}
    for model, raw in raw_responses.items():
        rctx = f"{ctx}: responses[{model!r}]"
        if not isinstance(raw, dict):
            raise SpecError(f"{rctx} must be an object")
        _reject_unknown_keys(raw, _RESPONSE_KEYS, rctx)
        text = _req_str(raw, "text", rctx)
        usage: Usage | None = None
        if "usage" in raw:
            raw_usage = raw["usage"]
            if not isinstance(raw_usage, dict):
                raise SpecError(f"{rctx}: usage must be an object")
            _reject_unknown_keys(raw_usage, _USAGE_KEYS, f"{rctx}: usage")
            usage = Usage(
                input_tokens=_req_int(raw_usage, "input_tokens", f"{rctx}: usage"),
                output_tokens=_req_int(raw_usage, "output_tokens", f"{rctx}: usage"),
            )
        responses[model] = FixtureResponse(text=text, usage=usage)

    return FixtureSpec(
        responses=responses,
        run_id=run_id,
        clock_start=clock_start,
        clock_step_ms=clock_step_ms,
        ids_seed=ids_seed,
        sha256=_raw_sha256(path, "fixture"),
    )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

_DECISION_KEYS = frozenset({"decision_version", "winner", "rationale"})


def parse_decision(text: str, allowed_winners: frozenset[str]) -> Decision:
    ctx = "judge decision"
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise SpecError(f"{ctx} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SpecError(f"{ctx} must be a JSON object")
    _reject_unknown_keys(obj, _DECISION_KEYS, ctx)
    version = _req_int(obj, "decision_version", ctx, minimum=1)
    if version != DECISION_VERSION:
        raise SpecError(f"{ctx}: unsupported decision_version {version}")
    winner = _req_str(obj, "winner", ctx)
    if winner not in allowed_winners:
        raise SpecError(
            f"{ctx}: winner {winner!r} is not one of {sorted(allowed_winners)!r}"
        )
    rationale = _req_str(obj, "rationale", ctx)
    return Decision(decision_version=version, winner=winner, rationale=rationale)


def build_decision_doc(decision: Decision) -> dict[str, object]:
    return {
        "decision_version": decision.decision_version,
        "winner": decision.winner,
        "rationale": decision.rationale,
    }


# ---------------------------------------------------------------------------
# Plan / manifest documents
# ---------------------------------------------------------------------------


def build_plan_doc(plan: Plan) -> dict[str, object]:
    return {
        "plan_version": plan.plan_version,
        "nodes": [
            {
                "node_id": node.node_id,
                "role": node.role,
                "identity": node.identity,
                "route_id": node.route_id,
                "depends_on": list(node.depends_on),
                "input_manifest": [
                    {"kind": entry.kind, "label": entry.label, "producer": entry.producer}
                    for entry in node.input_manifest.entries
                ],
            }
            for node in plan.nodes
        ],
        "anonymization": dict(plan.anonymization),
    }


def build_routes_doc(routes: Mapping[str, ModelRoute]) -> dict[str, object]:
    return {
        route_id: {
            "model": route.model,
            "backend": route.backend,
            "max_output_tokens": route.max_output_tokens,
            "pricing": {
                "input_microusd_per_mtok": route.pricing.input_microusd_per_mtok,
                "output_microusd_per_mtok": route.pricing.output_microusd_per_mtok,
            },
        }
        for route_id, route in sorted(routes.items())
    }


def build_budget_doc(budget: Budget) -> dict[str, object]:
    return {
        "max_calls": budget.max_calls,
        "max_total_tokens": budget.max_total_tokens,
        "max_cost_microusd": budget.max_cost_microusd,
    }


def build_manifest_doc(
    *,
    run_id: str,
    created_ts: str,
    engine_version: str,
    task_doc: Mapping[str, object],
    task_sha256: str,
    plan_doc: Mapping[str, object],
    plan_sha256: str,
    routes_doc: Mapping[str, object],
    budget_doc: Mapping[str, object],
    fixture_sha256: str,
    determinism_doc: Mapping[str, object],
) -> dict[str, object]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "created_ts": created_ts,
        "engine_version": engine_version,
        "task": dict(task_doc),
        "task_sha256": task_sha256,
        "plan": dict(plan_doc),
        "plan_sha256": plan_sha256,
        "routes": dict(routes_doc),
        "budget_caps": dict(budget_doc),
        "fixture_sha256": fixture_sha256,
        "determinism": dict(determinism_doc),
    }
