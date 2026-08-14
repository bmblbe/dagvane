"""Shared fixtures: one session-scoped happy council run reused by read-only tests."""

from __future__ import annotations

import pytest

from helpers import (
    FIXTURE_HAPPY,
    HAPPY_RUN_ID,
    TASK_BASIC,
    CompletedRun,
    run_council_cli,
)


@pytest.fixture(scope="session")
def happy_run(tmp_path_factory: pytest.TempPathFactory) -> CompletedRun:
    cwd = tmp_path_factory.mktemp("happy")
    run = run_council_cli(TASK_BASIC, FIXTURE_HAPPY, cwd, HAPPY_RUN_ID)
    assert run.returncode == 0, run.stderr.decode()
    return run
