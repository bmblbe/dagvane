"""Acceptance criterion 1: Python 3.11 minimum is declared consistently."""

from __future__ import annotations

import tomllib

from helpers import REPO_ROOT


def _pyproject() -> dict[str, object]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def test_python_minimum_is_311_everywhere() -> None:
    doc = _pyproject()
    project = doc["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == ">=3.11"
    tool = doc["tool"]
    assert isinstance(tool, dict)
    assert tool["mypy"]["python_version"] == "3.11"
    assert tool["mypy"]["strict"] is True
    assert tool["ruff"]["target-version"] == "py311"


def test_zero_runtime_dependencies_and_cli_entry() -> None:
    doc = _pyproject()
    project = doc["project"]
    assert isinstance(project, dict)
    assert project["dependencies"] == []
    assert project["scripts"] == {"dagvane": "dagvane.cli:main"}


def test_workspace_state_and_legacy_editor_config_stay_untracked() -> None:
    ignore_rules = {
        line.strip()
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".dagvane/" in ignore_rules
    assert ".kateproject" in ignore_rules
    assert not (REPO_ROOT / ".kateproject").exists()
