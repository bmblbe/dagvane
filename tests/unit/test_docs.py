"""Contract tests for the active documentation taxonomy.

Archived and immutable historical documents deliberately preserve old wording
and links.  These tests cover only current user/developer entry points.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from helpers import FIXTURES_DIR, REPO_ROOT

CANONICAL_DOCS = (
    Path("README.md"),
    Path("DEVELOPMENT.md"),
    Path("docs/ARCHITECTURE.md"),
    Path("docs/MODULES.md"),
    Path("docs/DEVELOPMENT_PLAN.md"),
    Path("docs/TODO.md"),
)

ACTIVE_MARKDOWN = CANONICAL_DOCS + (
    Path("gui/README.md"),
    Path("docs/architecture/modules/backends/ARCHITECTURE.md"),
)

FIXTURE_NAMES = (
    "task_basic.json",
    "task_low_budget.json",
    "fixture_happy.json",
    "fixture_bad_decision.json",
    "fixture_missing_model.json",
)

LEGACY_MARKERS = (
    "dagvane init",
    "dagvane history",
    "requirements.txt",
    "Python 3.9",
    "secrets.env",
)

STALE_ACTIVE_MARKERS = (
    "candidate awaiting exact-SHA Codex re-review",
    "awaiting exact-SHA Codex re-review",
    "G1 — CURRENT",
    "docs/development/CURRENT_STATE.md",
    "docs/implementation/MASTER_PLAN.md",
    "docs/architecture/README.md",
    "docs/development/ORCHESTRAL_WORKFLOW.md",
)

RETIRED_ACTIVE_DOCS = (
    Path("docs/development/CURRENT_STATE.md"),
    Path("docs/development/ORCHESTRAL_WORKFLOW.md"),
    Path("docs/implementation/MASTER_PLAN.md"),
    Path("docs/architecture/README.md"),
    Path("docs/architecture/modules/README.md"),
    Path("docs/architecture/modules/autodev/ARCHITECTURE.md"),
    Path("docs/architecture/modules/backends/PLAN.md"),
)

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")

ARCHIVE_HASHES = {
    Path("root/README.md"): (
        "e10c478dbc4791f21304e0af24db301b499ffcb33d7d4df26f86adadf75eac68"
    ),
    Path("root/DEVELOPMENT.md"): (
        "5a310dcbce8b10661d297edb8ad2c73034dd1ddf780592f81f41d47a5c4932a2"
    ),
    Path("architecture/README.md"): (
        "b401d1f99c93677447c2766295d6d2e2be67a75eb7248c22a7f147e6c235b7fb"
    ),
    Path("architecture/modules/README.md"): (
        "0329c8649f07bb74f938a39983a62859e2c455b687d7f14d3c79c81fe6f74ee4"
    ),
    Path("architecture/modules/autodev/ARCHITECTURE.md"): (
        "b7c07ca77f4e7e0bdc7ea6239e4db179354614a2d57584579dd3b2ef0e1ccab8"
    ),
    Path("architecture/modules/backends/PLAN.md"): (
        "f9cf7d9fbbef9e4ef597650de591125bdad9df87a3cb87f436a56571b54da5f0"
    ),
    Path("development/CURRENT_STATE.md"): (
        "80925aa059a301ede5d9baa8c8c59ad7d5fc19381ba744e2f075aabd094b5eb3"
    ),
    Path("development/ORCHESTRAL_WORKFLOW.md"): (
        "6e1ac1c7ce944b6328be2a7668ae835814c7ff181ca5bf33afd15ac66854a946"
    ),
    Path("implementation/MASTER_PLAN.md"): (
        "0e2376adb3b02d41623df4530d18cbf4d9d85ba05e0bb48589a22ec08ca69a03"
    ),
}


def _read(relative: Path | str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_canonical_document_set_exists() -> None:
    for relative in CANONICAL_DOCS:
        assert (REPO_ROOT / relative).is_file(), f"missing canonical doc {relative}"


def test_documented_fixtures_are_real_and_discoverable() -> None:
    user_docs = _read("README.md") + _read("DEVELOPMENT.md")
    for name in FIXTURE_NAMES:
        assert (FIXTURES_DIR / name).is_file(), f"documented fixture {name} must exist"
        assert name in user_docs, f"active user/developer docs must name fixture {name}"

    readme = _read("README.md")
    assert "tests/fixtures/task_basic.json" in readme
    assert "tests/fixtures/fixture_happy.json" in readme


def test_readme_describes_the_real_cli_and_python_floor() -> None:
    readme = _read("README.md")
    for command in (
        "plan council",
        "council",
        "runs show",
        "events",
        "--fixture",
        "--profile",
        "conversations list|show|current|use",
        "goal prepare|show|approve|run|resume|cancel|list",
    ):
        assert command in readme, f"README must document {command!r}"
    assert "3.11" in readme
    assert "3.11" in _read("DEVELOPMENT.md")


def test_current_rejected_status_and_stop_gate_are_explicit() -> None:
    readme = _read("README.md")
    todo = _read("docs/TODO.md")
    for text in (readme, todo):
        assert "324f6c51cf7a68a8a8ad61529147873deef5a3d2" in text
        assert "4 BLOCKER + 7 MAJOR" in text
    assert "goal run" in readme
    assert "Stop gate" in readme
    assert "жодна workspace-команда" in readme
    for unsafe_surface in ("goal prepare", "goal approve", "conversations show/use"):
        assert unsafe_surface in readme
    assert "MilHRMS" in todo
    assert "RC1" in todo


def test_active_docs_carry_no_retired_paths_or_legacy_claims() -> None:
    active_text = "\n".join(_read(path) for path in CANONICAL_DOCS)
    for marker in LEGACY_MARKERS:
        assert marker not in active_text, f"active docs still mention legacy {marker!r}"
    for marker in STALE_ACTIVE_MARKERS:
        assert marker not in active_text, f"active docs still mention stale {marker!r}"
    assert not re.search(r"\bMIT\b", active_text), "active docs must not claim a license"


def test_retired_document_paths_are_not_active() -> None:
    for relative in RETIRED_ACTIVE_DOCS:
        assert not (REPO_ROOT / relative).exists(), (
            f"retired document must live only in the dated archive: {relative}"
        )


def test_agent_onboarding_uses_the_new_authority_order() -> None:
    agents = _read("AGENTS.md")
    claude = _read("CLAUDE.md")
    for path in (
        "docs/TODO.md",
        "docs/DEVELOPMENT_PLAN.md",
        "docs/ARCHITECTURE.md",
        "docs/MODULES.md",
        "DEVELOPMENT.md",
    ):
        assert path in agents
    assert "docs/TODO.md" in claude
    for marker in STALE_ACTIVE_MARKERS[3:]:
        assert marker not in agents
        assert marker not in claude


def test_active_relative_markdown_links_resolve() -> None:
    for relative in ACTIVE_MARKDOWN:
        source = REPO_ROOT / relative
        for raw_target in MARKDOWN_LINK.findall(source.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            resolved = (
                REPO_ROOT / target.lstrip("/")
                if target.startswith("/")
                else source.parent / target
            )
            assert resolved.exists(), f"broken local link in {relative}: {raw_target}"


def test_archive_manifest_maps_retired_active_paths() -> None:
    archive = REPO_ROOT / "docs/archive/2026-08-15-pre-reset"
    manifest = _read(archive.relative_to(REPO_ROOT) / "README.md")
    assert "Historical, non-authoritative" in manifest
    assert "324f6c51cf7a68a8a8ad61529147873deef5a3d2" in manifest
    for relative, expected_hash in ARCHIVE_HASHES.items():
        archived = archive / relative
        assert archived.is_file(), f"missing archived source document {archived}"
        actual_hash = hashlib.sha256(archived.read_bytes()).hexdigest()
        assert actual_hash == expected_hash
        assert expected_hash in manifest
