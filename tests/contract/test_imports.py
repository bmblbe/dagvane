"""Acceptance criteria 12 and 15: no vendor SDKs, no network/process modules,
and the clock/ID discipline — mechanically enforced by an AST import scan.

The product tree bans vendor SDKs, HTTP clients, and network/process surfaces
everywhere except the designated live adapters, where exactly one lazy vendor
import each is permitted (G1); a runtime check proves those imports stay lazy.
The test tree additionally bans vendor SDKs and HTTP clients (subprocess stays
legitimate there for CLI tests).
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
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
    # network and process surfaces excluded from the engine by the architecture
    "socket",
    "ssl",
    "http",
    "urllib",
    "subprocess",
}

# Exact submodules exempt from the top-level ban: pure string parsing with no
# network surface. ``urllib.request``/``urllib.error`` stay banned; profile
# validation needs standard URL authority parsing (Codex B1 remediation).
EXEMPT_SUBMODULES = {"urllib.parse"}

# The only files allowed to (lazily) import their one optional live dependency.
LIVE_ADAPTER_ALLOWLIST: dict[Path, set[str]] = {
    Path("adapters") / "backends" / "anthropic.py": {"anthropic"},
    Path("adapters") / "backends" / "openai_compat.py": {"httpx"},
}

# Wall-clock and identifier entropy may only enter through the runtime port.
RESTRICTED_TO_RUNTIME_PORT = {"uuid", "time", "datetime", "random"}
RUNTIME_PORT = Path("ports") / "runtime.py"


def _imported_top_modules(tree: ast.AST) -> set[str]:
    """Top-level modules imported, with exact exempt submodules excluded."""
    full_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            full_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            full_names.add(node.module)
    return {name.split(".")[0] for name in full_names if name not in EXEMPT_SUBMODULES}


def test_source_tree_has_no_forbidden_imports() -> None:
    package_root = SRC_DIR / "dagvane"
    violations: list[str] = []
    scanned = 0
    for path in sorted(package_root.rglob("*.py")):
        scanned += 1
        modules = _imported_top_modules(ast.parse(path.read_text(encoding="utf-8")))
        relative = path.relative_to(package_root)
        allowed = LIVE_ADAPTER_ALLOWLIST.get(relative, set())
        for module in sorted((modules & FORBIDDEN_EVERYWHERE) - allowed):
            violations.append(f"{relative}: forbidden import {module!r}")
        if relative != RUNTIME_PORT:
            for module in sorted(modules & RESTRICTED_TO_RUNTIME_PORT):
                violations.append(
                    f"{relative}: {module!r} is restricted to ports/runtime.py"
                )
    assert scanned >= 15, "scan looks broken: too few source files found"
    assert not violations, "\n".join(violations)


def test_live_adapter_vendor_imports_stay_lazy() -> None:
    """Importing the live adapters must not import their optional dependencies."""
    code = (
        "import sys\n"
        "import dagvane.adapters.backends.anthropic\n"
        "import dagvane.adapters.backends.openai_compat\n"
        "assert 'anthropic' not in sys.modules, 'anthropic imported eagerly'\n"
        "assert 'httpx' not in sys.modules, 'httpx imported eagerly'\n"
        "print('lazy-ok')\n"
    )
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert b"lazy-ok" in proc.stdout


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
