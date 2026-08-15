"""Acceptance criteria 12 and 15: no vendor SDKs, no network/process modules,
and the clock/ID discipline — mechanically enforced by an AST import scan.

With legacy tooling removed, the scan holds repository-wide: the product tree
bans all of it; the test tree additionally bans vendor SDKs and HTTP clients
(subprocess stays legitimate there for CLI tests).
"""

from __future__ import annotations

import ast
from pathlib import Path

from helpers import SRC_DIR, TESTS_DIR

FORBIDDEN_EVERYWHERE = {
    # vendor SDKs and HTTP clients
    "anthropic",
    "openai",
    "httpx",
    "requests",
    "aiohttp",
    # boundary-validation dependency rejected for G0
    "pydantic",
    # network and process surfaces excluded from G0 by the architecture
    "socket",
    "ssl",
    "http",
    "urllib",
    "subprocess",
}

# Wall-clock and identifier entropy may only enter through the runtime port.
RESTRICTED_TO_RUNTIME_PORT = {"uuid", "time", "datetime", "random"}
RUNTIME_PORT = Path("ports") / "runtime.py"


def _imported_top_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_source_tree_has_no_forbidden_imports() -> None:
    package_root = SRC_DIR / "dagvane"
    violations: list[str] = []
    scanned = 0
    for path in sorted(package_root.rglob("*.py")):
        scanned += 1
        modules = _imported_top_modules(ast.parse(path.read_text(encoding="utf-8")))
        relative = path.relative_to(package_root)
        for module in sorted(modules & FORBIDDEN_EVERYWHERE):
            violations.append(f"{relative}: forbidden import {module!r}")
        if relative != RUNTIME_PORT:
            for module in sorted(modules & RESTRICTED_TO_RUNTIME_PORT):
                violations.append(
                    f"{relative}: {module!r} is restricted to ports/runtime.py"
                )
    assert scanned >= 15, "scan looks broken: too few source files found"
    assert not violations, "\n".join(violations)


# Vendor SDKs, HTTP clients, and the rejected validation dependency stay out of
# the test tree too; process/venv modules are legitimate there (CLI subprocess
# tests, the installed-entry-point smoke test).
FORBIDDEN_IN_TESTS = {"anthropic", "openai", "httpx", "requests", "aiohttp", "pydantic"}


def test_test_tree_has_no_vendor_or_client_imports() -> None:
    violations: list[str] = []
    scanned = 0
    for path in sorted(TESTS_DIR.rglob("*.py")):
        scanned += 1
        modules = _imported_top_modules(ast.parse(path.read_text(encoding="utf-8")))
        relative = path.relative_to(TESTS_DIR)
        for module in sorted(modules & FORBIDDEN_IN_TESTS):
            violations.append(f"tests/{relative}: forbidden import {module!r}")
    assert scanned >= 15, "scan looks broken: too few test files found"
    assert not violations, "\n".join(violations)
