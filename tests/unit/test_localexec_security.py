"""Direct regression coverage for the managed shell cwd authority."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from dagvane.adapters.localexec import run_shell
from dagvane.ports.runtime import SteppingMonotonic


def test_run_shell_pins_cwd_against_rename_to_foreign_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned = tmp_path / "owned"
    owned.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    foreign_marker = foreign / "escaped.txt"
    backup = tmp_path / "owned-before-shell-swap"
    owned_stat = os.stat(owned, follow_symlinks=False)
    cwd_identity = (owned_stat.st_dev, owned_stat.st_ino)
    real_popen = subprocess.Popen
    injected = False

    def swap_at_command(*args: object, **kwargs: object) -> Any:
        nonlocal injected
        command = args[0] if args else kwargs.get("args")
        if (
            not injected
            and isinstance(command, str)
            and "escaped.txt" in command
        ):
            injected = True
            owned.rename(backup)
            owned.symlink_to(foreign, target_is_directory=True)
        return cast(Any, real_popen)(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", swap_at_command)
    result = run_shell(
        "touch escaped.txt",
        cwd=owned,
        cwd_identity=cwd_identity,
        monotonic=SteppingMonotonic(),
        timeout_seconds=5,
    )

    assert result.ok
    assert injected
    assert not foreign_marker.exists()
    assert (backup / "escaped.txt").is_file()
    assert owned.is_symlink()
    assert backup.is_dir()
