"""The real installed console entry point, exercised from a clean venv.

Everything else in the suite runs ``python -m dagvane`` via PYTHONPATH; this
module installs the project into a fresh virtual environment (fully offline:
``--no-build-isolation --no-index`` against the system setuptools) and drives
the generated ``dagvane`` script itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import FIXTURE_HAPPY, REPO_ROOT, TASK_BASIC


def _base_python() -> str | None:
    """A Python that can drive an offline setuptools build, if one exists."""
    candidates = [sys.executable, "/usr/bin/python3", shutil.which("python3") or ""]
    for candidate in candidates:
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-c", "import pip, setuptools"], capture_output=True, check=False
        )
        if probe.returncode == 0:
            return candidate
    return None


@pytest.fixture(scope="module")
def installed_dagvane(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = _base_python()
    if base is None:
        pytest.skip("no Python with pip+setuptools available for an offline install")
    work = tmp_path_factory.mktemp("entrypoint")
    # Install from a staged copy so the build never writes into the repository.
    staged = work / "project"
    staged.mkdir()
    shutil.copy(REPO_ROOT / "pyproject.toml", staged / "pyproject.toml")
    shutil.copytree(REPO_ROOT / "src", staged / "src")
    venv_dir = work / "venv"
    subprocess.run(
        [base, "-m", "venv", "--system-site-packages", str(venv_dir)],
        capture_output=True,
        check=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    install = subprocess.run(
        [
            str(bin_dir / "python"),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-build-isolation",
            "--no-index",
            str(staged),
        ],
        capture_output=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr.decode(errors="replace")
    script = bin_dir / "dagvane"
    assert script.is_file(), "pip install must generate the dagvane console script"
    return script


def test_installed_console_script_reports_version(installed_dagvane: Path) -> None:
    proc = subprocess.run(
        [str(installed_dagvane), "--version"], capture_output=True, check=False
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == b"dagvane 0.2.0.dev0"


def test_installed_console_script_runs_a_council(
    installed_dagvane: Path, tmp_path: Path
) -> None:
    proc = subprocess.run(
        [
            str(installed_dagvane),
            "council",
            str(TASK_BASIC),
            "--fixture",
            str(FIXTURE_HAPPY),
            "--output",
            "json",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    report = json.loads(proc.stdout)
    assert report["status"] == "completed"
    assert (tmp_path / ".dagvane" / "runs" / "r-happy-0001" / "events.jsonl").is_file()
