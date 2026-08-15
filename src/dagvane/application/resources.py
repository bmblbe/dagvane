"""Execution-resource catalog and the deterministic cheap-first router (MVP).

The router is explicit policy, not an AI: task kind, risk, and prior failed
attempts map to a tier ladder (TOOLS → LOCAL → CHEAP → STANDARD → STRONG →
CRITICAL), and the catalog resolves the tier to a configured resource. Every
decision carries a compact human-readable reason that is persisted with the
work it routed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dagvane.domain.models import SpecError

TIER_ORDER: tuple[str, ...] = (
    "TOOLS",
    "LOCAL",
    "CHEAP",
    "STANDARD",
    "STRONG",
    "CRITICAL",
)


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """One routable execution resource from workspace configuration."""

    resource_id: str
    kind: str  # "external_agent" | "chat_backend"
    runtime: str  # "codex" | "agy" | "ollama" | "command"
    tier: str
    model: str | None = None
    reasoning: str | None = None
    base_url: str | None = None
    enabled: bool = True
    command_template: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    resource: ResourceSpec
    tier: str
    reason: str


def _parse_resource(resource_id: str, raw: object) -> ResourceSpec:
    if not isinstance(raw, dict):
        raise SpecError(f"resource {resource_id!r} must be a table")
    kind = raw.get("kind")
    runtime = raw.get("runtime")
    tier = raw.get("tier")
    if not isinstance(kind, str) or kind not in ("external_agent", "chat_backend"):
        raise SpecError(f"resource {resource_id!r}: invalid kind {kind!r}")
    if not isinstance(runtime, str) or not runtime:
        raise SpecError(f"resource {resource_id!r}: invalid runtime")
    if not isinstance(tier, str) or tier not in TIER_ORDER:
        raise SpecError(f"resource {resource_id!r}: invalid tier {tier!r}")
    command_raw = raw.get("command", [])
    if not isinstance(command_raw, list) or not all(
        isinstance(part, str) for part in command_raw
    ):
        raise SpecError(f"resource {resource_id!r}: command must be a string list")
    return ResourceSpec(
        resource_id=resource_id,
        kind=kind,
        runtime=runtime,
        tier=tier,
        model=raw.get("model") if isinstance(raw.get("model"), str) else None,
        reasoning=raw.get("reasoning") if isinstance(raw.get("reasoning"), str) else None,
        base_url=raw.get("base_url") if isinstance(raw.get("base_url"), str) else None,
        enabled=bool(raw.get("enabled", True)),
        command_template=tuple(command_raw),
    )


class ResourceCatalog:
    """Configured resources, indexed by id and by tier (enabled only)."""

    def __init__(self, resources_config: dict[str, Any]) -> None:
        self._by_id: dict[str, ResourceSpec] = {
            resource_id: _parse_resource(resource_id, raw)
            for resource_id, raw in resources_config.items()
        }
        # LOCAL availability is a runtime property (probe result); the router
        # asks the catalog, and the session marks it unavailable on probe fail.
        self._local_available: bool | None = None

    def get(self, resource_id: str) -> ResourceSpec:
        spec = self._by_id.get(resource_id)
        if spec is None:
            raise SpecError(f"unknown resource {resource_id!r}")
        if not spec.enabled:
            raise SpecError(f"resource {resource_id!r} is disabled")
        return spec

    def enabled(self) -> list[ResourceSpec]:
        return [spec for spec in self._by_id.values() if spec.enabled]

    def first_in_tier(self, tier: str) -> ResourceSpec | None:
        for resource_id in sorted(self._by_id):
            spec = self._by_id[resource_id]
            if spec.enabled and spec.tier == tier:
                return spec
        return None

    def mark_local_available(self, available: bool) -> None:
        self._local_available = available

    @property
    def local_available(self) -> bool:
        return bool(self._local_available)


# Base tier per task kind — the cheap-first default before escalation.
_BASE_TIER: dict[str, str] = {
    "classify": "LOCAL",
    "summarize": "LOCAL",
    "analyze": "CHEAP",
    "chat": "STANDARD",
    "prepare": "STANDARD",
    "implement": "STANDARD",
    "remediate": "STANDARD",
    "review": "STRONG",
}


def _escalate(tier: str, steps: int) -> str:
    index = TIER_ORDER.index(tier)
    return TIER_ORDER[min(index + steps, len(TIER_ORDER) - 1)]


def route_task(
    catalog: ResourceCatalog,
    task_kind: str,
    *,
    risk: str = "normal",  # "normal" | "high"
    attempt: int = 1,
    preferred_resource: str | None = None,
) -> RoutingDecision:
    """Deterministic tier selection with the §26 escalation ladder.

    attempt 1 → base tier; attempt 2 → same tier (change strategy);
    attempt 3+ → one tier stronger. High risk raises review to CRITICAL.
    """
    if preferred_resource is not None and attempt <= 2:
        spec = catalog.get(preferred_resource)
        return RoutingDecision(
            resource=spec,
            tier=spec.tier,
            reason=f"{task_kind}: configured resource {preferred_resource} "
            f"(attempt {attempt})",
        )

    base = _BASE_TIER.get(task_kind)
    if base is None:
        raise SpecError(f"unknown task kind {task_kind!r}")
    tier = base
    if task_kind == "review" and risk == "high":
        tier = "CRITICAL"
    if attempt >= 3:
        tier = _escalate(tier, attempt - 2)

    # LOCAL degrades to CHEAP when no capable local model is available.
    if tier == "LOCAL" and not catalog.local_available:
        tier = "CHEAP"

    probe_tier = tier
    while True:
        found = catalog.first_in_tier(probe_tier)
        if found is not None:
            reason = f"{task_kind}: tier {tier}"
            if probe_tier != tier:
                reason += f" → {probe_tier} (no resource in {tier})"
            if attempt > 1:
                reason += f", attempt {attempt}"
            if risk == "high":
                reason += ", high risk"
            return RoutingDecision(resource=found, tier=probe_tier, reason=reason)
        if probe_tier == TIER_ORDER[-1]:
            break
        probe_tier = _escalate(probe_tier, 1)
    raise SpecError(
        f"no enabled resource can serve task kind {task_kind!r} (tier {tier})"
    )
