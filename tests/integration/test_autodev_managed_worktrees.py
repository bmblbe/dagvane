"""R1-A managed candidate, verification, and review worktree integration.

These tests bind application call sites to live manager handles, prove unowned
collisions are preserved, exercise cleanup recovery, and cover the two-phase
candidate port without any raw-path fallback.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest

from dagvane.adapters.localexec import GitOps
from dagvane.adapters.localexec import run_shell as actual_run_shell
from dagvane.adapters.worktrees import (
    ManagedWorktreeHandle,
    ManagedWorktrees,
    WorktreePurpose,
    WorktreeSpec,
)
from dagvane.application.autodev import (
    CandidateWorktreeLeaseV2,
    CandidateWorktreeLifecycleV2,
    GoalRunner,
    ManagedWorktreeLifecycle,
    RunState,
    _CandidateWorktreeUse,
)
from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    approve,
)
from dagvane.cli_workspace import Composition
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.workspace.config import render_toml


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _composition(tmp_path: Path) -> tuple[Composition, GoalRecord]:
    root = tmp_path / "project"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    (root / ".gitignore").write_text(".dagvane/\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)
    state_dir = root / ".dagvane"
    state_dir.mkdir()
    config = {
        "router": {"local_enabled": False},
        "goal": {
            "agent_timeout_seconds": 30,
            "implement_resource": "writer",
            "review_resource": "reviewer",
            "review_policy": "always",
        },
        "resources": {
            "writer": {
                "kind": "external_agent",
                "runtime": "command",
                "tier": "STANDARD",
                "command": ["unused"],
            },
            "reviewer": {
                "kind": "external_agent",
                "runtime": "command",
                "tier": "STRONG",
                "command": ["unused"],
            },
        },
    }
    (state_dir / "config.toml").write_text(
        render_toml(config) + "\n", encoding="utf-8"
    )
    comp = Composition(root)
    base = GitOps.head_sha(root)
    contract = GoalContract(
        name="goal-a",
        base_sha=base,
        objective="exercise managed lifecycle glue",
        must_have=["managed worktrees"],
        non_goals=[],
        checks=[AcceptanceCheck(check_id="ok", description="ok", command="true")],
        verify_commands=["true"],
        limits=GoalLimits(
            max_wall_seconds=3600,
            max_agent_calls=10,
            max_attempts=3,
            max_consecutive_failures=2,
        ),
    )
    now = comp.clock.now_iso()
    record = GoalRecord(
        contract=contract,
        status=GoalStatus.PREPARED,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
        baseline={"status": "completed", "base_sha": base},
    )
    approve(record)
    comp.goals.save("goal-a", record)
    return comp, record


class _ReviewRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, invocation: AgentInvocation) -> AgentExecution:
        self.calls += 1
        return AgentExecution(
            runtime=invocation.runtime,
            model=invocation.model,
            reasoning=invocation.reasoning,
            cwd=str(invocation.cwd),
            started_ts="2026-08-16T00:00:00.000Z",
            finished_ts="2026-08-16T00:00:00.001Z",
            duration_ms=1,
            exit_code=0,
            timed_out=False,
            output_text='{"findings": []}',
            prompt_path="prompt",
            output_path="output",
            log_path="log",
        )


class _ImmutableOnlyLifecycle:
    """Delegating lifecycle that intentionally lacks candidate V2 methods."""

    def __init__(self, manager: ManagedWorktrees) -> None:
        self.manager = manager

    def target_path(self, spec: WorktreeSpec) -> Path:
        return self.manager.target_path(spec)

    def create(self, spec: WorktreeSpec) -> ManagedWorktreeHandle:
        return self.manager.create(spec)

    def remove(self, handle: ManagedWorktreeHandle) -> None:
        self.manager.remove(handle)


class _MismatchedImmutableLifecycle:
    """Returns a valid handle for the wrong managed generation/path."""

    def __init__(self, manager: ManagedWorktrees, wrong_spec: WorktreeSpec) -> None:
        self.manager = manager
        self.wrong_spec = wrong_spec

    def target_path(self, spec: WorktreeSpec) -> Path:
        return self.manager.target_path(spec)

    def create(self, spec: WorktreeSpec) -> ManagedWorktreeHandle:
        return self.manager.create(self.wrong_spec)

    def remove(self, handle: ManagedWorktreeHandle) -> None:
        raise AssertionError((handle, "mismatched handle must never authorize remove"))


def _state(comp: Composition, record: GoalRecord) -> RunState:
    return RunState(
        run_id="run-1",
        goal_name=record.contract.name,
        base_sha=record.contract.base_sha,
        status="running",
        started_ts=comp.clock.now_iso(),
        implement_resource_id="writer",
    )


def _runner(
    comp: Composition,
    *,
    manager: ManagedWorktreeLifecycle,
    review_runner: _ReviewRunner | None = None,
    candidate: CandidateWorktreeLifecycleV2 | None = None,
) -> GoalRunner:
    return GoalRunner(
        workspace=comp.workspace,
        config=comp.config,
        store=comp.goals,
        catalog=comp.catalog,
        runner=review_runner if review_runner is not None else _ReviewRunner(),
        clock=comp.clock,
        monotonic=comp.monotonic,
        ids=comp.ids,
        progress=lambda _line: None,
        worktrees=manager,
        candidate_worktrees=candidate,
    )


def _manager(comp: Composition) -> ManagedWorktrees:
    return ManagedWorktrees(
        repo_root=comp.workspace.root,
        worktrees_root=comp.workspace.worktrees_dir,
    )


def _owner_doc(path: Path) -> dict[str, object]:
    latest, _valid = ManagedWorktrees._latest_record_document(  # noqa: SLF001
        path.read_bytes(), "test owner record"
    )
    loaded: dict[str, object] = json.loads(latest)
    return loaded


def _sentinel_target(
    manager: ManagedWorktrees, spec: WorktreeSpec
) -> tuple[Path, Path]:
    target = manager.target_path(spec)
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("preserve me\n", encoding="utf-8")
    return target, sentinel


def test_missing_candidate_capability_fails_before_target_or_writer_effect(
    tmp_path: Path,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=_ImmutableOnlyLifecycle(manager))
    state = _state(comp, record)

    with pytest.raises(SpecError, match="managed lifecycle V2"):
        runner._open_candidate_worktree(record, state)  # noqa: SLF001

    spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    assert not manager.target_path(spec).exists()
    assert not comp.workspace.worktrees_dir.joinpath(".owners").exists()


def test_unowned_candidate_verify_and_review_targets_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    review_runner = _ReviewRunner()
    runner = _runner(
        comp,
        manager=manager,
        review_runner=review_runner,
        candidate=manager,
    )
    state = _state(comp, record)

    candidate_spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    candidate_target, candidate_sentinel = _sentinel_target(manager, candidate_spec)
    with pytest.raises(StorageError, match="no owner record"):
        runner._open_candidate_worktree(record, state)  # noqa: SLF001
    assert candidate_sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert candidate_target.is_dir()

    verify_spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.VERIFY,
        sha=record.contract.base_sha,
    )
    verify_target, verify_sentinel = _sentinel_target(manager, verify_spec)

    def shell_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("verification shell ran before managed admission")

    monkeypatch.setattr(
        "dagvane.application.autodev.run_shell", shell_must_not_run
    )
    with pytest.raises(StorageError, match="no owner record"):
        runner._immutable_verification(  # noqa: SLF001
            record, state, record.contract.base_sha
        )
    assert verify_sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert verify_target.is_dir()

    review_spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.REVIEW,
        sha=record.contract.base_sha,
    )
    review_target, review_sentinel = _sentinel_target(manager, review_spec)
    with pytest.raises(StorageError, match="no owner record"):
        runner._review_candidate(  # noqa: SLF001
            record, state, record.contract.base_sha
        )
    assert review_sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert review_target.is_dir()
    assert review_runner.calls == 0

    owners = comp.workspace.worktrees_dir / ".owners"
    assert not any(path.suffix == ".json" for path in owners.iterdir())


def test_normal_candidate_verify_review_use_managed_purposes(
    tmp_path: Path,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    review_runner = _ReviewRunner()
    runner = _runner(
        comp,
        manager=manager,
        review_runner=review_runner,
        candidate=manager,
    )
    state = _state(comp, record)
    main_head = GitOps.head_sha(comp.workspace.root)

    use = runner._open_candidate_worktree(record, state)  # noqa: SLF001
    assert use is not None
    candidate_path = use.path
    assert use.sha == record.contract.base_sha
    assert GitOps.head_sha(candidate_path) == record.contract.base_sha
    runner._close_candidate_worktree()  # noqa: SLF001

    ok, failures = runner._immutable_verification(  # noqa: SLF001
        record, state, record.contract.base_sha
    )
    assert ok and failures == []
    kind, blocking, reason = runner._review_candidate(  # noqa: SLF001
        record, state, record.contract.base_sha
    )
    assert (kind, blocking, reason) == ("clean", [], "")
    assert review_runner.calls == 1

    assert candidate_path.is_dir()
    candidate_owner = (
        comp.workspace.worktrees_dir / ".owners" / "goal-a-run-1.json"
    )
    owner_doc = _owner_doc(candidate_owner)
    assert owner_doc["purpose"] == "candidate"
    assert owner_doc["sha"] == record.contract.base_sha
    for purpose in (WorktreePurpose.VERIFY, WorktreePurpose.REVIEW):
        spec = WorktreeSpec(
            goal_name="goal-a",
            run_id="run-1",
            purpose=purpose,
            sha=record.contract.base_sha,
        )
        assert not manager.target_path(spec).exists()
        owner_path = (
            comp.workspace.worktrees_dir / ".owners" / f"{spec.target_leaf}.json"
        )
        assert _owner_doc(owner_path)["state"] == "removed"
    assert GitOps.head_sha(comp.workspace.root) == main_head
    assert GitOps.is_clean(comp.workspace.root)
    assert not hasattr(GitOps, "fresh_worktree")
    assert not hasattr(GitOps, "worktree_add")
    assert not hasattr(GitOps, "worktree_remove")


def test_verify_and_review_reject_mismatched_managed_handle_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    review_runner = _ReviewRunner()

    def shell_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("shell ran through a mismatched managed handle")

    monkeypatch.setattr(
        "dagvane.application.autodev.run_shell", shell_must_not_run
    )
    verify_wrong = WorktreeSpec(
        goal_name="foreign-verify",
        run_id="foreign-run",
        purpose=WorktreePurpose.VERIFY,
        sha=record.contract.base_sha,
    )
    verify_lifecycle = _MismatchedImmutableLifecycle(manager, verify_wrong)
    verify_runner = _runner(comp, manager=verify_lifecycle)
    with pytest.raises(StorageError, match="returned path"):
        verify_runner._immutable_verification(  # noqa: SLF001
            record, state, record.contract.base_sha
        )

    review_wrong = WorktreeSpec(
        goal_name="foreign-review",
        run_id="foreign-run",
        purpose=WorktreePurpose.REVIEW,
        sha=record.contract.base_sha,
    )
    review_lifecycle = _MismatchedImmutableLifecycle(manager, review_wrong)
    review_goal_runner = _runner(
        comp, manager=review_lifecycle, review_runner=review_runner
    )
    with pytest.raises(StorageError, match="returned path"):
        review_goal_runner._review_candidate(  # noqa: SLF001
            record, state, record.contract.base_sha
        )

    assert review_runner.calls == 0
    for spec in (verify_wrong, review_wrong):
        assert manager.target_path(spec).is_dir()
        owner_path = (
            comp.workspace.worktrees_dir / ".owners" / f"{spec.target_leaf}.json"
        )
        assert _owner_doc(owner_path)["state"] == "ready"


def test_verify_and_review_reject_correct_path_at_wrong_sha_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    review_runner = _ReviewRunner()
    _git(
        ["commit", "--allow-empty", "-q", "-m", "wrong immutable SHA"],
        comp.workspace.root,
    )
    wrong_sha = GitOps.head_sha(comp.workspace.root)

    def shell_must_not_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("shell ran before exact immutable SHA assertion")

    monkeypatch.setattr(
        "dagvane.application.autodev.run_shell", shell_must_not_run
    )
    wrong_verify = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.VERIFY,
        sha=wrong_sha,
    )
    verify_lifecycle = _MismatchedImmutableLifecycle(manager, wrong_verify)
    with pytest.raises(StorageError, match="expected exact SHA"):
        _runner(comp, manager=verify_lifecycle)._immutable_verification(  # noqa: SLF001
            record, state, record.contract.base_sha
        )

    wrong_review = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.REVIEW,
        sha=wrong_sha,
    )
    review_lifecycle = _MismatchedImmutableLifecycle(manager, wrong_review)
    with pytest.raises(StorageError, match="expected exact SHA"):
        _runner(
            comp,
            manager=review_lifecycle,
            review_runner=review_runner,
        )._review_candidate(  # noqa: SLF001
            record, state, record.contract.base_sha
        )
    assert review_runner.calls == 0


def test_candidate_rejects_bound_sha_head_mismatch_before_state_or_effect(
    tmp_path: Path,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    candidate_path = comp.workspace.worktrees_dir / "goal-a-run-1"
    _git(
        [
            "worktree",
            "add",
            "--detach",
            "--",
            str(candidate_path),
            record.contract.base_sha,
        ],
        comp.workspace.root,
    )
    claimed_sha = "1" * 40
    lease = _ProtocolLease(candidate_path, claimed_sha)
    lifecycle = _RecoveryLifecycle(lease)
    runner = _runner(
        comp,
        manager=manager,
        candidate=cast(CandidateWorktreeLifecycleV2, lifecycle),
    )

    with pytest.raises(StorageError, match="authoritative binding"):
        runner._open_candidate_worktree(record, state)  # noqa: SLF001

    assert lease.closed
    assert state.candidate_sha is None
    assert state.worktree is None
    assert not comp.goals.run_state_path("goal-a").exists()


def test_absent_manager_refuses_run_state_selected_candidate_sha(
    tmp_path: Path,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=manager, candidate=manager)
    state = _state(comp, record)
    _git(
        ["commit", "--allow-empty", "-q", "-m", "reachable but unowned"],
        comp.workspace.root,
    )
    asserted_sha = GitOps.head_sha(comp.workspace.root)
    expected_path = comp.workspace.worktrees_dir / "goal-a-run-1"
    state.candidate_sha = asserted_sha
    state.worktree = str(expected_path)

    with pytest.raises(StorageError, match="state asserts a prior candidate"):
        runner._open_candidate_worktree(record, state)  # noqa: SLF001

    assert not expected_path.exists()
    assert not (
        comp.workspace.worktrees_dir / ".owners" / "goal-a-run-1.json"
    ).exists()


def test_candidate_exception_releases_lease_and_retains_owned_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=manager, candidate=manager)
    state = _state(comp, record)

    def explode(
        _record: GoalRecord,
        _state: RunState,
        _use: _CandidateWorktreeUse,
    ) -> GoalStatus:
        raise RuntimeError("injected candidate body failure")

    monkeypatch.setattr(runner, "_loop_with_candidate", explode)
    with pytest.raises(RuntimeError, match="candidate body failure"):
        runner._loop(record, state)  # noqa: SLF001

    target = comp.workspace.worktrees_dir / "goal-a-run-1"
    owner_path = comp.workspace.worktrees_dir / ".owners" / "goal-a-run-1.json"
    owner_doc = _owner_doc(owner_path)
    assert target.is_dir()
    assert owner_doc["state"] == "ready"
    assert owner_doc["purpose"] == "candidate"
    # A new holder can lock the same lifecycle file, proving the exception's
    # outer finally released the live lease without discarding owner evidence.
    with owner_path.with_suffix(".lock").open("rb") as lifecycle_file:
        fcntl.flock(lifecycle_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lifecycle_file.fileno(), fcntl.LOCK_UN)


def test_verify_cleanup_failure_retains_managed_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=manager)
    state = _state(comp, record)
    original_remove = GitOps.worktree_remove_forced_fd

    def fail_remove(_repo: Path, _target_fd: int) -> None:
        raise SpecError("injected managed cleanup failure")

    monkeypatch.setattr(
        GitOps, "worktree_remove_forced_fd", staticmethod(fail_remove)
    )
    with pytest.raises(SpecError, match="injected managed cleanup failure"):
        runner._immutable_verification(  # noqa: SLF001
            record, state, record.contract.base_sha
        )

    owner_path = (
        comp.workspace.worktrees_dir / ".owners" / "goal-a-run-1-verify.json"
    )
    owner_doc = _owner_doc(owner_path)
    assert owner_doc["state"] == "removing"
    assert owner_doc["location"] == "quarantine"
    quarantine = comp.workspace.worktrees_dir / (
        f".goal-a-run-1-verify.{owner_doc['nonce']}.remove"
    )
    assert quarantine.is_dir()
    assert state.verification is None

    monkeypatch.setattr(
        GitOps,
        "worktree_remove_forced_fd",
        staticmethod(original_remove),
    )
    ok, failures = runner._immutable_verification(  # noqa: SLF001
        record, state, record.contract.base_sha
    )
    assert ok and failures == []
    assert _owner_doc(owner_path)["state"] == "removed"
    assert not quarantine.exists()


def test_real_git_add_uses_pinned_inode_not_public_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    target = manager.target_path(spec)
    displaced = comp.workspace.worktrees_dir / "displaced-owned-add"
    real_run = subprocess.run
    injected = False

    def replace_before_git(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal injected
        if not injected and argv[3:6] == ["worktree", "add", "--detach"]:
            injected = True
            target.rename(displaced)
            target.mkdir()
            (target / "sentinel").write_text("foreign\n", encoding="utf-8")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", replace_before_git)
    with pytest.raises(StorageError, match="owned directory"):
        manager.create_candidate(spec)

    assert (target / "sentinel").read_text(encoding="utf-8") == "foreign\n"
    listing = _git(["worktree", "list", "--porcelain"], comp.workspace.root)
    assert f"worktree {target}" not in listing
    assert f"worktree {displaced}" in listing


def test_real_git_move_uses_pinned_source_not_public_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    handle = manager.create_candidate(spec)
    target = manager.target_path(spec)
    displaced = comp.workspace.worktrees_dir / "displaced-owned-move"
    real_run = subprocess.run
    injected = False

    def replace_before_git(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal injected
        if not injected and argv[3:5] == ["worktree", "move"]:
            injected = True
            target.rename(displaced)
            target.mkdir()
            (target / "sentinel").write_text("foreign\n", encoding="utf-8")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", replace_before_git)
    with pytest.raises(SpecError, match="move via pinned fds"):
        manager.remove(handle)

    assert (target / "sentinel").read_text(encoding="utf-8") == "foreign\n"
    assert sorted(path.name for path in target.iterdir()) == ["sentinel"]
    assert (displaced / ".git").is_file()
    listing = _git(["worktree", "list", "--porcelain"], comp.workspace.root)
    assert "displaced-owned-move" not in listing
    assert ".remove" not in listing


def test_real_git_add_keeps_pinned_repo_authority_on_root_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    repo = comp.workspace.root
    target = manager.target_path(spec)
    owned_repo = tmp_path / "owned-project"
    owned_target = owned_repo / target.relative_to(repo)
    real_run = subprocess.run
    injected = False

    def replace_repo_before_git(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal injected
        if not injected and argv[3:6] == ["worktree", "add", "--detach"]:
            injected = True
            repo.rename(owned_repo)
            shutil.copytree(owned_repo, repo, symlinks=True)
            (target / "foreign-sentinel").write_text(
                "keep\n", encoding="utf-8"
            )
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", replace_repo_before_git)
    with pytest.raises(StorageError, match="pinned directory"):
        manager.create_candidate(spec)

    assert (target / "foreign-sentinel").read_text(encoding="utf-8") == "keep\n"
    assert not (target / ".git").exists()
    foreign_listing = _git(["worktree", "list", "--porcelain"], repo)
    assert f"worktree {target}" not in foreign_listing
    owned_listing = _git(
        [
            f"--git-dir={owned_repo / '.git'}",
            f"--work-tree={owned_repo}",
            "worktree",
            "list",
            "--porcelain",
        ],
        owned_repo,
    )
    assert f"worktree {owned_target}" in owned_listing


def test_real_git_move_keeps_pinned_common_authority_on_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    spec = WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    handle = manager.create_candidate(spec)
    repo = comp.workspace.root
    target = manager.target_path(spec)
    quarantine = comp.workspace.worktrees_dir / (
        f".{spec.target_leaf}.{handle.generation}.remove"
    )
    common = repo / ".git"
    owned_common = repo / ".git-owned"
    real_run = subprocess.run
    injected = False
    foreign_registry: dict[str, bytes] = {}

    def replace_common_before_git(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        nonlocal injected, foreign_registry
        if not injected and argv[3:5] == ["worktree", "move"]:
            injected = True
            common.rename(owned_common)
            shutil.copytree(owned_common, common, symlinks=True)
            foreign_registry = {
                str(path.relative_to(common)): path.read_bytes()
                for path in (common / "worktrees").rglob("*")
                if path.is_file()
            }
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", replace_common_before_git)
    with pytest.raises(SpecError, match="move via pinned fds"):
        manager.remove(handle)

    assert not quarantine.exists()
    assert target.is_dir()
    assert foreign_registry == {
        str(path.relative_to(common)): path.read_bytes()
        for path in (common / "worktrees").rglob("*")
        if path.is_file()
    }
    foreign_listing = _git(
        [
            f"--git-dir={common}",
            f"--work-tree={repo}",
            "worktree",
            "list",
            "--porcelain",
        ],
        repo,
    )
    assert f"worktree {target}" in foreign_listing
    assert f"worktree {quarantine}" not in foreign_listing
    owned_listing = _git(
        [
            f"--git-dir={owned_common}",
            f"--work-tree={repo}",
            "worktree",
            "list",
            "--porcelain",
        ],
        repo,
    )
    assert f"worktree {target}" in owned_listing
    assert f"worktree {quarantine}" not in owned_listing


class _ProtocolLease:
    def __init__(self, path: Path, sha: str) -> None:
        self._path = path
        self._sha = sha
        self.closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def bound_sha(self) -> str:
        return self._sha

    @property
    def generation(self) -> str:
        return "0" * 32

    def close(self) -> None:
        self.closed = True

    @contextmanager
    def pinned_authority(self) -> Iterator[tuple[int, int, tuple[int, int]]]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        worktree_fd = os.open(self._path, flags)
        common = GitOps.common_dir(self._path)
        common_fd = os.open(common, flags)
        try:
            st = os.fstat(worktree_fd)
            yield worktree_fd, common_fd, (st.st_dev, st.st_ino)
        finally:
            os.close(common_fd)
            os.close(worktree_fd)


class _TwoPhaseLifecycle:
    def __init__(self, lease: _ProtocolLease) -> None:
        self.lease = lease
        self.events: list[tuple[str, str]] = []
        self._token = object()

    def acquire_existing(self, spec: WorktreeSpec) -> CandidateWorktreeLeaseV2 | None:
        return self.lease

    def create_candidate(self, spec: WorktreeSpec) -> CandidateWorktreeLeaseV2:
        raise AssertionError(spec)

    def prove_for_use(
        self,
        handle: CandidateWorktreeLeaseV2,
        *,
        expected_bound_sha: str,
    ) -> Path:
        assert handle is self.lease
        assert handle.bound_sha == expected_bound_sha
        self.events.append(("prove", expected_bound_sha))
        return handle.path

    def begin_sha_advance(
        self, handle: CandidateWorktreeLeaseV2, *, expected_old_sha: str
    ) -> object:
        assert handle is self.lease
        assert GitOps.head_sha(handle.path) == expected_old_sha
        assert not GitOps.is_clean(handle.path)
        self.events.append(("begin", expected_old_sha))
        return self._token

    def complete_sha_advance(
        self,
        handle: CandidateWorktreeLeaseV2,
        token: object,
        *,
        new_sha: str,
    ) -> None:
        assert handle is self.lease
        assert token is self._token
        assert GitOps.head_sha(handle.path) == new_sha
        assert GitOps.is_clean(handle.path)
        self.events.append(("complete", new_sha))
        self.lease._sha = new_sha  # noqa: SLF001 - fake manager checkpoint


class _RecoveryLifecycle:
    def __init__(self, lease: _ProtocolLease) -> None:
        self.lease = lease
        self.acquired = 0

    def acquire_existing(self, spec: WorktreeSpec) -> CandidateWorktreeLeaseV2 | None:
        self.acquired += 1
        return self.lease

    def create_candidate(self, spec: WorktreeSpec) -> CandidateWorktreeLeaseV2:
        raise AssertionError((spec, "cancel recovery must not create"))

    def prove_for_use(
        self,
        handle: CandidateWorktreeLeaseV2,
        *,
        expected_bound_sha: str,
    ) -> Path:
        assert handle is self.lease
        assert handle.bound_sha == expected_bound_sha
        return handle.path

    def begin_sha_advance(
        self, handle: CandidateWorktreeLeaseV2, *, expected_old_sha: str
    ) -> object:
        raise AssertionError((handle, expected_old_sha))

    def complete_sha_advance(
        self,
        handle: CandidateWorktreeLeaseV2,
        token: object,
        *,
        new_sha: str,
    ) -> None:
        raise AssertionError((handle, token, new_sha))


class _SwapLifecycle(_RecoveryLifecycle):
    def __init__(
        self,
        lease: _ProtocolLease,
        *,
        outside: Path,
        swap_on_proof: int,
    ) -> None:
        super().__init__(lease)
        self.outside = outside
        self.swap_on_proof = swap_on_proof
        self.proofs = 0

    def prove_for_use(
        self,
        handle: CandidateWorktreeLeaseV2,
        *,
        expected_bound_sha: str,
    ) -> Path:
        self.proofs += 1
        if self.proofs == self.swap_on_proof:
            backup = handle.path.with_name(handle.path.name + "-owned-backup")
            handle.path.rename(backup)
            handle.path.symlink_to(self.outside, target_is_directory=True)
        return super().prove_for_use(
            handle, expected_bound_sha=expected_bound_sha
        )


def test_candidate_commit_uses_two_phase_manager_checkpoint(
    tmp_path: Path,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    state.candidate_sha = record.contract.base_sha
    candidate_path = comp.workspace.worktrees_dir / "goal-a-run-1"
    _git(
        [
            "worktree",
            "add",
            "--detach",
            "--",
            str(candidate_path),
            record.contract.base_sha,
        ],
        comp.workspace.root,
    )
    lease = _ProtocolLease(candidate_path, record.contract.base_sha)
    lifecycle = _TwoPhaseLifecycle(lease)
    runner = _runner(
        comp,
        manager=manager,
        candidate=cast(CandidateWorktreeLifecycleV2, lifecycle),
    )
    use = _CandidateWorktreeUse(
        lifecycle=lifecycle,
        handle=lease,
        expected_path=candidate_path,
    )
    (candidate_path / "candidate.txt").write_text("candidate\n", encoding="utf-8")

    candidate, advanced = runner._commit_candidate(  # noqa: SLF001
        record, state, use, "candidate commit"
    )

    assert advanced
    assert candidate != record.contract.base_sha
    assert lifecycle.events == [
        ("prove", record.contract.base_sha),
        ("begin", record.contract.base_sha),
        ("prove", record.contract.base_sha),
        ("complete", candidate),
        ("prove", candidate),
    ]
    assert state.candidate_sha == candidate
    assert GitOps.head_sha(candidate_path) == candidate


def test_candidate_shell_swap_cannot_execute_in_foreign_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=manager)
    state = _state(comp, record)
    use = runner._open_candidate_worktree(record, state)  # noqa: SLF001
    assert use is not None
    target = use.handle.path
    outside = tmp_path / "foreign-shell-cwd"
    outside.mkdir()
    marker = outside / "escaped.txt"
    backup = target.with_name("owned-before-shell-swap")
    real_popen = subprocess.Popen
    injected = False

    def swap_before_spawn(*args: object, **kwargs: object) -> Any:
        nonlocal injected
        command = args[0] if args else kwargs.get("args")
        if (
            not injected
            and isinstance(command, str)
            and "escaped.txt" in command
        ):
            injected = True
            target.rename(backup)
            target.symlink_to(outside, target_is_directory=True)
        return cast(Any, real_popen)(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", swap_before_spawn)
    try:
        with pytest.raises(StorageError):
            runner._run_candidate_shell(  # noqa: SLF001
                "touch escaped.txt", use, timeout_seconds=5
            )
    finally:
        runner._close_candidate_worktree()  # noqa: SLF001

    assert injected
    assert not marker.exists()
    assert (backup / "escaped.txt").is_file()
    assert target.is_symlink()
    assert backup.is_dir()


def test_candidate_commit_swap_cannot_run_git_in_foreign_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    runner = _runner(comp, manager=manager)
    state = _state(comp, record)
    use = runner._open_candidate_worktree(record, state)  # noqa: SLF001
    assert use is not None
    target = use.handle.path
    (target / "candidate.txt").write_text("candidate\n", encoding="utf-8")

    outside = tmp_path / "foreign-repository"
    outside.mkdir()
    _git(["init", "-q"], outside)
    _git(["config", "user.name", "Foreign"], outside)
    _git(["config", "user.email", "foreign@example.test"], outside)
    (outside / "README.md").write_text("foreign\n", encoding="utf-8")
    _git(["add", "-A"], outside)
    _git(["commit", "-q", "-m", "foreign base"], outside)
    (outside / "escaped.txt").write_text("escaped\n", encoding="utf-8")
    foreign_before = GitOps.head_sha(outside)
    real_commit = GitOps.commit_all
    backup = target.with_name("owned-before-commit-swap")

    def swap_then_commit(worktree: Path, message: str) -> str | None:
        target.rename(backup)
        target.symlink_to(outside, target_is_directory=True)
        return real_commit(worktree, message)

    monkeypatch.setattr(GitOps, "commit_all", staticmethod(swap_then_commit))
    try:
        with pytest.raises(StorageError):
            runner._commit_candidate(record, state, use, "candidate commit")  # noqa: SLF001
    finally:
        runner._close_candidate_worktree()  # noqa: SLF001

    assert GitOps.head_sha(outside) == foreign_before
    assert (outside / "escaped.txt").exists()
    assert target.is_symlink()
    assert backup.is_dir()


@pytest.mark.parametrize("state_has_candidate_assertions", [False, True])
def test_cancel_resume_recovers_manager_sha_before_terminal_state(
    tmp_path: Path, state_has_candidate_assertions: bool
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    candidate_path = comp.workspace.worktrees_dir / "goal-a-run-1"
    _git(
        ["commit", "--allow-empty", "-q", "-m", "recovered candidate"],
        comp.workspace.root,
    )
    recovered_sha = GitOps.head_sha(comp.workspace.root)
    _git(
        [
            "worktree",
            "add",
            "--detach",
            "--",
            str(candidate_path),
            recovered_sha,
        ],
        comp.workspace.root,
    )
    lease = _ProtocolLease(candidate_path, recovered_sha)
    lifecycle = _RecoveryLifecycle(lease)
    runner = _runner(
        comp,
        manager=manager,
        candidate=cast(CandidateWorktreeLifecycleV2, lifecycle),
    )
    if state_has_candidate_assertions:
        state.candidate_sha = record.contract.base_sha
        state.worktree = str(candidate_path)
    record.status = GoalStatus.CANCELLED
    comp.goals.save("goal-a", record)
    runner._save_state(record, state)  # noqa: SLF001

    assert runner.resume("goal-a") is GoalStatus.CANCELLED

    reloaded = runner.load_state("goal-a")
    assert reloaded is not None
    assert reloaded.status == GoalStatus.CANCELLED.value
    assert reloaded.candidate_sha == recovered_sha
    assert lifecycle.acquired == 1
    assert lease.closed


def test_cancel_resume_abandons_preclaim_without_git_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    spec = WorktreeSpec(
        goal_name=state.goal_name,
        run_id=state.run_id,
        purpose=WorktreePurpose.CANDIDATE,
        sha=record.contract.base_sha,
    )
    original_finish = manager._finish_preclaim  # noqa: SLF001

    def stop_after_preclaim(_session: object) -> bool:
        raise StorageError("injected stop after candidate preclaim")

    monkeypatch.setattr(manager, "_finish_preclaim", stop_after_preclaim)
    with pytest.raises(StorageError, match="candidate preclaim"):
        manager.create_candidate(spec)
    monkeypatch.setattr(manager, "_finish_preclaim", original_finish)
    owner_path = (
        comp.workspace.worktrees_dir / ".owners" / "goal-a-run-1.json"
    )
    preclaim_nonce = _owner_doc(owner_path)["nonce"]

    def add_must_not_run(_repo: Path, _target_fd: int, _sha: str) -> None:
        raise AssertionError("cancelled acquisition must never call Git add")

    monkeypatch.setattr(
        GitOps, "worktree_add_detached_exact", staticmethod(add_must_not_run)
    )
    record.status = GoalStatus.CANCELLED
    comp.goals.save("goal-a", record)
    runner = _runner(comp, manager=manager, candidate=manager)
    runner._save_state(record, state)  # noqa: SLF001

    assert runner.resume("goal-a") is GoalStatus.CANCELLED
    owner = _owner_doc(owner_path)
    assert owner["state"] == "abandoned"
    assert owner["nonce"] == preclaim_nonce
    assert not manager.target_path(spec).exists()
    assert _git(["worktree", "list", "--porcelain"], comp.workspace.root).count(
        "worktree "
    ) == 1


@pytest.mark.parametrize("swap_on_proof", [2, 3])
def test_candidate_target_swap_fails_before_or_after_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_on_proof: int,
) -> None:
    comp, record = _composition(tmp_path)
    manager = _manager(comp)
    state = _state(comp, record)
    candidate_path = comp.workspace.worktrees_dir / "goal-a-run-1"
    _git(
        [
            "worktree",
            "add",
            "--detach",
            "--",
            str(candidate_path),
            record.contract.base_sha,
        ],
        comp.workspace.root,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")
    lease = _ProtocolLease(candidate_path, record.contract.base_sha)
    lifecycle = _SwapLifecycle(
        lease,
        outside=outside,
        swap_on_proof=swap_on_proof,
    )
    runner = _runner(
        comp,
        manager=manager,
        candidate=cast(CandidateWorktreeLifecycleV2, lifecycle),
    )
    use = runner._open_candidate_worktree(record, state)  # noqa: SLF001
    assert use is not None
    shell_calls = 0

    def counted_shell(*args: object, **kwargs: object) -> object:
        nonlocal shell_calls
        shell_calls += 1
        return actual_run_shell(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("dagvane.application.autodev.run_shell", counted_shell)
    try:
        with pytest.raises(StorageError):
            runner._run_checks(record, use)  # noqa: SLF001
    finally:
        runner._close_candidate_worktree()  # noqa: SLF001

    assert shell_calls == swap_on_proof - 2
    assert sentinel.read_text(encoding="utf-8") == "untouched\n"
