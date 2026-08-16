"""Process-record checkpoint contract: ``validate_process_record_location``,
the ``_PinnedOwnerDir`` fd-pinned owner directory, and
``terminate_recorded_process``'s contained (never-raising) status handling.

Offline: no real agent process is spawned except a trivial sleeping decoy
used to prove a live/reused pid is spared.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import dagvane.adapters.agents.subprocess_runner as subprocess_runner_module
from dagvane.adapters.agents.subprocess_runner import (
    SubprocessAgentRunner,
    _PinnedOwnerDir,
    terminate_recorded_process,
    validate_process_record_location,
)
from dagvane.adapters.localexec import terminate_and_reap
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentInvocation
from dagvane.ports.runtime import FixedClock, SequentialIds, SteppingMonotonic
from dagvane.workspace.paths import atomic_write_json

_LEAF = "agent-process.json"


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


# =============================================================================
# 1. validate_process_record_location: pairing, non-Path, and the invalid
#    hierarchy matrix. Every case is a fail-closed StorageError/SpecError
#    with no filesystem mutation.
# =============================================================================


def test_rejects_non_path_root_and_path(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    valid_path = root / "owner" / _LEAF
    with pytest.raises(SpecError):
        validate_process_record_location(str(root), valid_path, ctx="t")
    with pytest.raises(SpecError):
        validate_process_record_location(root, str(valid_path), ctx="t")
    with pytest.raises(SpecError):
        validate_process_record_location(str(root), str(valid_path), ctx="t")


def _traversal(root: Path) -> Path:
    return root / ".." / "owner" / _LEAF


def _outside(root: Path) -> Path:
    return root.parent / "elsewhere" / "owner" / _LEAF


def _is_root(root: Path) -> Path:
    return root


def _is_parent(root: Path) -> Path:
    return root.parent


def _same_prefix_sibling(root: Path) -> Path:
    return root.parent / (root.name + "-evil") / "owner" / _LEAF


def _extra_depth(root: Path) -> Path:
    return root / "owner" / "sub" / _LEAF


def _wrong_leaf(root: Path) -> Path:
    return root / "owner" / "wrong.json"


def _invalid_owner(root: Path) -> Path:
    return root / ".hidden" / _LEAF


def _symlink_owner(root: Path) -> Path:
    real_owner = root / "real-owner"
    real_owner.mkdir()
    (root / "owner").symlink_to(real_owner)
    return root / "owner" / _LEAF


def _symlink_leaf(root: Path) -> Path:
    owner = root / "owner"
    owner.mkdir()
    outside = root.parent / "outside-leaf-target"
    outside.write_bytes(b'{"pid": 1}')
    (owner / _LEAF).symlink_to(outside)
    return owner / _LEAF


def _nonregular_leaf(root: Path) -> Path:
    owner = root / "owner"
    owner.mkdir()
    (owner / _LEAF).mkdir()
    return owner / _LEAF


@pytest.mark.parametrize(
    "build,expected_exc",
    [
        (_traversal, StorageError),
        (_outside, StorageError),
        (_is_root, StorageError),
        (_is_parent, StorageError),
        (_same_prefix_sibling, StorageError),
        (_extra_depth, StorageError),
        (_wrong_leaf, StorageError),
        (_invalid_owner, SpecError),
        (_symlink_owner, StorageError),
        (_symlink_leaf, StorageError),
        (_nonregular_leaf, StorageError),
    ],
    ids=[
        "traversal",
        "outside",
        "is-root",
        "is-parent",
        "same-prefix-sibling",
        "extra-depth",
        "wrong-leaf",
        "invalid-owner",
        "symlink-owner",
        "symlink-leaf",
        "nonregular-leaf",
    ],
)
def test_invalid_hierarchy_matrix_fails_closed(
    tmp_path: Path,
    build: Callable[[Path], Path],
    expected_exc: type[Exception],
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    path = build(root)
    with pytest.raises(expected_exc):
        validate_process_record_location(root, path, ctx="t")


def test_valid_hierarchy_returns_owner_dir(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    path = owner / _LEAF
    assert validate_process_record_location(root, path, ctx="t") == owner


def test_pairing_rejected_before_id_clock_env_rundir_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    record_path = owner / _LEAF

    ids = SequentialIds(seed="t")
    original_new_id = ids.new_id
    calls: list[str] = []

    def spying_new_id(kind: str) -> str:
        calls.append(kind)
        return original_new_id(kind)

    monkeypatch.setattr(ids, "new_id", spying_new_id)

    def failing_popen(*args: object, **kwargs: object) -> object:
        raise AssertionError("Popen must not be called for a rejected pairing")

    monkeypatch.setattr("dagvane.adapters.agents.subprocess_runner.subprocess.Popen", failing_popen)

    runner = SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=_clock(),
        monotonic=SteppingMonotonic(),
        ids=ids,
    )

    # path without root.
    invocation = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "pass"),
        process_record_path=record_path,
        process_record_root=None,
    )
    with pytest.raises(SpecError):
        runner.run(invocation)

    # root without path.
    invocation2 = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "pass"),
        process_record_path=None,
        process_record_root=root,
    )
    with pytest.raises(SpecError):
        runner.run(invocation2)

    assert calls == []  # the id source was never consulted
    assert list(runs_dir.iterdir()) == []  # no run directory was created


def test_invalid_hierarchy_rejected_before_id_clock_env_rundir_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    root = tmp_path / "records"
    root.mkdir()

    ids = SequentialIds(seed="t")
    calls: list[str] = []

    def spying_new_id(kind: str) -> str:
        calls.append(kind)
        return "unreachable"

    monkeypatch.setattr(ids, "new_id", spying_new_id)

    def failing_popen(*args: object, **kwargs: object) -> object:
        raise AssertionError("Popen must not be called for an invalid hierarchy")

    monkeypatch.setattr("dagvane.adapters.agents.subprocess_runner.subprocess.Popen", failing_popen)

    runner = SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=_clock(),
        monotonic=SteppingMonotonic(),
        ids=ids,
    )
    invocation = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "pass"),
        process_record_path=root,  # equal to root: invalid hierarchy
        process_record_root=root,
    )
    with pytest.raises(StorageError):
        runner.run(invocation)
    assert calls == []
    assert list(runs_dir.iterdir()) == []


# =============================================================================
# 2. Valid runner process-record lifecycle: covered end-to-end by
#    tests/unit/test_agent_runner.py::test_process_record_exists_while_running_and_is_removed_after.
#    Add the resume/orphan-reconciliation counterpart: a record for a still
#    running process is terminated and removed.
# =============================================================================


def _spawn_orphaned_grandchild() -> int:
    """Fork+setsid a sleeping grandchild and exit the immediate child, so
    the grandchild is reparented to init and this test process never has to
    ``wait()`` it — exactly the non-parent situation ``terminate_recorded_
    process`` documents (poll for death, orphan reaped by init)."""
    spawner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os, sys, time\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    os.setsid()\n"
            "    time.sleep(60)\n"
            "    sys.exit(0)\n"
            "else:\n"
            "    print(pid, flush=True)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = spawner.stdout.readline() if spawner.stdout else ""
    spawner.wait(timeout=5)
    return int(line.strip())


def test_terminate_recorded_process_terminates_live_matching_pid(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    record_path = owner / _LEAF
    pid = _spawn_orphaned_grandchild()
    try:
        atomic_write_json(
            record_path,
            {"pid": pid, "command": sys.executable},
            allowed_root=root,
        )
        assert terminate_recorded_process(record_path, allowed_root=root) is True
        assert not record_path.exists()

        def _dead() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        deadline = time.time() + 5.0
        while time.time() < deadline and not _dead():
            time.sleep(0.05)
        assert _dead()
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# =============================================================================
# 3. Missing/malformed/dead/reused terminator records: contained (False),
#    never raises. Missing/dead/reused already covered in
#    tests/unit/test_agent_runner.py; add the remaining shapes.
# =============================================================================


def test_missing_owner_directory_returns_false(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    record_path = root / "never-existed" / _LEAF
    assert terminate_recorded_process(record_path, allowed_root=root) is False


@pytest.mark.parametrize(
    "write_bytes",
    [
        b"not json at all",
        b'{"pid": "not-an-int", "command": "x"}',
        b'{"pid": -1, "command": "x"}',
        b'{"pid": 0, "command": "x"}',
        b"[1, 2, 3]",
        b"\xff\xfe not utf-8",
    ],
    ids=["invalid-json", "non-int-pid", "negative-pid", "zero-pid", "non-dict", "bad-utf8"],
)
def test_malformed_record_returns_false_and_is_removed(
    tmp_path: Path, write_bytes: bytes
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    record_path = owner / _LEAF
    record_path.write_bytes(write_bytes)
    assert terminate_recorded_process(record_path, allowed_root=root) is False
    assert not record_path.exists()


def test_oversized_record_returns_false_and_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    record_path = owner / _LEAF
    padding = "x" * subprocess_runner_module._MAX_PROCESS_RECORD_BYTES
    record_path.write_bytes(json.dumps({"pid": 1, "padding": padding}).encode())
    assert terminate_recorded_process(record_path, allowed_root=root) is False
    assert not record_path.exists()


# =============================================================================
# 4. Owner directory swapped to an ordinary directory or a symlink between
#    validation and a record effect: fail closed, replacement/outside bytes
#    untouched.
# =============================================================================


def test_write_record_fails_closed_when_owner_swapped_for_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    pinned = _PinnedOwnerDir(root, owner / _LEAF, ctx="t")
    try:
        replacement = root / "run-a-replacement"
        replacement.mkdir()
        (replacement / "sentinel.txt").write_bytes(b"replacement bytes")
        owner.rename(root / "run-a-original")
        replacement.rename(owner)
        with pytest.raises(StorageError):
            pinned.write_record({"pid": 1})
        assert (owner / "sentinel.txt").read_bytes() == b"replacement bytes"
        assert not (owner / _LEAF).exists()
    finally:
        pinned.close()


def test_write_record_fails_closed_when_owner_swapped_for_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    pinned = _PinnedOwnerDir(root, owner / _LEAF, ctx="t")
    try:
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "sentinel.txt").write_bytes(b"outside bytes")
        owner.rmdir()
        owner.symlink_to(outside)
        with pytest.raises(StorageError):
            pinned.write_record({"pid": 1})
        assert (outside / "sentinel.txt").read_bytes() == b"outside bytes"
        assert not (outside / _LEAF).exists()
    finally:
        pinned.close()


def test_unlink_record_fails_closed_when_owner_swapped(tmp_path: Path) -> None:
    root = tmp_path / "records"
    root.mkdir()
    owner = root / "run-a"
    owner.mkdir()
    atomic_write_json(owner / _LEAF, {"pid": 1}, allowed_root=root)
    pinned = _PinnedOwnerDir(root, owner / _LEAF, ctx="t")
    try:
        replacement = root / "run-a-replacement"
        replacement.mkdir()
        (replacement / _LEAF).write_bytes(b"replacement record bytes")
        owner.rename(root / "run-a-original")
        replacement.rename(owner)
        with pytest.raises(StorageError):
            pinned.unlink_record()
        assert (owner / _LEAF).read_bytes() == b"replacement record bytes"
    finally:
        pinned.close()


# =============================================================================
# 5. Authority ROOT swapped (renamed aside and replaced by an ordinary
#    directory containing the same owner name, or by a symlink to an outside
#    directory) after initial validation/pinning but before a record effect
#    or signal decision: every effect fails closed with StorageError, no
#    signal is sent, and bytes in the original, replacement, parent, sibling
#    and outside locations stay byte-identical.
# =============================================================================


class _RootSwapHarness:
    """``<tmp>/records/run-a`` plus sentinel bytes in every location a
    refusal must leave untouched. ``swap()`` renames the root aside and
    replaces it after the authority has already been validated and pinned."""

    def __init__(self, tmp_path: Path, kind: str) -> None:
        self.tmp_path = tmp_path
        self.kind = kind
        self.root = tmp_path / "records"
        self.root.mkdir()
        self.owner = self.root / "run-a"
        self.owner.mkdir()
        self.record_path = self.owner / _LEAF
        self.original_root = tmp_path / "records-original"
        self.original_owner = self.original_root / "run-a"
        self._sentinels: dict[Path, bytes] = {}
        # Planted pre-swap (they travel with the renamed-aside root), so
        # their preserved bytes are expected at the post-swap location.
        (self.owner / "sentinel.txt").write_bytes(b"original owner bytes")
        self._sentinels[self.original_owner / "sentinel.txt"] = b"original owner bytes"
        sibling = self.root / "run-b"
        sibling.mkdir()
        (sibling / "sentinel.txt").write_bytes(b"sibling owner bytes")
        self._sentinels[self.original_root / "run-b" / "sentinel.txt"] = (
            b"sibling owner bytes"
        )
        parent_sentinel = tmp_path / "parent-sentinel.txt"
        parent_sentinel.write_bytes(b"parent bytes")
        self._sentinels[parent_sentinel] = b"parent bytes"
        outside_sentinel = tmp_path / "outside-sentinel.txt"
        outside_sentinel.write_bytes(b"outside bytes")
        self._sentinels[outside_sentinel] = b"outside bytes"
        self.swapped_owner: Path | None = None

    def swap(self, *, plant_replacement_leaf: bool = False) -> None:
        self.root.rename(self.original_root)
        if self.kind == "directory":
            self.root.mkdir()
            replacement_owner = self.root / "run-a"
        else:
            outside_root = self.tmp_path / "outside-root"
            outside_root.mkdir()
            self.root.symlink_to(outside_root)
            replacement_owner = outside_root / "run-a"
        replacement_owner.mkdir()
        (replacement_owner / "sentinel.txt").write_bytes(b"replacement owner bytes")
        self._sentinels[replacement_owner / "sentinel.txt"] = (
            b"replacement owner bytes"
        )
        if plant_replacement_leaf:
            (replacement_owner / _LEAF).write_bytes(b"replacement record bytes")
            self._sentinels[replacement_owner / _LEAF] = b"replacement record bytes"
        self.swapped_owner = replacement_owner

    def assert_untouched(self) -> None:
        for path, data in self._sentinels.items():
            assert path.read_bytes() == data, path

    def assert_no_leaf_anywhere(self) -> None:
        assert self.swapped_owner is not None
        for directory in (
            self.original_owner,
            self.original_root / "run-b",
            self.swapped_owner,
            self.tmp_path,
        ):
            assert not (directory / _LEAF).exists(), directory


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_pinned_write_fails_closed_when_root_swapped(
    tmp_path: Path, kind: str
) -> None:
    harness = _RootSwapHarness(tmp_path, kind)
    pinned = _PinnedOwnerDir(harness.root, harness.record_path, ctx="t")
    try:
        harness.swap()
        with pytest.raises(StorageError):
            pinned.write_record({"pid": 1})
    finally:
        pinned.close()
    harness.assert_untouched()
    harness.assert_no_leaf_anywhere()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_pinned_unlink_fails_closed_when_root_swapped(
    tmp_path: Path, kind: str
) -> None:
    harness = _RootSwapHarness(tmp_path, kind)
    atomic_write_json(harness.record_path, {"pid": 1}, allowed_root=harness.root)
    pinned = _PinnedOwnerDir(harness.root, harness.record_path, ctx="t")
    try:
        harness.swap(plant_replacement_leaf=True)
        with pytest.raises(StorageError):
            pinned.unlink_record()
    finally:
        pinned.close()
    harness.assert_untouched()
    # The record written before the swap is preserved in the renamed-aside
    # original owner; the replacement's planted leaf was not unlinked.
    assert json.loads(
        (harness.original_owner / _LEAF).read_text(encoding="utf-8")
    ) == {"pid": 1}


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_runner_write_record_fails_closed_when_root_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Root swapped after the runner's initial validation/pinning (injected
    at the started-timestamp read, before the spawn): the record write fails
    closed, the already-spawned child is terminated and reaped, and no
    record lands anywhere."""
    harness = _RootSwapHarness(tmp_path, kind)
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()

    class _SwapOnFirstCallClock:
        def __init__(self) -> None:
            self._inner = _clock()
            self._swapped = False

        def now_iso(self) -> str:
            if not self._swapped:
                self._swapped = True
                harness.swap()
            return self._inner.now_iso()

    reaped: list[subprocess.Popen[bytes]] = []
    original_reap = terminate_and_reap

    def spying_reap(proc: subprocess.Popen[bytes]) -> None:
        reaped.append(proc)
        original_reap(proc)

    monkeypatch.setattr(subprocess_runner_module, "terminate_and_reap", spying_reap)

    runner = SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=_SwapOnFirstCallClock(),
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
    )
    invocation = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "import time; time.sleep(60)"),
        process_record_path=harness.record_path,
        process_record_root=harness.root,
    )
    with pytest.raises(StorageError):
        runner.run(invocation)
    assert len(reaped) == 1  # the unrecordable child was killed, not leaked
    assert reaped[0].poll() is not None  # and reaped
    harness.assert_untouched()
    harness.assert_no_leaf_anywhere()


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_runner_unlink_record_fails_closed_when_root_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Root swapped while the child runs (injected around the stream pump):
    the post-run record unlink fails closed; the record written through the
    pinned fd is preserved in the renamed-aside original owner and the
    replacement's planted leaf bytes are untouched."""
    harness = _RootSwapHarness(tmp_path, kind)
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()
    runner = SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=_clock(),
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
    )
    original_pump = runner._pump

    def swapping_pump(
        proc: subprocess.Popen[bytes], timeout_seconds: int
    ) -> tuple[bool, int | None, bytes, int]:
        harness.swap(plant_replacement_leaf=True)
        return original_pump(proc, timeout_seconds)

    monkeypatch.setattr(runner, "_pump", swapping_pump)
    invocation = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "pass"),
        process_record_path=harness.record_path,
        process_record_root=harness.root,
    )
    with pytest.raises(StorageError):
        runner.run(invocation)
    harness.assert_untouched()
    preserved = json.loads(
        (harness.original_owner / _LEAF).read_text(encoding="utf-8")
    )
    assert preserved["pid"] > 0  # written through the pinned fd pre-swap


def _swapping_owner_class(
    harness: _RootSwapHarness, *, swap_after_read: bool
) -> type[_PinnedOwnerDir]:
    """A ``_PinnedOwnerDir`` whose ``read_record`` injects the root swap —
    before the read (the read boundary) or after it (the pre-signal
    boundary), always after initial validation/pinning."""

    class _SwappingOwner(_PinnedOwnerDir):
        def read_record(self) -> dict[str, object] | None:
            if not swap_after_read:
                harness.swap(plant_replacement_leaf=True)
                return super().read_record()
            doc = super().read_record()
            harness.swap(plant_replacement_leaf=True)
            return doc

    return _SwappingOwner


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_terminator_read_fails_closed_when_root_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    harness = _RootSwapHarness(tmp_path, kind)
    atomic_write_json(
        harness.record_path,
        {"pid": os.getpid(), "command": sys.executable},
        allowed_root=harness.root,
    )
    kills: list[tuple[int, object]] = []
    monkeypatch.setattr(
        subprocess_runner_module,
        "kill_process_group",
        lambda pid, sig: kills.append((pid, sig)),
    )
    monkeypatch.setattr(
        subprocess_runner_module,
        "_PinnedOwnerDir",
        _swapping_owner_class(harness, swap_after_read=False),
    )
    with pytest.raises(StorageError):
        terminate_recorded_process(harness.record_path, allowed_root=harness.root)
    assert kills == []  # invalid authority sends no signal
    harness.assert_untouched()
    assert (harness.original_owner / _LEAF).exists()  # record preserved


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_terminator_presignal_fails_closed_when_root_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Root swapped after the record was read but immediately before the
    TERM decision: a live, argv-matching recorded process is NOT signalled."""
    harness = _RootSwapHarness(tmp_path, kind)
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record_bytes_path = harness.record_path
        atomic_write_json(
            record_bytes_path,
            {"pid": decoy.pid, "command": sys.executable},
            allowed_root=harness.root,
        )
        preserved_bytes = record_bytes_path.read_bytes()
        kills: list[tuple[int, object]] = []
        monkeypatch.setattr(
            subprocess_runner_module,
            "kill_process_group",
            lambda pid, sig: kills.append((pid, sig)),
        )
        monkeypatch.setattr(
            subprocess_runner_module,
            "_PinnedOwnerDir",
            _swapping_owner_class(harness, swap_after_read=True),
        )
        with pytest.raises(StorageError):
            terminate_recorded_process(
                harness.record_path, allowed_root=harness.root
            )
        assert kills == []  # invalid authority sends no signal
        assert decoy.poll() is None  # the recorded live process was spared
        harness.assert_untouched()
        assert (harness.original_owner / _LEAF).read_bytes() == preserved_bytes
    finally:
        decoy.kill()
        decoy.wait()


# =============================================================================
# 6. The validation-return -> root-open authority gap: root renamed aside and
#    replaced by an ordinary directory (same owner name) from *inside*
#    ``validate_process_record_location`` itself, before it returns to
#    ``_PinnedOwnerDir.__init__``. The root must already be pinned by fd
#    before validation runs, so the swap is caught by the immediate
#    post-validation re-prove and the replacement identity is never adopted.
# =============================================================================


def test_runner_rejects_root_swapped_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _RootSwapHarness(tmp_path, "directory")
    runs_dir = tmp_path / "agent-runs"
    runs_dir.mkdir()

    original_validate = subprocess_runner_module.validate_process_record_location

    def swapping_validate(root: object, path: object, *, ctx: str) -> Path:
        owner_dir = original_validate(root, path, ctx=ctx)
        harness.swap()
        return owner_dir

    monkeypatch.setattr(
        subprocess_runner_module,
        "validate_process_record_location",
        swapping_validate,
    )

    def failing_popen(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "Popen must not be called when the root is swapped during validation"
        )

    monkeypatch.setattr(
        "dagvane.adapters.agents.subprocess_runner.subprocess.Popen", failing_popen
    )

    runner = SubprocessAgentRunner(
        runs_dir=runs_dir,
        clock=_clock(),
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
    )
    invocation = AgentInvocation(
        runtime="command",
        prompt="hello",
        cwd=tmp_path,
        command_template=(sys.executable, "-c", "pass"),
        process_record_path=harness.record_path,
        process_record_root=harness.root,
    )
    with pytest.raises(StorageError):
        runner.run(invocation)
    harness.assert_untouched()
    harness.assert_no_leaf_anywhere()
    assert list(runs_dir.iterdir()) == []  # no run directory was created


def test_terminator_rejects_root_swapped_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replacement owner holds a syntactically valid, live-pid forged
    record: if the swap were adopted, the terminator would read and act on
    it. It must instead fail closed before ever opening the owner fd."""
    harness = _RootSwapHarness(tmp_path, "directory")
    atomic_write_json(
        harness.record_path,
        {"pid": os.getpid(), "command": sys.executable},
        allowed_root=harness.root,
    )
    original_record_bytes = harness.record_path.read_bytes()
    forged_record = json.dumps(
        {"pid": os.getpid(), "command": sys.executable}
    ).encode()

    original_validate = subprocess_runner_module.validate_process_record_location

    def swapping_validate(root: object, path: object, *, ctx: str) -> Path:
        owner_dir = original_validate(root, path, ctx=ctx)
        harness.swap()
        assert harness.swapped_owner is not None
        (harness.swapped_owner / _LEAF).write_bytes(forged_record)
        return owner_dir

    monkeypatch.setattr(
        subprocess_runner_module,
        "validate_process_record_location",
        swapping_validate,
    )
    kills: list[tuple[int, object]] = []
    monkeypatch.setattr(
        subprocess_runner_module,
        "kill_process_group",
        lambda pid, sig: kills.append((pid, sig)),
    )
    with pytest.raises(StorageError):
        terminate_recorded_process(harness.record_path, allowed_root=harness.root)
    assert kills == []  # invalid authority sends no signal
    harness.assert_untouched()
    assert harness.swapped_owner is not None
    assert (harness.swapped_owner / _LEAF).read_bytes() == forged_record
    assert (harness.original_owner / _LEAF).read_bytes() == original_record_bytes


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(), reason="needs procfs fd listing"
)
def test_repeated_pin_refusals_do_not_leak_fds(tmp_path: Path) -> None:
    """Both refusal shapes that open fds before failing — a missing owner
    (contained False) and an owner entry that is a regular file (the root fd
    is already pinned when the owner open fails) — leak nothing."""
    root = tmp_path / "records"
    root.mkdir()
    missing = root / "missing-owner" / _LEAF
    regular_owner = root / "reg-owner"
    regular_owner.write_bytes(b"not a directory")
    baseline = len(os.listdir("/proc/self/fd"))
    for _ in range(100):
        assert terminate_recorded_process(missing, allowed_root=root) is False
        with pytest.raises(StorageError):
            terminate_recorded_process(regular_owner / _LEAF, allowed_root=root)
    assert len(os.listdir("/proc/self/fd")) == baseline
