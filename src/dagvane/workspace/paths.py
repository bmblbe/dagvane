"""Workspace state layout: every durable Dagvane artifact of a target project
lives under ``<workspace>/.dagvane/`` (Git-ignored via a self-written
``.gitignore`` so target repositories need no preparation)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from dagvane.domain.models import StorageError


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmp → fsync → atomic rename: a crash never leaves a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise StorageError(f"cannot write {path}: {exc}") from exc


def atomic_write_json(path: Path, doc: Mapping[str, object]) -> None:
    atomic_write_bytes(
        path, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )


def read_json(path: Path) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StorageError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise StorageError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StorageError(f"{path} must contain a JSON object")
    return loaded


def append_jsonl(path: Path, doc: Mapping[str, object]) -> None:
    line = json.dumps(doc, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError(f"cannot append to {path}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


class Workspace:
    """One target project directory and its ``.dagvane/`` state root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.state_dir = self.root / ".dagvane"
        self.config_path = self.state_dir / "config.toml"
        self.conversations_dir = self.state_dir / "conversations"
        self.goals_dir = self.state_dir / "goals"
        self.agent_runs_dir = self.state_dir / "agent-runs"
        self.worktrees_dir = self.state_dir / "worktrees"

    def ensure(self) -> None:
        """Create the state layout; self-ignore so target repos stay clean."""
        for directory in (
            self.state_dir,
            self.conversations_dir,
            self.goals_dir,
            self.agent_runs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        gitignore = self.state_dir / ".gitignore"
        if not gitignore.exists():
            atomic_write_bytes(gitignore, b"*\n")
