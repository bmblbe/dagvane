"""Live council profiles: strict TOML boundary validation (G1).

A profile defines backend connections, model routes with pinned pricing, and
the fixed council role→route mapping. It carries credential *environment
variable names* only — credential values never appear in any Dagvane document,
artifact, or event.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dagvane.domain.models import ModelRoute, Pricing, SpecError
from dagvane.protocol.frames import sha256_hex

PROFILE_VERSION = 1

BACKEND_KIND_ANTHROPIC = "anthropic"
BACKEND_KIND_OPENAI_COMPAT = "openai_compat"
BACKEND_KINDS: frozenset[str] = frozenset(
    {BACKEND_KIND_ANTHROPIC, BACKEND_KIND_OPENAI_COMPAT}
)

# The fixed council-v1 role slots a profile must route.
COUNCIL_ROLE_SLOTS: tuple[str, ...] = (
    "proposer_a",
    "proposer_b",
    "reviewer_a",
    "reviewer_b",
    "judge",
)

DEFAULT_TIMEOUT_SECONDS = 120

_PROFILE_KEYS = frozenset({"profile_version", "connections", "routes", "council"})
_CONNECTION_KEYS = frozenset({"kind", "credential_env", "base_url", "timeout_seconds"})
_ROUTE_KEYS = frozenset(
    {
        "connection",
        "model",
        "max_output_tokens",
        "input_microusd_per_mtok",
        "output_microusd_per_mtok",
    }
)


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """How to reach one backend service. Never holds a credential value."""

    connection_id: str
    kind: str
    credential_env: str
    base_url: str | None
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    connections: Mapping[str, ConnectionSpec]
    routes: Mapping[str, ModelRoute]
    council: Mapping[str, str]  # role slot -> route id
    sha256: str

    def role_routes(self) -> dict[str, ModelRoute]:
        return {slot: self.routes[route_id] for slot, route_id in self.council.items()}

    def used_connections(self) -> dict[str, ConnectionSpec]:
        """Connections actually referenced by the council role routes."""
        used = {route.backend for route in self.role_routes().values()}
        return {cid: spec for cid, spec in self.connections.items() if cid in used}


def _req_str(obj: Mapping[str, object], key: str, ctx: str) -> str:
    if key not in obj:
        raise SpecError(f"{ctx}: missing required key {key!r}")
    value = obj[key]
    if not isinstance(value, str) or not value:
        raise SpecError(f"{ctx}: key {key!r} must be a non-empty string")
    return value


def _req_int(obj: Mapping[str, object], key: str, ctx: str, *, minimum: int) -> int:
    if key not in obj:
        raise SpecError(f"{ctx}: missing required key {key!r}")
    value = obj[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SpecError(f"{ctx}: key {key!r} must be an integer >= {minimum}")
    return value


def _req_table(obj: Mapping[str, object], key: str, ctx: str) -> Mapping[str, object]:
    if key not in obj:
        raise SpecError(f"{ctx}: missing required table {key!r}")
    value = obj[key]
    if not isinstance(value, dict) or not value:
        raise SpecError(f"{ctx}: {key!r} must be a non-empty table")
    return value


def _reject_unknown_keys(obj: Mapping[str, object], allowed: frozenset[str], ctx: str) -> None:
    unknown = sorted(set(obj.keys()) - allowed)
    if unknown:
        raise SpecError(f"{ctx}: unknown keys {unknown!r}")


def _parse_connection(connection_id: str, raw: object, ctx: str) -> ConnectionSpec:
    if not isinstance(raw, dict):
        raise SpecError(f"{ctx} must be a table")
    _reject_unknown_keys(raw, _CONNECTION_KEYS, ctx)
    kind = _req_str(raw, "kind", ctx)
    if kind not in BACKEND_KINDS:
        raise SpecError(f"{ctx}: kind {kind!r} must be one of {sorted(BACKEND_KINDS)!r}")
    credential_env = _req_str(raw, "credential_env", ctx)
    base_url: str | None = None
    if "base_url" in raw:
        base_url = _req_str(raw, "base_url", ctx)
        if not (base_url.startswith("https://") or base_url.startswith("http://")):
            raise SpecError(f"{ctx}: base_url must start with http:// or https://")
    if kind == BACKEND_KIND_OPENAI_COMPAT and base_url is None:
        raise SpecError(f"{ctx}: base_url is required for kind {kind!r}")
    timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if "timeout_seconds" in raw:
        timeout_seconds = _req_int(raw, "timeout_seconds", ctx, minimum=1)
    return ConnectionSpec(
        connection_id=connection_id,
        kind=kind,
        credential_env=credential_env,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def _parse_route(
    route_id: str, raw: object, connections: Mapping[str, ConnectionSpec], ctx: str
) -> ModelRoute:
    if not isinstance(raw, dict):
        raise SpecError(f"{ctx} must be a table")
    _reject_unknown_keys(raw, _ROUTE_KEYS, ctx)
    connection = _req_str(raw, "connection", ctx)
    if connection not in connections:
        raise SpecError(f"{ctx}: unknown connection {connection!r}")
    return ModelRoute(
        route_id=route_id,
        model=_req_str(raw, "model", ctx),
        backend=connection,
        pricing=Pricing(
            input_microusd_per_mtok=_req_int(
                raw, "input_microusd_per_mtok", ctx, minimum=0
            ),
            output_microusd_per_mtok=_req_int(
                raw, "output_microusd_per_mtok", ctx, minimum=0
            ),
        ),
        max_output_tokens=_req_int(raw, "max_output_tokens", ctx, minimum=1),
    )


def parse_profile(raw_bytes: bytes, *, source: str) -> ProfileSpec:
    """Strictly parse and validate profile TOML bytes."""
    ctx = f"profile {source}"
    try:
        obj = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise SpecError(f"{ctx} is not valid TOML: {exc}") from exc
    _reject_unknown_keys(obj, _PROFILE_KEYS, ctx)
    version = _req_int(obj, "profile_version", ctx, minimum=1)
    if version != PROFILE_VERSION:
        raise SpecError(f"{ctx}: unsupported profile_version {version}")

    raw_connections = _req_table(obj, "connections", ctx)
    connections = {
        connection_id: _parse_connection(
            connection_id, raw, f"{ctx}: connections[{connection_id!r}]"
        )
        for connection_id, raw in raw_connections.items()
    }

    raw_routes = _req_table(obj, "routes", ctx)
    routes = {
        route_id: _parse_route(route_id, raw, connections, f"{ctx}: routes[{route_id!r}]")
        for route_id, raw in raw_routes.items()
    }

    raw_council = _req_table(obj, "council", ctx)
    council_ctx = f"{ctx}: council"
    _reject_unknown_keys(raw_council, frozenset(COUNCIL_ROLE_SLOTS), council_ctx)
    council: dict[str, str] = {}
    for slot in COUNCIL_ROLE_SLOTS:
        route_id = _req_str(raw_council, slot, council_ctx)
        if route_id not in routes:
            raise SpecError(f"{council_ctx}: slot {slot!r} references unknown route {route_id!r}")
        council[slot] = route_id

    return ProfileSpec(
        connections=connections,
        routes=routes,
        council=council,
        sha256=sha256_hex(raw_bytes),
    )


def load_profile_file(path: Path) -> ProfileSpec:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SpecError(f"cannot read profile file {path}: {exc}") from exc
    return parse_profile(raw, source=str(path))
