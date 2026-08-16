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

import dagvane.adapters.agents.subprocess_runner as subprocess_runner_module
from dagvane.adapters.agents.subprocess_runner import (
    SubprocessAgentRunner,
    child_environment,
    terminate_recorded_process,
)
from dagvane.domain.models import SpecError, StorageError
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.agent import AgentInvocation
from dagvane.ports.runtime import (
    FixedClock,
    SteppingMonotonic,
    SystemClock,
    SystemIds,
    SystemMonotonic,
)
from dagvane.workspace.paths import atomic_write_bytes_at, atomic_write_json


def _runner(tmp_path: Path, scrubber: SecretScrubber, **kwargs: int) -> SubprocessAgentRunner:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    return SubprocessAgentRunner(
        runs_dir=runs_dir,
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
    record_root = tmp_path / "records"
    record_root.mkdir()
    owner_dir = record_root / "run-a"
    owner_dir.mkdir()
    record_path = owner_dir / "agent-process.json"
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
            process_record_root=record_root,
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
    record_root = tmp_path / "records"
    record_root.mkdir()
    owner_dir = record_root / "run-a"
    owner_dir.mkdir()
    record_path = owner_dir / "agent-process.json"
    assert (
        terminate_recorded_process(record_path, allowed_root=record_root) is False
    )  # no record

    atomic_write_json(record_path, {"pid": 2**22 + 12345, "command": "x"})
    assert (
        terminate_recorded_process(record_path, allowed_root=record_root) is False
    )  # dead pid
    assert not record_path.exists()

    # A live PID whose argv does not match the record is treated as reused
    # and deliberately NOT killed: the decoy below must survive the call.
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        atomic_write_json(
            record_path, {"pid": decoy.pid, "command": "/definitely/not/python"}
        )
        assert (
            terminate_recorded_process(record_path, allowed_root=record_root) is False
        )
        assert not record_path.exists()
        assert decoy.poll() is None  # still alive: reused PIDs are spared
    finally:
        decoy.kill()
        decoy.wait()


def test_foreign_goal_attempt_record_is_rejected_before_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path-owned record carrying another Goal/attempt never authorizes a signal.

    The current runner's binding remains the pinned owner path plus the
    recorded command/PID reuse guard; this regression keeps the foreign
    embedded identity case explicit without changing that mechanism.
    """
    record_root = tmp_path / "records"
    record_root.mkdir()
    owner_dir = record_root / "goal-a"
    owner_dir.mkdir()
    record_path = owner_dir / "agent-process.json"
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    kills: list[tuple[int, object]] = []
    monkeypatch.setattr(
        subprocess_runner_module,
        "kill_process_group",
        lambda pid, sig: kills.append((pid, sig)),
    )
    try:
        atomic_write_json(
            record_path,
            {
                "pid": decoy.pid,
                "command": "/definitely-not-the-decoy-command",
                "goal_name": "foreign-goal",
                "run_id": "foreign-attempt",
            },
        )
        assert terminate_recorded_process(record_path, allowed_root=record_root) is False
        assert kills == []
        assert decoy.poll() is None
        assert not record_path.exists()
    finally:
        decoy.kill()
        decoy.wait()


# =============================================================================
# Execution-storage consistency checkpoint (runs_dir canonicality, execution
# id validation, exclusive run-dir creation, fixed-leaf hierarchy checks).
# =============================================================================


class _FixedIds:
    """Deterministic/malicious IdSource double: returns a queued value."""

    def __init__(self, values: list[object]) -> None:
        self._values = list(values)
        self.calls = 0

    def new_id(self, kind: str) -> str:
        self.calls += 1
        return self._values.pop(0)  # type: ignore[return-value]


class _SpyClock:
    """Wraps SystemClock, counting reads: proves no clock effect precedes id
    validation for a rejected execution id."""

    def __init__(self) -> None:
        self.calls = 0
        self._inner = FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)

    def now_iso(self) -> str:
        self.calls += 1
        return self._inner.now_iso()


class _SpyMonotonic:
    def __init__(self) -> None:
        self.calls = 0
        self._inner = SteppingMonotonic()

    def now_ms(self) -> int:
        self.calls += 1
        return self._inner.now_ms()


def _new_runner(
    runs_dir: Path, ids: object, *, clock: object | None = None, monotonic: object | None = None
) -> SubprocessAgentRunner:
    return SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=clock if clock is not None else SystemClock(),  # type: ignore[arg-type]
        monotonic=monotonic if monotonic is not None else SystemMonotonic(),  # type: ignore[arg-type]
        ids=ids,  # type: ignore[arg-type]
        scrubber=SecretScrubber(),
    )


def test_non_string_execution_id_rejected_before_any_effect(tmp_path: Path) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    ids = _FixedIds([123])
    clock = _SpyClock()
    monotonic = _SpyMonotonic()
    runner = _new_runner(runs_dir, ids, clock=clock, monotonic=monotonic)
    with pytest.raises(SpecError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))
    assert clock.calls == 0
    assert monotonic.calls == 0
    assert list(runs_dir.iterdir()) == []


def test_dot_dot_execution_id_rejected_before_any_effect(tmp_path: Path) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    ids = _FixedIds(["../evil"])
    clock = _SpyClock()
    runner = _new_runner(runs_dir, ids, clock=clock)
    with pytest.raises(SpecError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))
    assert clock.calls == 0
    assert list(runs_dir.iterdir()) == []


def test_runs_dir_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        _new_runner(Path("relative/agent-runs"), SystemIds())


def test_runs_dir_dot_dot_component_rejected(tmp_path: Path) -> None:
    real = tmp_path / "agent-runs"
    real.mkdir()
    with pytest.raises(StorageError):
        _new_runner(tmp_path / "unrelated" / ".." / "agent-runs", SystemIds())


def test_runs_dir_symlink_ancestor_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    runs_dir = real_parent / "agent-runs"
    runs_dir.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(StorageError):
        _new_runner(link_parent / "agent-runs", SystemIds())


def test_runs_dir_wrong_type_rejected(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "agent-runs"
    not_a_dir.write_bytes(b"not a directory")
    with pytest.raises(StorageError):
        _new_runner(not_a_dir, SystemIds())


def test_runs_dir_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        _new_runner(tmp_path / "does-not-exist", SystemIds())


def test_existing_run_directory_collision_preserves_bytes_and_spawns_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    collision = runs_dir / "agent-fixed"
    collision.mkdir()
    sentinel = collision / "sentinel.txt"
    sentinel.write_bytes(b"pre-existing bytes")

    def _no_spawn(*_a: object, **_k: object) -> None:
        pytest.fail("spawned a child")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    runner = _new_runner(runs_dir, _FixedIds(["agent-fixed"]))
    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))
    assert sentinel.read_bytes() == b"pre-existing bytes"
    assert sorted(p.name for p in collision.iterdir()) == ["sentinel.txt"]


def test_existing_run_directory_symlink_collision_preserves_link_and_spawns_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    collision = runs_dir / "agent-fixed"
    collision.symlink_to(real_target, target_is_directory=True)

    def _no_spawn(*_a: object, **_k: object) -> None:
        pytest.fail("spawned a child")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    runner = _new_runner(runs_dir, _FixedIds(["agent-fixed"]))
    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))
    assert collision.is_symlink()
    assert collision.resolve() == real_target.resolve()


def test_child_created_output_raw_symlink_raises_without_reading_target(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("top secret contents", encoding="utf-8")
    runner = _runner(tmp_path, SecretScrubber())
    execution = None
    with pytest.raises(StorageError):
        execution = runner.run(
            _invocation(
                tmp_path,
                "output.symlink_to(Path(sys.argv[3]))\n",
                command_template=(
                    sys.executable,
                    str(tmp_path / "agent.py"),
                    "{prompt_file}",
                    "{output_file}",
                    str(secret_file),
                ),
            )
        )
    assert execution is None
    assert secret_file.read_text(encoding="utf-8") == "top secret contents"


def test_child_created_output_raw_wrong_type_raises(tmp_path: Path) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.mkdir()\n"))


def _hook_after_log_write(
    monkeypatch: pytest.MonkeyPatch, attack: object
) -> None:
    """Run ``attack(run_dir)`` exactly once, right after the real
    ``stream.log`` write lands (resolved relative to the pinned run-dir fd)
    and before the raw-output read/cleanup that follows it — the natural
    seam between "child has fully exited, stream is captured" and "raw
    output is consumed" in the fixed runner. The real run directory's
    pathname is recovered from the pinned fd (``/proc/self/fd``) purely to
    stage the attack by pathname, mirroring what an external attacker with
    only pathname access — not the fd — could still see and do.
    """
    fired = False

    def wrapper(dir_fd: int, name: str, data: bytes) -> None:
        nonlocal fired
        atomic_write_bytes_at(dir_fd, name, data)
        if not fired and name == "stream.log":
            fired = True
            run_dir = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            attack(run_dir)  # type: ignore[operator]

    monkeypatch.setattr(subprocess_runner_module, "atomic_write_bytes_at", wrapper)


def test_output_raw_swapped_to_outside_symlink_after_log_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file swap racing between the checkpoint's read of ``output.raw``
    and the moment it lands: once the child has exited and the stream is
    captured, an attacker (a surviving descendant sharing the run
    directory) removes the legitimate ``output.raw`` and plants a symlink
    to an outside file in its place. The fixed reader opens the fixed leaf
    strictly relative to the pinned run-directory fd with ``O_NOFOLLOW``, so
    the swapped symlink is refused outright — never opened, never read,
    never unlinked."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"OUTSIDE SENTINEL BYTES")
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber)
    invocation = _invocation(
        tmp_path, "output.write_text('legit content', encoding='utf-8')\n"
    )

    captured: dict[str, Path] = {}

    def real_attack(run_dir: Path) -> None:
        raw = run_dir / "output.raw"
        raw.unlink()
        raw.symlink_to(outside)
        captured["raw"] = raw

    _hook_after_log_write(monkeypatch, real_attack)

    with pytest.raises(StorageError):
        runner.run(invocation)

    assert outside.read_bytes() == b"OUTSIDE SENTINEL BYTES"
    assert captured["raw"].is_symlink()  # left untouched, never unlinked
    run_dir = captured["raw"].parent
    assert not (run_dir / "output.md").exists()


def test_run_dir_replaced_by_outside_symlink_before_cleanup_never_touches_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-dir swap racing the pathname-based cleanup: once the child has
    exited and the stream is captured, an attacker renames the real run
    directory aside and plants a symlink at its old path pointing to an
    unrelated outside directory (with its own ``output.raw``). The fixed
    checkpoint pins the real run directory by fd before spawning, so both
    the raw-output read and its removal are resolved against that fd — never
    by re-walking the (now hijacked) pathname — so the outside directory's
    ``output.raw`` is neither read nor deleted, and the real one is cleaned
    up correctly."""
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    outside_raw = outside_dir / "output.raw"
    outside_raw.write_bytes(b"OUTSIDE RAW SENTINEL")
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber)
    invocation = _invocation(
        tmp_path, "output.write_text('real content', encoding='utf-8')\n"
    )

    moved: dict[str, Path] = {}

    def real_attack(run_dir: Path) -> None:
        moved_aside = tmp_path / "moved-run-dir"
        run_dir.rename(moved_aside)
        run_dir.symlink_to(outside_dir, target_is_directory=True)
        moved["run_dir"] = moved_aside

    _hook_after_log_write(monkeypatch, real_attack)

    with pytest.raises(StorageError):
        runner.run(invocation)

    # Outside bytes are untouched: not read into anything persisted, not
    # deleted.
    assert outside_raw.read_bytes() == b"OUTSIDE RAW SENTINEL"
    assert not (outside_dir / "output.md").exists()
    # The real run directory's own output.raw was removed through the
    # pinned dirfd, proving cleanup followed the fd, not the hijacked path.
    assert not (moved["run_dir"] / "output.raw").exists()


def test_run_dir_replaced_by_ordinary_directory_after_log_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-dir swap to an ordinary (non-symlink) replacement directory:
    once the child has exited and the stream is captured, an attacker
    renames the real run directory aside and creates a plain directory at
    its old path. Every fixed-leaf write below is already resolved through
    the pinned run-dir fd, so this swap cannot redirect them — the real
    artifacts land in the moved-aside directory, and the fresh replacement
    stays empty — but the checkpoint must still refuse to report success,
    because the returned pathnames would otherwise appear to point into the
    ordinary-directory replacement rather than the real, owned directory."""
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber)
    invocation = _invocation(
        tmp_path, "output.write_text('real content', encoding='utf-8')\n"
    )

    moved: dict[str, Path] = {}

    def real_attack(run_dir: Path) -> None:
        moved_aside = tmp_path / "moved-run-dir-ordinary"
        run_dir.rename(moved_aside)
        run_dir.mkdir()
        moved["run_dir"] = moved_aside
        moved["replacement"] = run_dir

    _hook_after_log_write(monkeypatch, real_attack)

    with pytest.raises(StorageError):
        runner.run(invocation)

    assert list(moved["replacement"].iterdir()) == []  # untouched, empty
    assert (moved["run_dir"] / "output.md").read_text(encoding="utf-8") == "real content"
    assert (moved["run_dir"] / "prompt.md").exists()
    assert (moved["run_dir"] / "stream.log").exists()


def test_run_dir_swapped_during_output_write_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-dir swap timed to land immediately around the ``output.md``
    write itself — the historical "validate, then act by pathname" seam:
    the attacker swaps the run directory for an outside symlink right
    before Dagvane's final artifact write. Because that write is resolved
    through the pinned run-dir fd, it still lands in the real, owned
    directory — never into the swapped-in outside target — but the
    checkpoint must still fail closed, since the public run_dir pathname no
    longer names that directory."""
    outside_dir = tmp_path / "outside-during-write"
    outside_dir.mkdir()
    scrubber = SecretScrubber()
    runner = _runner(tmp_path, scrubber)
    invocation = _invocation(
        tmp_path, "output.write_text('final content', encoding='utf-8')\n"
    )

    fired = False
    moved: dict[str, Path] = {}

    def wrapper(dir_fd: int, name: str, data: bytes) -> None:
        nonlocal fired
        if not fired and name == "output.md":
            fired = True
            run_dir = Path(os.readlink(f"/proc/self/fd/{dir_fd}"))
            moved_aside = tmp_path / "moved-run-dir-during-write"
            run_dir.rename(moved_aside)
            run_dir.symlink_to(outside_dir, target_is_directory=True)
            moved["run_dir"] = moved_aside
        atomic_write_bytes_at(dir_fd, name, data)

    monkeypatch.setattr(subprocess_runner_module, "atomic_write_bytes_at", wrapper)

    with pytest.raises(StorageError):
        runner.run(invocation)

    assert not (outside_dir / "output.md").exists()
    assert (
        moved["run_dir"] / "output.md"
    ).read_text(encoding="utf-8") == "final content"


def test_fake_execution_still_succeeds_with_hardened_storage_checkpoint(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    execution = runner.run(
        _invocation(tmp_path, "output.write_text('final answer', encoding='utf-8')\n")
    )
    assert execution.succeeded
    assert execution.output_text == "final answer"
    run_dir = Path(execution.output_path).parent
    assert run_dir.parent == tmp_path / "agent-runs"
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "output.md",
        "prompt.md",
        "stream.log",
    ]


# =============================================================================
# Root-binding checkpoint: runs_dir is canonical only at construction; a
# post-construction replacement (symlink or ordinary directory swapped in at
# the same pathname, before or mid-``run()``) must never let creation or
# pinned artifacts land outside the originally trusted root.
# =============================================================================


def test_runs_dir_replaced_by_outside_symlink_before_run_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    ids = _FixedIds(["agent-fixed"])
    clock = _SpyClock()
    runner = _new_runner(runs_dir, ids, clock=clock)

    outside = tmp_path / "outside-root"
    outside.mkdir()
    moved_aside = tmp_path / "moved-real-runs-dir"
    runs_dir.rename(moved_aside)
    runs_dir.symlink_to(outside, target_is_directory=True)

    def _no_spawn(*_a: object, **_k: object) -> None:
        pytest.fail("spawned a child")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))

    assert clock.calls == 0
    assert list(outside.iterdir()) == []
    assert list(moved_aside.iterdir()) == []


def test_runs_dir_replaced_by_ordinary_directory_before_run_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    ids = _FixedIds(["agent-fixed"])
    clock = _SpyClock()
    runner = _new_runner(runs_dir, ids, clock=clock)

    moved_aside = tmp_path / "moved-real-runs-dir-2"
    runs_dir.rename(moved_aside)
    runs_dir.mkdir()  # fresh, ordinary directory at the same pathname

    def _no_spawn(*_a: object, **_k: object) -> None:
        pytest.fail("spawned a child")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))

    assert clock.calls == 0
    assert list(runs_dir.iterdir()) == []  # replacement stays empty
    assert list(moved_aside.iterdir()) == []  # real root untouched


def test_root_swapped_between_root_fd_open_and_child_mkdir_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root fd is opened and its identity checked; before the exclusive
    ``mkdir(execution_id, dir_fd=root_fd)`` that follows, an attacker renames
    the real root aside and plants a replacement (ordinary directory) at its
    old pathname. A dir_fd-relative mkdir would otherwise succeed straight
    into the (still held open) real root undetected — the fixed checkpoint
    re-proves the root pathname's identity after the mkdir and before any
    clock read or spawn, so this must still fail closed."""
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    ids = _FixedIds(["agent-fixed"])
    clock = _SpyClock()
    runner = _new_runner(runs_dir, ids, clock=clock)

    real_mkdir = os.mkdir
    fired = False
    moved: dict[str, Path] = {}

    def wrapper(path: object, *args: object, **kwargs: object) -> None:
        nonlocal fired
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
        if not fired and kwargs.get("dir_fd") is not None:
            fired = True
            moved_aside = tmp_path / "moved-real-runs-dir-3"
            runs_dir.rename(moved_aside)
            runs_dir.mkdir()
            moved["run_dir"] = moved_aside

    monkeypatch.setattr(os, "mkdir", wrapper)

    def _no_spawn(*_a: object, **_k: object) -> None:
        pytest.fail("spawned a child")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)

    with pytest.raises(StorageError):
        runner.run(_invocation(tmp_path, "output.write_text('x')\n"))

    assert clock.calls == 0
    assert list(runs_dir.iterdir()) == []  # fresh replacement stays empty
    # The execution directory was created relative to the pinned root fd,
    # so it landed in the real (moved-aside) root, not the replacement —
    # correct fd behavior, and still no artifact or spawn beyond that mkdir.
    assert sorted(p.name for p in moved["run_dir"].iterdir()) == ["agent-fixed"]
    assert list((moved["run_dir"] / "agent-fixed").iterdir()) == []


# =============================================================================
# Prompt provenance: a normal command child can mutate prompt.md before exit;
# acceptance must verify the exact scrubbed bytes and restore them on any
# deviation, never publishing forged provenance. Mutation by a surviving
# descendant *after* acceptance is later process-lifecycle scope (group
# termination / orphan reconciliation), deliberately not covered here.
# =============================================================================


def _single_run_dir(tmp_path: Path) -> Path:
    entries = list((tmp_path / "agent-runs").iterdir())
    assert len(entries) == 1
    return entries[0]


def test_child_overwriting_prompt_is_refused_and_prompt_restored(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    with pytest.raises(StorageError):
        runner.run(
            _invocation(
                tmp_path,
                "Path(sys.argv[1]).write_text('forged prompt', encoding='utf-8')\n"
                "output.write_text('answer', encoding='utf-8')\n",
            )
        )
    prompt = _single_run_dir(tmp_path) / "prompt.md"
    assert not prompt.is_symlink()
    assert prompt.read_text(encoding="utf-8") == "the prompt"


def test_child_deleting_prompt_is_refused_and_prompt_restored(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    with pytest.raises(StorageError):
        runner.run(
            _invocation(
                tmp_path,
                "Path(sys.argv[1]).unlink()\n"
                "output.write_text('answer', encoding='utf-8')\n",
            )
        )
    prompt = _single_run_dir(tmp_path) / "prompt.md"
    assert not prompt.is_symlink()
    assert prompt.read_text(encoding="utf-8") == "the prompt"


def test_child_symlinking_prompt_never_reads_outside_and_prompt_restored(
    tmp_path: Path,
) -> None:
    """The child replaces prompt.md with a symlink to an outside file whose
    content happens to equal the scrubbed prompt: a verifier that followed
    the link would compare equal and accept forged provenance. ``O_NOFOLLOW``
    refuses the link without ever opening its target; the restore replaces
    the symlink entry itself, leaving the outside file untouched."""
    outside = tmp_path / "outside-prompt.txt"
    outside.write_text("the prompt", encoding="utf-8")
    runner = _runner(tmp_path, SecretScrubber())
    with pytest.raises(StorageError):
        runner.run(
            _invocation(
                tmp_path,
                "p = Path(sys.argv[1])\n"
                "p.unlink()\n"
                "p.symlink_to(Path(sys.argv[3]))\n"
                "output.write_text('answer', encoding='utf-8')\n",
                command_template=(
                    sys.executable,
                    str(tmp_path / "agent.py"),
                    "{prompt_file}",
                    "{output_file}",
                    str(outside),
                ),
            )
        )
    assert outside.read_text(encoding="utf-8") == "the prompt"  # untouched
    prompt = _single_run_dir(tmp_path) / "prompt.md"
    assert not prompt.is_symlink()  # the link entry itself was replaced
    assert prompt.read_text(encoding="utf-8") == "the prompt"


def test_child_replacing_prompt_with_directory_is_refused_and_prompt_restored(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path, SecretScrubber())
    with pytest.raises(StorageError):
        runner.run(
            _invocation(
                tmp_path,
                "p = Path(sys.argv[1])\n"
                "p.unlink()\n"
                "p.mkdir()\n"
                "output.write_text('answer', encoding='utf-8')\n",
            )
        )
    prompt = _single_run_dir(tmp_path) / "prompt.md"
    assert prompt.is_file() and not prompt.is_symlink()
    assert prompt.read_text(encoding="utf-8") == "the prompt"
