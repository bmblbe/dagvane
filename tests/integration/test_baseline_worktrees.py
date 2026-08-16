"""R1-A SEC-001 slice: managed baseline worktrees against a real Git repo.

The approved baseline runs at the exact approved base SHA in a managed
disposable worktree, becomes ``completed`` only after the managed cleanup
provably succeeded, converges after a genuine managed interruption, and
refuses an unowned entry at its deterministic target without a single
shell/Git invocation. No ``rmtree``, no ``prune``."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import dagvane.adapters.localexec as localexec
import dagvane.application.prepare as prepare_module
from dagvane.adapters.localexec import GitOps
from dagvane.adapters.localexec import run_shell as actual_run_shell
from dagvane.adapters.worktrees import ManagedWorktrees, WorktreePurpose, WorktreeSpec
from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    approve,
)
from dagvane.application.prepare import collect_baseline
from dagvane.cli_workspace import Composition
from dagvane.domain.models import SpecError, StorageError


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _owner_doc(path: Path) -> dict[str, object]:
    latest, _valid = ManagedWorktrees._latest_record_document(  # noqa: SLF001
        path.read_bytes(), "test owner record"
    )
    loaded: dict[str, object] = json.loads(latest)
    return loaded


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)


def _pending_goal(comp: Composition, name: str) -> GoalRecord:
    base = GitOps.head_sha(comp.workspace.root)
    contract = GoalContract(
        name=name,
        base_sha=base,
        objective="test objective",
        must_have=["the deliverable"],
        non_goals=["everything else"],
        checks=[
            AcceptanceCheck(
                check_id="probe", description="probe", command="test -f marker.txt"
            )
        ],
        verify_commands=["true"],
        limits=GoalLimits(
            max_wall_seconds=3600,
            max_agent_calls=40,
            max_attempts=4,
            max_consecutive_failures=3,
        ),
    )
    now = comp.clock.now_iso()
    record = GoalRecord(
        contract=contract,
        status=GoalStatus.PREPARED,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
        baseline={"status": "pending", "base_sha": base},
    )
    approve(record)
    comp.goals.save(name, record)
    return record


def _collect(comp: Composition, record: GoalRecord, name: str) -> None:
    collect_baseline(
        workspace=comp.workspace,
        config=comp.config,
        goals=comp.goals,
        record=record,
        expected_name=name,
        monotonic=comp.monotonic,
        progress=lambda _line: None,
    )


def _spec(record: GoalRecord) -> WorktreeSpec:
    return WorktreeSpec(
        goal_name=record.contract.name,
        purpose=WorktreePurpose.BASELINE,
        sha=record.contract.base_sha,
    )


def test_baseline_runs_managed_at_exact_sha_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")

    recorded: list[list[str]] = []
    orig_git, orig_git_bytes = localexec._git, localexec._git_bytes
    orig_remove_fd = GitOps.worktree_remove_forced_fd

    def spy_git(args: list[str], cwd: Path) -> str:
        recorded.append(list(args))
        return orig_git(args, cwd)

    def spy_git_bytes(args: list[str], cwd: Path) -> bytes:
        recorded.append(list(args))
        return orig_git_bytes(args, cwd)

    def no_rmtree(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("shutil.rmtree must never run in the managed protocol")

    def spy_remove_fd(repo: Path, target_fd: int) -> None:
        recorded.append(
            [
                "worktree",
                "remove",
                "--force",
                "--",
                f"/proc/self/fd/{target_fd}",
            ]
        )
        orig_remove_fd(repo, target_fd)

    monkeypatch.setattr(localexec, "_git", spy_git)
    monkeypatch.setattr(localexec, "_git_bytes", spy_git_bytes)
    monkeypatch.setattr(GitOps, "worktree_remove_forced_fd", spy_remove_fd)
    monkeypatch.setattr(shutil, "rmtree", no_rmtree)

    _collect(comp, record, "goal-a")

    reloaded = comp.goals.load("goal-a")
    assert reloaded.baseline["status"] == "completed"
    assert reloaded.baseline["base_sha"] == record.contract.base_sha
    checks = reloaded.baseline["checks"]
    assert isinstance(checks, dict) and checks["probe"]["ok"] is False
    assert GitOps.is_clean(root)
    target = comp.workspace.worktrees_dir / "goal-a-baseline"
    owners = comp.workspace.worktrees_dir / ".owners"
    assert not target.exists()
    assert sorted(path.name for path in owners.iterdir()) == [
        "goal-a-baseline.json",
        "goal-a-baseline.lock",
    ]
    # Exactly one removal, against a generation-private quarantine — never
    # the public target pathname — and never a prune.
    removes = [argv for argv in recorded if argv[:2] == ["worktree", "remove"]]
    assert len(removes) == 1
    assert removes[0][:4] == ["worktree", "remove", "--force", "--"]
    assert removes[0][4] != str(target)
    assert removes[0][4].startswith("/proc/self/fd/")
    assert not any("prune" in argv for argv in recorded)
    assert ["worktree", "add", "--detach", "--", str(target), record.contract.base_sha] in recorded


def test_completion_only_after_managed_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")

    def broken_remove(repo: Path, target_fd: int) -> None:
        raise SpecError("simulated cleanup failure")

    monkeypatch.setattr(GitOps, "worktree_remove_forced_fd", staticmethod(broken_remove))
    with pytest.raises(SpecError, match="simulated cleanup failure"):
        _collect(comp, record, "goal-a")

    # Cleanup failed, so the baseline is NOT completed — pending and retryable.
    reloaded = comp.goals.load("goal-a")
    assert reloaded.baseline["status"] == "pending"
    target = comp.workspace.worktrees_dir / "goal-a-baseline"
    assert not target.exists()
    owner_doc = comp.workspace.worktrees_dir / ".owners" / "goal-a-baseline.json"
    removing = _owner_doc(owner_doc)
    assert removing["state"] == "removing"
    assert removing["location"] == "quarantine"
    quarantine = comp.workspace.worktrees_dir / (
        f".goal-a-baseline.{removing['nonce']}.remove"
    )
    assert quarantine.is_dir()
    monkeypatch.undo()

    _collect(comp, reloaded, "goal-a")
    final = comp.goals.load("goal-a")
    assert final.baseline["status"] == "completed"
    assert not target.exists()
    assert GitOps.is_clean(root)


def test_genuine_managed_interruption_converges_on_retry(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")
    # A genuine interruption: the worktree was created through the managed
    # protocol (ready record + registration) but collection never finished.
    manager = ManagedWorktrees(
        repo_root=comp.workspace.root, worktrees_root=comp.workspace.worktrees_dir
    )
    stale_handle = manager.create(_spec(record))
    stale = stale_handle.path
    assert stale.is_dir()
    stale_handle.close()  # simulate process death; release, never delete

    _collect(comp, record, "goal-a")
    reloaded = comp.goals.load("goal-a")
    assert reloaded.baseline["status"] == "completed"
    assert not stale.exists()
    owner_path = comp.workspace.worktrees_dir / ".owners" / "goal-a-baseline.json"
    assert _owner_doc(owner_path)["state"] == "removed"
    assert GitOps.is_clean(root)


def test_two_concurrent_baseline_lifecycles_admit_only_one_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")
    started = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original_run_shell = actual_run_shell

    def blocking_run_shell(*args: Any, **kwargs: Any) -> Any:
        if not started.is_set():
            started.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release first baseline")
        return original_run_shell(*args, **kwargs)

    def first_collector() -> None:
        try:
            _collect(comp, record, "goal-a")
        except BaseException as exc:  # surfaced in the assertion below
            errors.append(exc)

    monkeypatch.setattr(prepare_module, "run_shell", blocking_run_shell)
    thread = threading.Thread(target=first_collector)
    thread.start()
    assert started.wait(timeout=5)

    with pytest.raises(StorageError, match="lifecycle is active"):
        _collect(comp, record, "goal-a")

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert comp.goals.load("goal-a").baseline["status"] == "completed"


def test_unowned_expected_target_refused_preserved_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")
    target = comp.workspace.worktrees_dir / "goal-a-baseline"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")

    def no_shell(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("no shell/Git invocation may happen for an unowned target")

    monkeypatch.setattr(localexec, "_git", no_shell)
    monkeypatch.setattr(localexec, "_git_bytes", no_shell)
    monkeypatch.setattr(prepare_module, "run_shell", no_shell)

    with pytest.raises(StorageError, match="no owner record"):
        _collect(comp, record, "goal-a")

    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    reloaded = comp.goals.load("goal-a")
    assert reloaded.baseline["status"] == "pending"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only workflow")
def test_canonical_repo_untouched_by_baseline(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    head_before = GitOps.head_sha(root)
    comp = Composition(root)
    record = _pending_goal(comp, "goal-a")
    _collect(comp, record, "goal-a")
    assert GitOps.head_sha(root) == head_before
    assert GitOps.is_clean(root)
    # Only the main worktree remains registered.
    listing = localexec._git(["worktree", "list", "--porcelain"], root)
    assert listing.count("worktree ") == 1
