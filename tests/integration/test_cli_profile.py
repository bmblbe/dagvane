"""CLI surface for live profiles: mutual exclusion, credentials, preflight errors.

Everything here stays offline: failures must surface as usage errors (exit 2)
before any run state or network activity exists.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import FIXTURE_HAPPY, SRC_DIR, TASK_BASIC

PROFILE_TOML = """\
profile_version = 1

[connections.anthro]
kind = "anthropic"
credential_env = "DAGVANE_TEST_CRED_VAR"

[routes.only]
connection = "anthro"
model = "claude-sonnet-5"
max_output_tokens = 256
input_microusd_per_mtok = 3000000
output_microusd_per_mtok = 15000000

[council]
proposer_a = "only"
proposer_b = "only"
reviewer_a = "only"
reviewer_b = "only"
judge = "only"
"""


def run_cli_env(
    args: list[str], cwd: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env.pop("DAGVANE_TEST_CRED_VAR", None)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(SRC_DIR) + (os.pathsep + existing if existing else "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "dagvane", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
    )


def write_profile(tmp_path: Path, text: str = PROFILE_TOML) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_fixture_and_profile_are_mutually_exclusive(tmp_path: Path) -> None:
    profile = write_profile(tmp_path)
    proc = run_cli_env(
        [
            "council",
            str(TASK_BASIC),
            "--fixture",
            str(FIXTURE_HAPPY),
            "--profile",
            str(profile),
        ],
        tmp_path,
    )
    assert proc.returncode == 2
    assert b"not allowed with" in proc.stderr


def test_council_requires_fixture_or_profile(tmp_path: Path) -> None:
    proc = run_cli_env(["council", str(TASK_BASIC)], tmp_path)
    assert proc.returncode == 2
    assert not (tmp_path / ".dagvane").exists()


def test_missing_credential_env_is_a_usage_error(tmp_path: Path) -> None:
    profile = write_profile(tmp_path)
    proc = run_cli_env(["council", str(TASK_BASIC), "--profile", str(profile)], tmp_path)
    assert proc.returncode == 2
    assert b"DAGVANE_TEST_CRED_VAR" in proc.stderr
    assert not (tmp_path / ".dagvane").exists()


def test_invalid_profile_is_a_usage_error(tmp_path: Path) -> None:
    profile = write_profile(tmp_path, PROFILE_TOML + "\nstray = true\n")
    proc = run_cli_env(["council", str(TASK_BASIC), "--profile", str(profile)], tmp_path)
    assert proc.returncode == 2
    assert b"unknown keys" in proc.stderr
    assert not (tmp_path / ".dagvane").exists()


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None,
    reason="the anthropic SDK is installed in this environment",
)
def test_missing_live_extra_is_a_preflight_usage_error(tmp_path: Path) -> None:
    profile = write_profile(tmp_path)
    proc = run_cli_env(
        ["council", str(TASK_BASIC), "--profile", str(profile)],
        tmp_path,
        extra_env={"DAGVANE_TEST_CRED_VAR": "sk-test-not-a-real-key"},
    )
    assert proc.returncode == 2
    assert b"live" in proc.stderr
    assert b"sk-test-not-a-real-key" not in proc.stderr
    assert not (tmp_path / ".dagvane").exists()
