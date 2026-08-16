"""Adversarial security tests for Goal sidecar filesystem effects (R1-A).

Covers the invariants the R1-A recovery restored: the per-goal lease, the
durable run-state, and the in-flight agent-process record are each reached
only through a fixed, guarded leaf; a pre-existing symlink at any of those
leaves must fail closed with no filesystem/Git/agent effect; ``start``/
``resume`` revalidate Goal identity a second time once the lease is actually
held; ``collect_baseline`` and ``prepare_goal`` validate their identity
argument before any Git/shell/conversation/router/runner effect.

Offline only: no real model/network. External agents are either never
reachable (asserted via spies) or a trivial local ``command`` runtime.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from dagvane.adapters.agents.subprocess_runner import (
    SubprocessAgentRunner,
    terminate_recorded_process,
)
from dagvane.adapters.localexec import GitOps
from dagvane.application.autodev import GoalRunner
from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    GoalStore,
)
from dagvane.application.prepare import collect_baseline, prepare_goal
from dagvane.application.resources import ResourceCatalog
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentExecution, AgentInvocation, ExternalAgentRunner
from dagvane.ports.runtime import FixedClock, SequentialIds, SteppingMonotonic
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.lease import GoalLease
from dagvane.workspace.paths import Workspace, atomic_write_json

# -- shared fixtures / builders -----------------------------------------------


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "t@example.test"], root)
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)
    workspace = Workspace(root)
    workspace.ensure()
    return workspace


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


def _contract(name: str, base_sha: str) -> GoalContract:
    return GoalContract(
        name=name,
        base_sha=base_sha,
        objective="objective",
        must_have=["x"],
        non_goals=[],
        checks=[AcceptanceCheck(check_id="c1", description="d", command="true")],
        verify_commands=["true"],
        limits=GoalLimits(
            max_wall_seconds=3600,
            max_agent_calls=10,
            max_attempts=3,
            max_consecutive_failures=2,
        ),
    )


def _record(name: str, base_sha: str, clock: FixedClock, status: GoalStatus) -> GoalRecord:
    now = clock.now_iso()
    return GoalRecord(
        contract=_contract(name, base_sha),
        status=status,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
    )


class _NeverCalledRunner:
    """An ExternalAgentRunner double that fails the test if ever invoked."""

    def run(self, invocation: AgentInvocation) -> AgentExecution:  # pragma: no cover
        raise AssertionError(f"external agent unexpectedly invoked: {invocation}")


def _goal_runner(
    workspace: Workspace,
    store: GoalStore,
    clock: FixedClock,
    *,
    runner: ExternalAgentRunner | None = None,
    progress: list[str] | None = None,
) -> GoalRunner:
    log = progress if progress is not None else []
    return GoalRunner(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        store=store,
        catalog=ResourceCatalog({}),
        runner=runner if runner is not None else _NeverCalledRunner(),
        clock=clock,
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
        progress=log.append,
    )


def _git_porcelain_status(root: Path) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _assert_no_new_git_state(
    workspace: Workspace, head_before: str, status_before: str
) -> None:
    assert GitOps.head_sha(workspace.root) == head_before
    assert _git_porcelain_status(workspace.root) == status_before
    assert not workspace.worktrees_dir.exists() or list(workspace.worktrees_dir.iterdir()) == []


def _sentinels(tmp_path: Path, workspace: Workspace) -> dict[str, Path]:
    """Bytes planted outside, at root/parent, and at a same-prefix sibling of
    the goals dir — none of these must ever be touched by a negative path."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside")
    root_secret = workspace.root / "root-secret.txt"
    root_secret.write_bytes(b"root")
    parent_secret = tmp_path / "parent-secret.txt"
    parent_secret.write_bytes(b"parent")
    sibling_dir = workspace.state_dir / "goalsX"
    sibling_dir.mkdir()
    sibling = sibling_dir / "sibling-secret.txt"
    sibling.write_bytes(b"sibling")
    return {
        "outside": outside,
        "root": root_secret,
        "parent": parent_secret,
        "sibling": sibling,
    }


def _assert_sentinels_untouched(sentinels: dict[str, Path]) -> None:
    expected = {
        "outside": b"outside",
        "root": b"root",
        "parent": b"parent",
        "sibling": b"sibling",
    }
    for key, path in sentinels.items():
        assert path.read_bytes() == expected[key]


# =============================================================================
# 1. start/resume reject a `lease.lock` symlink outside the goals root
# =============================================================================


def test_start_rejects_lease_symlink_outside(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.APPROVED)
    store.save("goal-a", record)

    sentinels = _sentinels(tmp_path, workspace)
    goal_dir = workspace.goals_dir / "goal-a"
    (goal_dir / "lease.lock").symlink_to(sentinels["outside"])

    runner = _goal_runner(workspace, store, clock)
    head_before = GitOps.head_sha(workspace.root)
    status_before = _git_porcelain_status(workspace.root)
    with pytest.raises(StorageError):
        runner.start("goal-a")

    _assert_sentinels_untouched(sentinels)
    assert (goal_dir / "lease.lock").is_symlink()
    assert not (goal_dir / "run-state.json").exists()
    assert not (goal_dir / "agent-process.json").exists()
    _assert_no_new_git_state(workspace, head_before, status_before)


def test_resume_rejects_lease_symlink_outside(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.RUNNING)
    store.save("goal-a", record)

    sentinels = _sentinels(tmp_path, workspace)
    goal_dir = workspace.goals_dir / "goal-a"
    (goal_dir / "lease.lock").symlink_to(sentinels["outside"])

    runner = _goal_runner(workspace, store, clock)
    head_before = GitOps.head_sha(workspace.root)
    status_before = _git_porcelain_status(workspace.root)
    with pytest.raises(StorageError):
        runner.resume("goal-a")

    _assert_sentinels_untouched(sentinels)
    assert (goal_dir / "lease.lock").is_symlink()
    assert not (goal_dir / "run-state.json").exists()
    assert not (goal_dir / "agent-process.json").exists()
    _assert_no_new_git_state(workspace, head_before, status_before)


# =============================================================================
# 2. GoalLease itself: symlink refused; a valid acquire/exclude/release still
#    works (the guard does not break the happy path).
# =============================================================================


def test_goal_lease_refuses_symlink_and_valid_lifecycle_still_works(
    tmp_path: Path,
) -> None:
    allowed_root = tmp_path / "root"
    allowed_root.mkdir()
    outside = tmp_path / "outside-target"
    outside.write_bytes(b"outside")
    bad_path = allowed_root / "lease.lock"
    bad_path.symlink_to(outside)

    bad_lease = GoalLease(bad_path, allowed_root=allowed_root)
    with pytest.raises(StorageError):
        bad_lease.acquire(owner="attacker")
    assert outside.read_bytes() == b"outside"
    assert bad_path.is_symlink()

    good_path = allowed_root / "real.lock"
    first = GoalLease(good_path, allowed_root=allowed_root)
    second = GoalLease(good_path, allowed_root=allowed_root)
    first.acquire(owner="first")
    with pytest.raises(SpecError, match="another process"):
        second.acquire(owner="second")
    first.release()
    second.acquire(owner="second")  # freed lease is reacquirable
    second.release()


# =============================================================================
# 3. run-state: symlink rejected on load and save, target/content unchanged.
# =============================================================================


def test_run_state_load_rejects_symlink(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_dir = workspace.goals_dir / "goal-a"
    outside_target = tmp_path / "outside-run-state"
    outside_target.write_bytes(b'{"outside": true}')
    (goal_dir / "run-state.json").symlink_to(outside_target)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(StorageError):
        runner.load_state("goal-a")
    assert outside_target.read_bytes() == b'{"outside": true}'
    assert (goal_dir / "run-state.json").is_symlink()


def test_run_state_save_rejects_symlink(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_dir = workspace.goals_dir / "goal-a"
    outside_target = tmp_path / "outside-run-state"
    outside_target.write_bytes(b'{"outside": true}')
    (goal_dir / "run-state.json").symlink_to(outside_target)

    with pytest.raises(StorageError):
        atomic_write_json(
            store.run_state_path("goal-a"),
            {"attacker": "payload"},
            allowed_root=workspace.goals_dir,
        )
    assert outside_target.read_bytes() == b'{"outside": true}'
    assert (goal_dir / "run-state.json").is_symlink()


# =============================================================================
# 4. agent-process: symlink rejected before termination and before invocation.
# =============================================================================


def test_terminate_recorded_process_rejects_symlink(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_dir = workspace.goals_dir / "goal-a"
    outside_target = tmp_path / "outside-agent-process"
    outside_target.write_bytes(json.dumps({"pid": 1, "command": "init"}).encode())
    record_path = goal_dir / "agent-process.json"
    record_path.symlink_to(outside_target)

    # Exercise `terminate_recorded_process`'s own in-function symlink guard
    # directly on the leaf, not the store's path-construction guard (which
    # would already refuse a symlinked leaf before this function is ever
    # entered — see the lease tests in section 2 for that guard instead).
    with pytest.raises(StorageError):
        terminate_recorded_process(record_path, allowed_root=workspace.goals_dir)
    assert outside_target.read_bytes() == json.dumps({"pid": 1, "command": "init"}).encode()
    assert record_path.is_symlink()


def test_subprocess_runner_rejects_symlinked_process_record_before_spawn(
    tmp_path: Path,
) -> None:
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    goal_dir = tmp_path / "goal-a"
    goal_dir.mkdir()
    outside_target = tmp_path / "outside-agent-process"
    outside_target.write_bytes(b'{"outside": true}')
    record_path = goal_dir / "agent-process.json"
    record_path.symlink_to(outside_target)

    marker = tmp_path / "should-not-run.txt"
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
        command_template=(
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').write('x')",
        ),
        process_record_path=record_path,
        process_record_root=tmp_path,
    )
    with pytest.raises(StorageError):
        runner.run(invocation)
    assert not marker.exists()  # the child was never spawned
    assert outside_target.read_bytes() == b'{"outside": true}'
    assert record_path.is_symlink()


# =============================================================================
# 5. unknown/malformed/mismatched Goal via start and resume creates no lease.
# =============================================================================


def test_start_unknown_goal_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.start("nope")
    assert not (workspace.goals_dir / "nope").exists()


def test_resume_unknown_goal_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.resume("nope")
    assert not (workspace.goals_dir / "nope").exists()


def test_start_mismatched_goal_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.APPROVED))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = "goal-other"
    goal_path.write_text(json.dumps(doc), encoding="utf-8")

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.start("goal-a")
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()


def test_resume_mismatched_goal_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = "goal-other"
    goal_path.write_text(json.dumps(doc), encoding="utf-8")

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.resume("goal-a")
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()


def test_start_malformed_goal_json_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.APPROVED))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_path.write_text("not valid json {{{", encoding="utf-8")

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(StorageError):
        runner.start("goal-a")
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()


def test_resume_malformed_goal_json_creates_no_lease(tmp_path: Path) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_path.write_text("not valid json {{{", encoding="utf-8")

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(StorageError):
        runner.resume("goal-a")
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()


# =============================================================================
# 6. the second GoalStore.load happens under the held lease and is real: a
#    mutation applied as a side effect of the *first* (pre-lease) load is
#    seen by the second (post-lease) load and fails closed before any
#    state/agent effect.
# =============================================================================


def _lease_currently_held(lease_path: Path) -> bool:
    """True if some other holder has an exclusive flock on ``lease_path``."""
    fd = None
    try:
        fd = os.open(lease_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        if fd is not None:
            os.close(fd)


class _MutateOnFirstLoad:
    """Delegates every call to a real GoalStore; on the *first* `load` for a
    goal, mutates its on-disk manifest as a side effect (simulating a
    concurrent writer), so a subsequent `load` for the same name observes
    fresh disk state rather than anything cached from before the lease. The
    second call also asserts the lease is actually held at that point, so
    the reload is proven to happen *inside* the held lease, not merely after
    some unrelated second call."""

    def __init__(
        self,
        store: GoalStore,
        goal_path: Path,
        corrupt_doc: dict[str, Any],
        lease_path: Path,
    ) -> None:
        self._store = store
        self._goal_path = goal_path
        self._corrupt_doc = corrupt_doc
        self._lease_path = lease_path
        self.load_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def load(self, name: str) -> GoalRecord:
        self.load_calls += 1
        if self.load_calls == 2:
            assert _lease_currently_held(self._lease_path)
        record = self._store.load(name)
        if self.load_calls == 1:
            self._goal_path.write_text(json.dumps(self._corrupt_doc), encoding="utf-8")
        return record


def test_start_second_load_under_lease_observes_mutation_and_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    real_store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    real_store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.APPROVED))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    tampered = json.loads(goal_path.read_text(encoding="utf-8"))
    tampered["contract"]["name"] = "goal-other"
    mutating_store = _MutateOnFirstLoad(
        real_store, goal_path, tampered, real_store.lease_path("goal-a")
    )

    runner = _goal_runner(workspace, mutating_store, clock)  # type: ignore[arg-type]
    head_before = GitOps.head_sha(workspace.root)
    status_before = _git_porcelain_status(workspace.root)
    with pytest.raises(SpecError):
        runner.start("goal-a")

    assert mutating_store.load_calls == 2  # pre-lease + post-lease reload both ran
    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    assert not (workspace.goals_dir / "goal-a" / "agent-process.json").exists()
    _assert_no_new_git_state(workspace, head_before, status_before)


def test_resume_second_load_under_lease_observes_mutation_and_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    real_store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    real_store.save("goal-a", _record("goal-a", base_sha, clock, GoalStatus.RUNNING))
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    tampered = json.loads(goal_path.read_text(encoding="utf-8"))
    tampered["contract"]["name"] = "goal-other"
    mutating_store = _MutateOnFirstLoad(
        real_store, goal_path, tampered, real_store.lease_path("goal-a")
    )

    runner = _goal_runner(workspace, mutating_store, clock)  # type: ignore[arg-type]
    head_before = GitOps.head_sha(workspace.root)
    status_before = _git_porcelain_status(workspace.root)
    with pytest.raises(SpecError):
        runner.resume("goal-a")

    assert mutating_store.load_calls == 2
    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    assert not (workspace.goals_dir / "goal-a" / "agent-process.json").exists()
    _assert_no_new_git_state(workspace, head_before, status_before)


# =============================================================================
# 7. collect_baseline: expected-name mismatch fails before progress/Git/
#    shell/save/log.
# =============================================================================


def test_collect_baseline_expected_name_mismatch_fails_before_any_effect(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.APPROVED)
    progress: list[str] = []
    head_before = GitOps.head_sha(workspace.root)
    status_before = _git_porcelain_status(workspace.root)

    with pytest.raises(SpecError, match="expected name"):
        collect_baseline(
            workspace=workspace,
            config=WorkspaceConfig(workspace),
            goals=store,
            record=record,
            expected_name="goal-b",
            monotonic=SteppingMonotonic(),
            progress=progress.append,
        )

    assert progress == []  # no progress line was ever emitted
    _assert_no_new_git_state(workspace, head_before, status_before)
    assert not (workspace.goals_dir / "goal-a").exists()
    assert not (workspace.goals_dir / "goal-b").exists()


def test_collect_baseline_non_string_expected_name_fails_before_any_effect(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.APPROVED)
    progress: list[str] = []

    with pytest.raises(SpecError):
        collect_baseline(
            workspace=workspace,
            config=WorkspaceConfig(workspace),
            goals=store,
            record=record,
            expected_name=123,  # type: ignore[arg-type]
            monotonic=SteppingMonotonic(),
            progress=progress.append,
        )
    assert progress == []


# =============================================================================
# 8. prepare_goal: invalid absolute/traversal/non-string name fails before
#    every spyable Git/conversation/route/runner effect.
# =============================================================================


class _PoisonedConversations:
    """A ConversationStore double that fails the test if ever read."""

    def messages(self, conversation_id: str) -> list[dict[str, object]]:  # pragma: no cover
        raise AssertionError("conversations.messages unexpectedly called")


class _PoisonedGoalStore:
    """A GoalStore double that fails the test if ever queried/written."""

    def exists(self, name: str) -> bool:  # pragma: no cover
        raise AssertionError("goals.exists unexpectedly called")

    def load(self, name: str) -> GoalRecord:  # pragma: no cover
        raise AssertionError("goals.load unexpectedly called")

    def save(self, name: str, record: GoalRecord) -> None:  # pragma: no cover
        raise AssertionError("goals.save unexpectedly called")

    def log_event(self, name: str, event: dict[str, object]) -> None:  # pragma: no cover
        raise AssertionError("goals.log_event unexpectedly called")


@pytest.mark.parametrize(
    "bad_name",
    ["/etc/passwd", "../escape", "a/b", "", 123],
)
def test_prepare_goal_invalid_name_fails_before_every_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_name: object
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()

    def _poisoned_is_repo(cwd: Path) -> bool:  # pragma: no cover
        raise AssertionError("GitOps.is_repo unexpectedly called")

    monkeypatch.setattr(GitOps, "is_repo", staticmethod(_poisoned_is_repo))

    with pytest.raises(SpecError, match="prepare_goal: name"):
        prepare_goal(
            workspace=workspace,
            config=WorkspaceConfig(workspace),
            conversations=_PoisonedConversations(),  # type: ignore[arg-type]
            goals=_PoisonedGoalStore(),  # type: ignore[arg-type]
            catalog=ResourceCatalog({}),
            runner=_NeverCalledRunner(),
            clock=clock,
            name=bad_name,  # type: ignore[arg-type]
            conversation_id="conv-1",
            progress=lambda _line: (_ for _ in ()).throw(
                AssertionError("progress unexpectedly called")
            ),
        )


# =============================================================================
# 9. deterministic writer-worktree: valid `worktree=None` creates and persists
#    exactly `worktrees_dir / f"{goal_name}-{run_id}"`; a valid exact path
#    resumes without re-creating it (R1-A run-state binding).
# =============================================================================


class _FakeAgentRunner:
    """Writes a marker file and returns a fixed reply; records invocations."""

    def __init__(self) -> None:
        self.calls: list[AgentInvocation] = []

    def run(self, invocation: AgentInvocation) -> AgentExecution:
        self.calls.append(invocation)
        (invocation.cwd / "marker.txt").write_text("done\n", encoding="utf-8")
        return AgentExecution(
            runtime=invocation.runtime,
            model=invocation.model,
            reasoning=invocation.reasoning,
            cwd=str(invocation.cwd),
            started_ts="2026-08-16T00:00:00.000Z",
            finished_ts="2026-08-16T00:00:01.000Z",
            duration_ms=1,
            exit_code=0,
            timed_out=False,
            output_text="ok",
            prompt_path="/dev/null",
            output_path="/dev/null",
            log_path="/dev/null",
        )


def test_start_creates_and_persists_exact_deterministic_worktree(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.APPROVED)
    record.baseline = {"status": "completed", "base_sha": base_sha, "checks": {"c1": {"ok": True}}}
    store.save("goal-a", record)

    fake_runner = _FakeAgentRunner()
    runner = _goal_runner(workspace, store, clock, runner=fake_runner)
    runner.start("goal-a")

    state = store.run_state_path("goal-a")
    doc = json.loads(state.read_text(encoding="utf-8"))
    run_id = doc["run_id"]
    expected = workspace.worktrees_dir / f"goal-a-{run_id}"
    assert doc["worktree"] == str(expected)
    assert expected.is_dir()
    assert not expected.is_symlink()


def test_resume_reuses_exact_persisted_worktree_without_recreating(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    base_sha = GitOps.head_sha(workspace.root)
    record = _record("goal-a", base_sha, clock, GoalStatus.APPROVED)
    record.baseline = {"status": "completed", "base_sha": base_sha, "checks": {"c1": {"ok": True}}}
    store.save("goal-a", record)

    fake_runner = _FakeAgentRunner()
    runner = _goal_runner(workspace, store, clock, runner=fake_runner)
    runner.start("goal-a")

    state_path = store.run_state_path("goal-a")
    doc = json.loads(state_path.read_text(encoding="utf-8"))
    worktree_before = doc["worktree"]
    assert worktree_before is not None
    mtime_before = Path(worktree_before).stat().st_mtime_ns

    # A crashed run left both the goal and its run-state "running" with a
    # valid, exactly-matching persisted worktree; resume must reuse it, not
    # recreate it.
    doc["status"] = "running"
    state_path.write_text(json.dumps(doc), encoding="utf-8")
    running_record = store.load("goal-a")
    running_record.status = GoalStatus.RUNNING
    store.save("goal-a", running_record)
    runner2 = _goal_runner(workspace, store, clock, runner=fake_runner)
    runner2.resume("goal-a")

    assert Path(worktree_before).stat().st_mtime_ns == mtime_before
    final_doc = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_doc["worktree"] == worktree_before
