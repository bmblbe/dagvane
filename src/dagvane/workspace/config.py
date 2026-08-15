"""Workspace configuration: ``.dagvane/config.toml`` over engine defaults.

Read with ``tomllib``; written by a minimal TOML emitter (scalars, strings,
homogeneous lists, nested tables) — the stdlib has no writer. Secrets are
never stored: credential-bearing settings hold environment-variable *names*.
"""

from __future__ import annotations

import tomllib
from typing import Any

from dagvane.domain.models import SpecError
from dagvane.workspace.paths import Workspace, atomic_write_bytes

# Engine defaults: only what the Autonomous Developer MVP actually uses.
# Resource entries mirror the deterministic cheap-first router tiers.
DEFAULT_CONFIG: dict[str, Any] = {
    "chat": {
        # Which catalog resource answers `dagvane chat`.
        "resource": "codex-standard",
        # How many trailing conversation messages ride along as context.
        "history_messages": 16,
        "history_char_budget": 24000,
        "timeout_seconds": 900,
    },
    "router": {
        "local_enabled": True,
        "concurrency": 2,
    },
    "goal": {
        "review_policy": "substantial",  # always | substantial | never
        "implement_resource": "codex-standard",
        "review_resource": "codex-strong",
        "prepare_resource": "codex-standard",
        "max_wall_seconds": 4 * 3600,
        "max_agent_calls": 40,
        "max_attempts": 6,
        "max_consecutive_failures": 3,
        "agent_timeout_seconds": 1800,
    },
    "resources": {
        "codex-cheap": {
            "kind": "external_agent",
            "runtime": "codex",
            "model": "gpt-5.6-luna",
            "reasoning": "low",
            "tier": "CHEAP",
            "enabled": True,
        },
        "codex-standard": {
            "kind": "external_agent",
            "runtime": "codex",
            "model": "gpt-5.6-terra",
            "reasoning": "medium",
            "tier": "STANDARD",
            "enabled": True,
        },
        "codex-strong": {
            "kind": "external_agent",
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "tier": "STRONG",
            "enabled": True,
        },
        "codex-critical": {
            "kind": "external_agent",
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "ultra",
            "tier": "CRITICAL",
            "enabled": True,
        },
        "ollama-local": {
            "kind": "chat_backend",
            "runtime": "ollama",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5-coder:3b",
            "tier": "LOCAL",
            "enabled": True,
        },
        "agy-review": {
            "kind": "external_agent",
            "runtime": "agy",
            "model": "gemini-3.1-pro-high",
            "reasoning": "high",
            "tier": "STRONG",
            "enabled": False,  # optional; enable when the runtime is verified
        },
    },
}


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise SpecError(f"unsupported config value type {type(value).__name__}")


def render_toml(doc: dict[str, Any], prefix: str = "") -> str:
    """Minimal TOML emitter: scalars first, then nested tables, recursively."""
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    for key, value in doc.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_scalar(value)}")
    text = "\n".join(lines)
    for key, table in tables:
        full = f"{prefix}{key}"
        rendered = render_toml(table, prefix=full + ".")
        header = f"[{full}]"
        text = text + ("\n\n" if text else "") + header + "\n" + rendered
    return text


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _flatten(doc: dict[str, Any], prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    for key, value in doc.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=full + "."))
        else:
            flat[full] = value
    return flat


class WorkspaceConfig:
    """Effective configuration: workspace file merged over engine defaults."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._file_doc: dict[str, Any] = {}
        if workspace.config_path.exists():
            try:
                self._file_doc = tomllib.loads(
                    workspace.config_path.read_text(encoding="utf-8")
                )
            except (tomllib.TOMLDecodeError, OSError) as exc:
                raise SpecError(
                    f"cannot read {workspace.config_path}: {exc}"
                ) from exc
        self._effective = _merge(DEFAULT_CONFIG, self._file_doc)

    @property
    def effective(self) -> dict[str, Any]:
        return self._effective

    def flat(self) -> dict[str, object]:
        return _flatten(self._effective)

    def get(self, dotted_key: str) -> object:
        node: object = self._effective
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise SpecError(f"unknown config key {dotted_key!r}")
            node = node[part]
        return node

    def set(self, dotted_key: str, raw_value: str) -> None:
        """Set one key in the workspace file (value parsed as a TOML scalar)."""
        try:
            parsed = tomllib.loads(f"value = {raw_value}")["value"]
        except tomllib.TOMLDecodeError:
            parsed = raw_value  # bare string convenience: `dagvane config set k v`
        node = self._file_doc
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = parsed
        self.save()

    def save(self) -> None:
        self._workspace.ensure()
        rendered = render_toml(self._file_doc)
        atomic_write_bytes(
            self._workspace.config_path, (rendered + "\n").encode("utf-8")
        )
        self._effective = _merge(DEFAULT_CONFIG, self._file_doc)
