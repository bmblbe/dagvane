"""SubprocessAgentRunner — sanitization, environment, and lifecycle units.

Offline: every child is a local Python script; every secret is synthetic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dagvane.adapters.agents.subprocess_runner import (
    SubprocessAgentRunner,
    child_environment,
    terminate_recorded_process,
)
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.agent import AgentInvocation
from dagvane.ports.runtime import SystemClock, SystemIds, SystemMonotonic
from dagvane.workspace.paths import atomic_write_json


def _runner(tmp_path: Path, scrubber: SecretScrubber, **kwargs: int) -> SubprocessAgentRunner:
    return SubprocessAgentRunner(
        runs_dir=tmp_path / "agent-runs",
        clock=SystemClock(),
        monotonic=SystemMonotonic(),
        ids=SystemIds(),
        scrubber=scrubber,
        **kwargs,
    )


def _invocation(tmp_path: Path, script_body: str, **kwargs: object) -> AgentInvocation:
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "prompt = Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "output = Path(sys.argv[2])\n"
        "ctrl = Path(__file__).resolve().parent\n" + script_body,
        encoding="utf-8",
    )
    defaults: dict[str, object] = {
        "runtime": "command",
        "prompt": "the prompt",
        "cwd": tmp_path,
        "timeout_seconds": 30,
        "command_template": (
            sys.executable,
            str(script),
            "{prompt_file}",
            "{output_file}",
        ),
    }
    defaults.update(kwargs)
    return AgentInvocation(**defaults)  # type: ignore[arg-type]


def _wait_pid_gone(pid: int, *, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} is still alive")


def test_child_environment_is_minimal_and_registers_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_ONLY_SECRET", "never-inherited")
    monkeypatch.setenv("PLAIN_ALLOWED", "plain-value")
    monkeypatch.setenv("CRED_ALLOWED", "synthetic-cred-a1b2c3")
    scrubber = SecretScrubber()
    invocation = AgentInvocation(
        runtime="command",
        prompt="p",
        cwd=Path("."),
        command_template=("true",),
        env_passthrough=("PLAIN_ALLOWED",),
        secret_env=("CRED_ALLOWED", "ABSENT_NAME"),
    )
    env = child_environment(invocation, scrubber)
    assert "HOST_ONLY_SECRET" not in env
    assert env["PLAIN_ALLOWED"] == "plain-value"
    assert env["CRED_ALLOWED"] == "synthetic-cred-a1b2c3"
    assert set(env) <= {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "TERM",
        "PLAIN_ALLOWED",
        "CRED_ALLOWED",
    }
    # The credential value is registered ephemerally with the scrubber.
    assert scrubber.scrub("x synthetic-cred-a1b2c3 y") == "x [redacted] y"
    # Plain passthrough values are not treated as credentials.
    assert scrubber.scrub("plain-value") == "plain-value"


def test_prompt_stream_and_output_are_scrubbed_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-token-9e8d7c"
    monkeypatch.setenv("FAKE_TOKEN", secret)
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber)
    execution = runner.run(
        _invocation(
            tmp_path,
            "token = os.environ['FAKE_TOKEN']\n"
            "print('stream mentions', token)\n"
            "output.write_text('final message with ' + token, encoding='utf-8')\n",
            prompt=f"prompt containing {secret}",
            secret_env=("FAKE_TOKEN",),
        )
    )
    assert execution.succeeded
    secret_bytes = secret.encode("utf-8")
    prompt_bytes = Path(execution.prompt_path).read_bytes()
    assert secret_bytes not in prompt_bytes
    assert b"[redacted]" in prompt_bytes
    assert secret_bytes not in Path(execution.log_path).read_bytes()
    assert secret_bytes not in Path(execution.output_path).read_bytes()
    assert "[redacted]" in execution.output_text
    # The raw temporary written by the child is gone.
    run_dir = Path(execution.output_path).parent
    assert not (run_dir / "output.raw").exists()
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "output.md",
        "prompt.md",
        "stream.log",
    ]


def test_stream_and_output_are_bounded_with_truncation_markers(
    tmp_path: Path,
) -> None:
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber, max_stream_chars=2000, max_output_chars=500)
    execution = runner.run(
        _invocation(
            tmp_path,
            "sys.stdout.write('S' * 100000)\n"
            "output.write_text('O' * 100000, encoding='utf-8')\n",
        )
    )
    assert execution.succeeded
    log_text = Path(execution.log_path).read_text(encoding="utf-8")
    assert len(log_text) < 4000
    assert "[stream truncated" in log_text
    assert len(execution.output_text) < 1000
    assert execution.output_text.endswith("[output truncated]")


def test_timeout_terminates_and_reaps_the_whole_process_tree(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    execution = runner.run(
        _invocation(
            tmp_path,
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            "(ctrl / 'pids').write_text(f'{os.getpid()} {child.pid}')\n"
            "sys.stdout.write('spawned\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(300)\n",
            timeout_seconds=2,
        )
    )
    assert execution.timed_out
    assert not execution.succeeded
    parent_pid, child_pid = (
        int(part) for part in (tmp_path / "pids").read_text().split()
    )
    _wait_pid_gone(parent_pid)
    _wait_pid_gone(child_pid)  # the grandchild died with the process group


def test_process_record_exists_while_running_and_is_removed_after(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "agent-process.json"
    runner = _runner(tmp_path, SecretScrubber())
    execution = runner.run(
        _invocation(
            tmp_path,
            "(ctrl / 'observed').write_text("
            "Path(sys.argv[3]).read_text(encoding='utf-8'), encoding='utf-8')\n"
            "output.write_text('ok', encoding='utf-8')\n",
            command_template=(
                sys.executable,
                str(tmp_path / "agent.py"),
                "{prompt_file}",
                "{output_file}",
                str(record_path),
            ),
            process_record_path=record_path,
        )
    )
    assert execution.succeeded
    observed = json.loads((tmp_path / "observed").read_text(encoding="utf-8"))
    assert observed["pid"] > 0
    assert observed["pgid"] == observed["pid"]
    assert not record_path.exists()  # removed after reaping


def test_terminate_recorded_process_handles_missing_dead_and_reused_pids(
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "agent-process.json"
    assert terminate_recorded_process(record_path) is False  # no record

    atomic_write_json(record_path, {"pid": 2**22 + 12345, "command": "x"})
    assert terminate_recorded_process(record_path) is False  # dead pid
    assert not record_path.exists()

    # A live PID whose argv does not match the record is treated as reused
    # and deliberately NOT killed: the decoy below must survive the call.
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        atomic_write_json(
            record_path, {"pid": decoy.pid, "command": "/definitely/not/python"}
        )
        assert terminate_recorded_process(record_path) is False
        assert not record_path.exists()
        assert decoy.poll() is None  # still alive: reused PIDs are spared
    finally:
        decoy.kill()
        decoy.wait()
