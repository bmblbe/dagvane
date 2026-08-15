"""Documentation names the real implementation, not the retired prototype.

The published smoke commands themselves are exercised verbatim by the CLI and
entry-point tests; this module pins the documents to the real fixture names,
the real Python floor, and the absence of legacy-prototype vocabulary.
"""

from __future__ import annotations

import re

from helpers import FIXTURES_DIR, REPO_ROOT

FIXTURE_NAMES = (
    "task_basic.json",
    "task_low_budget.json",
    "fixture_happy.json",
    "fixture_bad_decision.json",
    "fixture_missing_model.json",
)

# "ANTHROPIC_API_KEY" left this list in G1: profiles legitimately name
# credential environment variables in the documentation.
LEGACY_MARKERS = (
    "dagvane init",
    "dagvane chat",
    "dagvane history",
    "dagvane config set",
    "requirements.txt",
    "Python 3.9",
    "secrets.env",
)


def _readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


def _development() -> str:
    return (REPO_ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")


def test_documented_fixtures_are_the_real_ones() -> None:
    readme = _readme()
    development = _development()
    for name in FIXTURE_NAMES:
        assert (FIXTURES_DIR / name).is_file(), f"documented fixture {name} must exist"
        assert name in readme, f"README must name fixture {name}"
        assert name in development, f"DEVELOPMENT must name fixture {name}"


def test_docs_describe_the_current_cli_and_python_floor() -> None:
    readme = _readme()
    for command in (
        "plan council",
        "council",
        "runs show",
        "events",
        "--fixture",
        "--profile",
    ):
        assert command in readme, f"README must document {command!r}"
    assert "3.11" in readme
    assert "3.11" in _development()


def test_docs_carry_no_legacy_prototype_claims() -> None:
    for name, text in (("README.md", _readme()), ("DEVELOPMENT.md", _development())):
        for marker in LEGACY_MARKERS:
            assert marker not in text, f"{name} still mentions legacy {marker!r}"
        # The stale license claim: the choice is an open owner decision.
        assert not re.search(r"\bMIT\b", text), f"{name} must not claim a license"
