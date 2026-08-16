"""R1-A SEC-001 slice: durable run-state identity/base/worktree binding.

``RunState`` construction and durable deserialization enforce the canonical
filesystem-ID contract with no coercion. Every state load — ``load_state``,
``start``, ``resume``, and CLI ``goal show`` — goes through one binder
(``GoalRunner._bind_state``) that requires the requested Goal name, the
durable contract identity, the run-state's own identity, the frozen contract
base SHA, and any persisted worktree claim to match byte-for-byte. A
valid-but-different, case-different, or wrong-type internal value is
corruption and fails closed before any lease/reconciliation/process/Git/
shell/agent effect.

No real Git repository is needed here: every negative case fails before any
Git call, so plain temp directories are enough (the happy worktree path and
crash reconciliation are covered by the existing Git-backed integration
suite).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dagvane.application.autodev import GoalRunner, RunState
from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    GoalStore,
)
from dagvane.application.resources import ResourceCatalog
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.agent import AgentExecution, AgentInvocation
from dagvane.ports.runtime import FixedClock, SequentialIds, SteppingMonotonic
from dagvane.workspace.config import WorkspaceConfig
from dagvane.workspace.paths import Workspace

BASE_SHA = "a" * 40
OTHER_SHA = "b" * 40


# -- shared fixtures / builders -----------------------------------------------


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    workspace = Workspace(root)
    workspace.ensure()
    return workspace


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


def _contract(name: str, base_sha: str = BASE_SHA) -> GoalContract:
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


def _record(
    name: str, clock: FixedClock, status: GoalStatus, base_sha: str = BASE_SHA
) -> GoalRecord:
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


class _NeverCalledClock:
    def now_iso(self) -> str:  # pragma: no cover - a call fails the test
        raise AssertionError("clock unexpectedly read")


class _NeverCalledWorktrees:
    def target_path(self, spec: Any) -> Path:  # pragma: no cover
        raise AssertionError(f"worktree lifecycle unexpectedly queried: {spec}")

    def create(self, spec: Any) -> Any:  # pragma: no cover
        raise AssertionError(f"worktree lifecycle unexpectedly invoked: {spec}")

    def remove(self, handle: Any) -> None:  # pragma: no cover
        raise AssertionError(f"worktree lifecycle unexpectedly invoked: {handle}")


def _fake_probe(calls: list[int]) -> Callable[..., bool]:
    """A ``probe_local_model`` double that records one call and always
    reports LOCAL unavailable, never touching the network."""

    def probe(catalog: ResourceCatalog, resource_id: str = "ollama-local") -> bool:
        calls.append(1)
        return False

    return probe


def _goal_runner(workspace: Workspace, store: GoalStore, clock: FixedClock) -> GoalRunner:
    return GoalRunner(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        store=store,
        catalog=ResourceCatalog({}),
        runner=_NeverCalledRunner(),
        clock=clock,
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
        progress=lambda _msg: None,
    )


def _valid_state_doc(
    *,
    run_id: str = "run-1",
    goal_name: str = "goal-a",
    base_sha: str = BASE_SHA,
    status: str = "running",
    worktree: str | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "goal_name": goal_name,
        "base_sha": base_sha,
        "status": status,
        "started_ts": "2026-08-16T00:00:00.000Z",
        "worktree": worktree,
        "candidate_sha": None,
        "tested_sha": None,
        "agent_calls": 0,
        "attempts": 0,
        "consecutive_failures": 0,
        "last_unmet": [],
        "review_passed_for": None,
        "review_findings": [],
        "reviews": [],
        "review_failures": 0,
        "routing_log": [],
        "check_results": {},
        "verify_ok": None,
        "verification": None,
        "implement_resource_id": None,
        "finish_reason": None,
    }


def _write_state(store: GoalStore, name: str, doc: dict[str, Any]) -> Path:
    path = store.run_state_path(name)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _sentinels(tmp_path: Path, workspace: Workspace) -> dict[str, Path]:
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside")
    root_secret = workspace.root / "root-secret.txt"
    root_secret.write_bytes(b"root")
    parent_secret = tmp_path / "parent-secret.txt"
    parent_secret.write_bytes(b"parent")
    return {"outside": outside, "root": root_secret, "parent": parent_secret}


def _assert_sentinels_untouched(sentinels: dict[str, Path]) -> None:
    expected = {"outside": b"outside", "root": b"root", "parent": b"parent"}
    for key, path in sentinels.items():
        assert path.read_bytes() == expected[key]


# =============================================================================
# 1. RunState direct construction and from_doc: strict identity, no coercion
# =============================================================================


def test_valid_round_trip() -> None:
    doc = _valid_state_doc()
    state = RunState.from_doc(doc)
    assert state.run_id == "run-1"
    assert state.goal_name == "goal-a"
    assert state.base_sha == BASE_SHA
    assert state.worktree is None
    assert state.to_doc()["run_id"] == "run-1"

    doc2 = _valid_state_doc(worktree="/abs/path")
    state2 = RunState.from_doc(doc2)
    assert state2.worktree == "/abs/path"


@pytest.mark.parametrize("candidate_key_present", [False, True])
def test_from_doc_accepts_absent_or_null_candidate_sha(
    candidate_key_present: bool,
) -> None:
    doc = _valid_state_doc()
    if not candidate_key_present:
        del doc["candidate_sha"]

    state = RunState.from_doc(doc)

    assert state.candidate_sha is None


@pytest.mark.parametrize(
    "bad_sha",
    [
        123,
        True,
        1.5,
        [],
        {},
        "",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        f"{'a' * 40}\n",
    ],
)
def test_from_doc_rejects_present_invalid_candidate_sha(bad_sha: Any) -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = bad_sha

    with pytest.raises(SpecError, match="candidate_sha"):
        RunState.from_doc(doc)


@pytest.mark.parametrize("field", ["tested_sha", "review_passed_for"])
@pytest.mark.parametrize("bad_sha", [123, True, [], {}, "", "a" * 39, "A" * 40])
def test_from_doc_rejects_invalid_coupled_optional_sha(
    field: str, bad_sha: Any
) -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = BASE_SHA
    doc[field] = bad_sha

    with pytest.raises(SpecError, match=field):
        RunState.from_doc(doc)


@pytest.mark.parametrize("candidate_key_present", [False, True])
@pytest.mark.parametrize("field", ["tested_sha", "review_passed_for"])
def test_from_doc_rejects_coupled_sha_without_candidate(
    field: str, candidate_key_present: bool
) -> None:
    doc = _valid_state_doc()
    if not candidate_key_present:
        del doc["candidate_sha"]
    doc[field] = BASE_SHA

    with pytest.raises(SpecError, match=field):
        RunState.from_doc(doc)


def test_from_doc_rejects_tested_sha_for_another_candidate() -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = BASE_SHA
    doc["tested_sha"] = OTHER_SHA

    with pytest.raises(SpecError, match="tested_sha"):
        RunState.from_doc(doc)


def test_from_doc_rejects_current_review_marker_without_current_tested_sha() -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = BASE_SHA
    doc["review_passed_for"] = BASE_SHA

    with pytest.raises(SpecError, match="review_passed_for.*tested_sha"):
        RunState.from_doc(doc)


def test_from_doc_accepts_current_review_marker_with_current_tested_sha() -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = BASE_SHA
    doc["tested_sha"] = BASE_SHA
    doc["review_passed_for"] = BASE_SHA

    state = RunState.from_doc(doc)

    assert state.tested_sha == BASE_SHA
    assert state.review_passed_for == BASE_SHA


def test_from_doc_accepts_stale_review_marker_for_prior_candidate() -> None:
    doc = _valid_state_doc()
    doc["candidate_sha"] = OTHER_SHA
    doc["review_passed_for"] = BASE_SHA

    state = RunState.from_doc(doc)

    assert state.candidate_sha == OTHER_SHA
    assert state.review_passed_for == BASE_SHA


@pytest.mark.parametrize("bad_id", ["", "../x", "a/b", "a\\b", "café", 123, None, True, "x" * 65])
def test_direct_construction_rejects_invalid_run_id(bad_id: Any) -> None:
    with pytest.raises(SpecError):
        RunState(
            run_id=bad_id,
            goal_name="goal-a",
            base_sha=BASE_SHA,
            status="running",
            started_ts="2026-08-16T00:00:00.000Z",
        )


@pytest.mark.parametrize("bad_name", ["", "../x", "a/b", 123, None])
def test_direct_construction_rejects_invalid_goal_name(bad_name: Any) -> None:
    with pytest.raises(SpecError):
        RunState(
            run_id="run-1",
            goal_name=bad_name,
            base_sha=BASE_SHA,
            status="running",
            started_ts="2026-08-16T00:00:00.000Z",
        )


def test_direct_construction_accepts_case_different_valid_id() -> None:
    # A case-different value is a *valid* filesystem id on its own —
    # construction alone must succeed; rejecting it as a foreign identity is
    # the binder's job (see the case-mismatch binding tests below), not
    # `RunState`'s.
    RunState(
        run_id="run-1",
        goal_name="Goal-A",
        base_sha=BASE_SHA,
        status="running",
        started_ts="2026-08-16T00:00:00.000Z",
    )


@pytest.mark.parametrize("bad_id", ["", "../x", "a/b", 123, None, True])
def test_from_doc_rejects_invalid_run_id_no_coercion(bad_id: Any) -> None:
    doc = _valid_state_doc()
    doc["run_id"] = bad_id
    with pytest.raises(SpecError):
        RunState.from_doc(doc)


@pytest.mark.parametrize("bad_name", ["", "../x", "a/b", 123, None])
def test_from_doc_rejects_invalid_goal_name_no_coercion(bad_name: Any) -> None:
    doc = _valid_state_doc()
    doc["goal_name"] = bad_name
    with pytest.raises(SpecError):
        RunState.from_doc(doc)


@pytest.mark.parametrize("bad_sha", [123, True, None, ["a"], {"a": 1}])
def test_from_doc_rejects_non_string_base_sha(bad_sha: Any) -> None:
    doc = _valid_state_doc()
    doc["base_sha"] = bad_sha
    with pytest.raises(SpecError):
        RunState.from_doc(doc)


@pytest.mark.parametrize("bad_worktree", [123, True, [], {}])
def test_from_doc_rejects_wrong_type_worktree(bad_worktree: Any) -> None:
    doc = _valid_state_doc()
    doc["worktree"] = bad_worktree
    with pytest.raises(SpecError):
        RunState.from_doc(doc)


@pytest.mark.parametrize(
    "key", ["run_id", "goal_name", "base_sha", "status", "started_ts"]
)
def test_from_doc_missing_required_field_is_controlled_spec_error(key: str) -> None:
    doc = _valid_state_doc()
    del doc[key]
    with pytest.raises(SpecError):
        RunState.from_doc(doc)


# =============================================================================
# 2. load_state / start / resume / CLI show: binding fails closed
# =============================================================================


def test_load_state_rejects_mismatched_goal_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    _write_state(store, "goal-a", _valid_state_doc(goal_name="goal-b"))

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.load_state("goal-a")


def test_load_state_rejects_case_different_goal_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    _write_state(store, "goal-a", _valid_state_doc(goal_name="Goal-a"))

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.load_state("goal-a")


def test_load_state_rejects_invalid_type_goal_name(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    path = store.run_state_path("goal-a")
    doc = _valid_state_doc()
    doc["goal_name"] = 12345
    path.write_text(json.dumps(doc), encoding="utf-8")

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.load_state("goal-a")


def test_load_state_rejects_base_sha_mismatch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA))
    _write_state(store, "goal-a", _valid_state_doc(base_sha=OTHER_SHA))

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.load_state("goal-a")


@pytest.mark.parametrize(
    ("candidate_sha", "tested_sha", "review_passed_for"),
    [
        (123, None, None),
        (True, None, None),
        ("A" * 40, None, None),
        (BASE_SHA, OTHER_SHA, None),
        (BASE_SHA, None, BASE_SHA),
    ],
)
def test_resume_rejects_invalid_candidate_evidence_before_any_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_sha: Any,
    tested_sha: Any,
    review_passed_for: Any,
) -> None:
    workspace = _workspace(tmp_path)
    setup_clock = _clock()
    store = GoalStore(workspace, setup_clock)
    store.save(
        "goal-a",
        _record("goal-a", setup_clock, GoalStatus.RUNNING, base_sha=BASE_SHA),
    )
    doc = _valid_state_doc(base_sha=BASE_SHA)
    doc["candidate_sha"] = candidate_sha
    doc["tested_sha"] = tested_sha
    doc["review_passed_for"] = review_passed_for
    state_path = _write_state(store, "goal-a", doc)
    state_bytes_before = state_path.read_bytes()
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)
    probe_calls: list[int] = []
    monkeypatch.setattr(
        "dagvane.application.autodev.probe_local_model", _fake_probe(probe_calls)
    )
    runner = GoalRunner(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        store=store,
        catalog=ResourceCatalog({}),
        runner=_NeverCalledRunner(),
        clock=_NeverCalledClock(),
        monotonic=SteppingMonotonic(),
        ids=SequentialIds(seed="t"),
        progress=lambda _msg: None,
        worktrees=_NeverCalledWorktrees(),
    )

    with pytest.raises(SpecError):
        runner.resume("goal-a")

    assert probe_calls == []
    assert state_path.read_bytes() == state_bytes_before
    assert goal_path.read_bytes() == goal_bytes_before
    assert not (workspace.goals_dir / "goal-a" / "log.jsonl").exists()
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()
    assert list(workspace.worktrees_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


@pytest.mark.parametrize(
    "bad_doc_mutator",
    [
        lambda doc: doc.__setitem__("goal_name", "goal-b"),
        lambda doc: doc.__setitem__("goal_name", "Goal-a"),
        lambda doc: doc.__setitem__("goal_name", 42),
        lambda doc: doc.__setitem__("base_sha", OTHER_SHA),
    ],
)
def test_start_fails_before_any_effect_on_mismatched_state(
    tmp_path: Any, bad_doc_mutator: Any
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record)
    doc = _valid_state_doc(base_sha=BASE_SHA)
    bad_doc_mutator(doc)
    state_path = _write_state(store, "goal-a", doc)
    state_bytes_before = state_path.read_bytes()
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.start("goal-a")

    assert state_path.read_bytes() == state_bytes_before
    assert goal_path.read_bytes() == goal_bytes_before
    assert not (workspace.goals_dir / "goal-a" / "log.jsonl").exists()
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()
    assert not workspace.worktrees_dir.exists() or list(workspace.worktrees_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


@pytest.mark.parametrize(
    "bad_doc_mutator",
    [
        lambda doc: doc.__setitem__("goal_name", "goal-b"),
        lambda doc: doc.__setitem__("goal_name", "Goal-a"),
        lambda doc: doc.__setitem__("goal_name", 42),
        lambda doc: doc.__setitem__("base_sha", OTHER_SHA),
    ],
)
def test_resume_fails_before_any_effect_on_mismatched_state(
    tmp_path: Any, bad_doc_mutator: Any
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record)
    doc = _valid_state_doc(base_sha=BASE_SHA)
    bad_doc_mutator(doc)
    state_path = _write_state(store, "goal-a", doc)
    state_bytes_before = state_path.read_bytes()
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.resume("goal-a")

    assert state_path.read_bytes() == state_bytes_before
    assert goal_path.read_bytes() == goal_bytes_before
    assert not (workspace.goals_dir / "goal-a" / "log.jsonl").exists()
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()
    assert not workspace.worktrees_dir.exists() or list(workspace.worktrees_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_cli_goal_show_never_prints_mismatched_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from dagvane.cli import main

    root = tmp_path / "project"
    root.mkdir()
    workspace = Workspace(root)
    workspace.ensure()
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    _write_state(store, "goal-a", _valid_state_doc(goal_name="goal-b"))

    monkeypatch.chdir(root)
    exit_code = main(["goal", "show", "goal-a"])
    assert exit_code != 0
    out = capsys.readouterr().out
    assert "goal-b" not in out
    assert '"run"' not in out


# =============================================================================
# 3. symlinked run-state.json leaf: no target read/write
# =============================================================================


def test_load_state_rejects_symlinked_run_state_leaf(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    outside = tmp_path / "outside-run-state.json"
    outside.write_bytes(b'{"outside": true}')
    run_state_path = workspace.goals_dir / "goal-a" / "run-state.json"
    run_state_path.symlink_to(outside)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(StorageError):
        runner.load_state("goal-a")
    assert outside.read_bytes() == b'{"outside": true}'
    assert run_state_path.is_symlink()


# =============================================================================
# 4. persisted worktree claims: only None or the exact deterministic path
# =============================================================================


def _expected_worktree(workspace: Workspace, goal_name: str, run_id: str) -> str:
    return str(workspace.worktrees_dir / f"{goal_name}-{run_id}")


@pytest.mark.parametrize(
    "bad_worktree_fn",
    [
        lambda ws: "/etc/outside",
        lambda ws: str(ws.state_dir),
        lambda ws: str(ws.worktrees_dir),
        lambda ws: str(ws.worktrees_dir.parent),
        lambda ws: str(ws.worktrees_dir / "goal-a-run-1x"),  # sibling-prefix
        lambda ws: str(ws.worktrees_dir / "goal-a-run-2"),  # another run
        lambda ws: str(ws.worktrees_dir / "goal-a-run-1-verify"),
        lambda ws: str(ws.worktrees_dir / "goal-a-run-1-review"),
        lambda ws: "goal-a-run-1",  # relative alias, not absolute
        lambda ws: "",
    ],
)
def test_load_state_rejects_bad_worktree_claims(tmp_path: Path, bad_worktree_fn: Any) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    bad_worktree = bad_worktree_fn(workspace)
    state_path = _write_state(
        store, "goal-a", _valid_state_doc(run_id="run-1", worktree=bad_worktree)
    )
    state_bytes_before = state_path.read_bytes()
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.load_state("goal-a")

    assert state_path.read_bytes() == state_bytes_before
    assert goal_path.read_bytes() == goal_bytes_before
    assert not workspace.worktrees_dir.exists() or list(workspace.worktrees_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_load_state_rejects_symlinked_worktree_claim(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    expected = workspace.worktrees_dir / "goal-a-run-1"
    outside = tmp_path / "outside-worktree"
    outside.mkdir()
    expected.symlink_to(outside, target_is_directory=True)
    state_path = _write_state(
        store, "goal-a", _valid_state_doc(run_id="run-1", worktree=str(expected))
    )
    state_bytes_before = state_path.read_bytes()
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)

    runner = _goal_runner(workspace, store, clock)
    # The worktree claim's *value* matches the expected string exactly, but
    # `_expected_worktree_path` proves the path is symlink-free before ever
    # accepting the claim — the string match alone is not authority.
    with pytest.raises(StorageError):
        runner.load_state("goal-a")
    assert list(outside.iterdir()) == []
    assert expected.is_symlink()
    assert state_path.read_bytes() == state_bytes_before
    assert goal_path.read_bytes() == goal_bytes_before
    _assert_sentinels_untouched(sentinels)


def test_load_state_accepts_none_and_exact_expected_worktree(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING))
    runner = _goal_runner(workspace, store, clock)

    _write_state(store, "goal-a", _valid_state_doc(run_id="run-1", worktree=None))
    state = runner.load_state("goal-a")
    assert state is not None
    assert state.worktree is None

    expected = _expected_worktree(workspace, "goal-a", "run-1")
    _write_state(store, "goal-a", _valid_state_doc(run_id="run-1", worktree=expected))
    state2 = runner.load_state("goal-a")
    assert state2 is not None
    assert state2.worktree == expected


# =============================================================================
# 6. injected invalid IdSource.new_id("goalrun") fails before any effect
# =============================================================================


class _BadIds:
    def new_id(self, kind: str) -> str:
        return "../evil"


def test_start_rejects_invalid_generated_run_id_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.APPROVED)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    goal_bytes_before = goal_path.read_bytes()
    sentinels = _sentinels(tmp_path, workspace)

    probe_calls: list[int] = []
    monkeypatch.setattr(
        "dagvane.application.autodev.probe_local_model", _fake_probe(probe_calls)
    )

    # `router.local_enabled` defaults to True (see DEFAULT_CONFIG), so an
    # invalid generated id must still short-circuit before the probe is ever
    # reached — the ordering, not a disabled probe, is what is under test.
    runner = GoalRunner(
        workspace=workspace,
        config=WorkspaceConfig(workspace),
        store=store,
        catalog=ResourceCatalog({}),
        runner=_NeverCalledRunner(),
        clock=clock,
        monotonic=SteppingMonotonic(),
        ids=_BadIds(),
        progress=lambda _msg: None,
    )
    with pytest.raises(SpecError):
        runner.start("goal-a")

    assert probe_calls == []
    assert goal_path.read_bytes() == goal_bytes_before
    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    assert not (workspace.goals_dir / "goal-a" / "log.jsonl").exists()
    assert not (workspace.goals_dir / "goal-a" / "lease.lock").exists()
    assert not workspace.worktrees_dir.exists() or list(workspace.worktrees_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


# =============================================================================
# 7. mutation between preflight and lease is caught by the under-lease re-read
# =============================================================================


def test_resume_reread_under_lease_catches_mutation_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record)
    state_path = _write_state(
        store, "goal-a", _valid_state_doc(run_id="run-1", base_sha=BASE_SHA)
    )
    probe_calls: list[int] = []
    monkeypatch.setattr(
        "dagvane.application.autodev.probe_local_model", _fake_probe(probe_calls)
    )

    runner = _goal_runner(workspace, store, clock)
    from dagvane.workspace.lease import GoalLease

    real_acquire = GoalLease.acquire

    def tampering_acquire(self: GoalLease, *, owner: str) -> None:
        # Simulate a concurrent tamper landing between the pre-lease
        # preflight read and the moment the lease is actually held.
        doc = _valid_state_doc(run_id="run-1", base_sha=OTHER_SHA)
        state_path.write_text(json.dumps(doc), encoding="utf-8")
        real_acquire(self, owner=owner)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GoalLease, "acquire", tampering_acquire)
        with pytest.raises(SpecError):
            runner.resume("goal-a")

    # The under-lease re-read/re-bind rejects the tampered state before the
    # probe — which now lives entirely inside the held lease, after that
    # re-read/re-bind and every applicable status check — is ever reached.
    assert probe_calls == []


def test_start_reread_under_lease_catches_mutation_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.APPROVED, base_sha=BASE_SHA)
    store.save("goal-a", record)
    probe_calls: list[int] = []
    monkeypatch.setattr(
        "dagvane.application.autodev.probe_local_model", _fake_probe(probe_calls)
    )

    runner = _goal_runner(workspace, store, clock)
    from dagvane.workspace.lease import GoalLease

    real_acquire = GoalLease.acquire

    def tampering_acquire(self: GoalLease, *, owner: str) -> None:
        # Simulate a concurrent tamper (e.g. a cancel + a foreign run-state
        # write) landing between the pre-lease preflight read — which, for
        # `start`, now also generates and validates the new run id — and the
        # moment the lease is actually held.
        doc = _valid_state_doc(run_id="run-1", goal_name="goal-a", base_sha=OTHER_SHA)
        _write_state(store, "goal-a", doc)
        real_acquire(self, owner=owner)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(GoalLease, "acquire", tampering_acquire)
        with pytest.raises(SpecError):
            runner.start("goal-a")

    # The under-lease re-read/re-bind rejects the tampered state before the
    # probe — which now lives entirely inside the held lease, after that
    # re-read/re-bind and every applicable status check — is ever reached.
    assert probe_calls == []


# =============================================================================
# 9. Ollama availability probe fires only after successful preflight binding
# =============================================================================


def _patched_probe(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr("dagvane.application.autodev.probe_local_model", _fake_probe(calls))
    return calls


@pytest.mark.parametrize(
    "bad_doc_mutator",
    [
        lambda doc: doc.__setitem__("goal_name", "goal-b"),
        lambda doc: doc.__setitem__("base_sha", OTHER_SHA),
    ],
)
def test_start_probe_call_count_zero_on_inconsistent_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_doc_mutator: Any
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA))
    doc = _valid_state_doc(base_sha=BASE_SHA)
    bad_doc_mutator(doc)
    _write_state(store, "goal-a", doc)
    calls = _patched_probe(monkeypatch)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.start("goal-a")
    assert calls == []


@pytest.mark.parametrize(
    "bad_doc_mutator",
    [
        lambda doc: doc.__setitem__("goal_name", "goal-b"),
        lambda doc: doc.__setitem__("base_sha", OTHER_SHA),
    ],
)
def test_resume_probe_call_count_zero_on_inconsistent_saved_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_doc_mutator: Any
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA))
    doc = _valid_state_doc(base_sha=BASE_SHA)
    bad_doc_mutator(doc)
    _write_state(store, "goal-a", doc)
    calls = _patched_probe(monkeypatch)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.resume("goal-a")
    assert calls == []


def test_resume_probe_call_count_zero_with_no_run_to_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No saved run-state is a clean (if empty) preflight bind, but the
    # under-lease re-read still finds nothing to resume and raises before
    # the probe — which lives after that check — is ever reached.
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.APPROVED))
    calls = _patched_probe(monkeypatch)

    runner = _goal_runner(workspace, store, clock)
    with pytest.raises(SpecError):
        runner.resume("goal-a")
    assert calls == []


def test_start_probes_only_after_binding_and_valid_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.APPROVED))
    calls = _patched_probe(monkeypatch)

    runner = _goal_runner(workspace, store, clock)
    # No real Git repo/worktree exists, so the run fails once it reaches
    # baseline collection — well after preflight binding, run-id validation,
    # and (the point under test) exactly one probe call.
    with pytest.raises(SpecError):
        runner.start("goal-a")
    assert calls == [1]


def test_resume_probes_only_after_binding_on_valid_running_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    store.save("goal-a", _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA))
    _write_state(store, "goal-a", _valid_state_doc(run_id="run-1", base_sha=BASE_SHA))
    calls = _patched_probe(monkeypatch)

    runner = _goal_runner(workspace, store, clock)
    # No real Git repo/worktree exists, so candidate lifecycle reconciliation
    # fails before any optional local-model availability probe. This prevents
    # effects from observing a stale RunState SHA after an interrupted advance.
    with pytest.raises(SpecError):
        runner.resume("goal-a")
    assert calls == []


# =============================================================================
# 10. `_save_state` re-binds to the trusted Goal contract before any write
# =============================================================================


def test_save_state_with_changed_goal_name_writes_nowhere(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record_a = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record_a)
    record_b = _record("goal-b", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-b", record_b)
    runner = _goal_runner(workspace, store, clock)

    state = RunState(
        run_id="run-1",
        goal_name="goal-b",  # identity changed underneath the caller
        base_sha=BASE_SHA,
        status="running",
        started_ts=clock.now_iso(),
    )
    sentinels = _sentinels(tmp_path, workspace)

    with pytest.raises(SpecError):
        runner._save_state(record_a, state)  # noqa: SLF001 — testing the boundary directly

    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    assert not (workspace.goals_dir / "goal-b" / "run-state.json").exists()
    _assert_sentinels_untouched(sentinels)


def test_save_state_with_changed_base_sha_writes_nowhere(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record)
    runner = _goal_runner(workspace, store, clock)

    state = RunState(
        run_id="run-1",
        goal_name="goal-a",
        base_sha=OTHER_SHA,  # diverges from the frozen contract base SHA
        status="running",
        started_ts=clock.now_iso(),
    )
    sentinels = _sentinels(tmp_path, workspace)

    with pytest.raises(SpecError):
        runner._save_state(record, state)  # noqa: SLF001 — testing the boundary directly

    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    _assert_sentinels_untouched(sentinels)


def test_save_state_with_bad_worktree_claim_writes_nowhere(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record)
    runner = _goal_runner(workspace, store, clock)

    state = RunState(
        run_id="run-1",
        goal_name="goal-a",
        base_sha=BASE_SHA,
        status="running",
        started_ts=clock.now_iso(),
        worktree=str(workspace.worktrees_dir / "goal-a-run-2"),  # another run's path
    )
    sentinels = _sentinels(tmp_path, workspace)

    with pytest.raises(SpecError):
        runner._save_state(record, state)  # noqa: SLF001 — testing the boundary directly

    assert not (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    _assert_sentinels_untouched(sentinels)


def test_save_state_writes_only_the_bound_goal_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    store = GoalStore(workspace, clock)
    record_a = _record("goal-a", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-a", record_a)
    record_b = _record("goal-b", clock, GoalStatus.RUNNING, base_sha=BASE_SHA)
    store.save("goal-b", record_b)
    runner = _goal_runner(workspace, store, clock)

    state = RunState(
        run_id="run-1",
        goal_name="goal-a",
        base_sha=BASE_SHA,
        status="running",
        started_ts=clock.now_iso(),
    )
    runner._save_state(record_a, state)  # noqa: SLF001 — testing the boundary directly

    assert (workspace.goals_dir / "goal-a" / "run-state.json").exists()
    assert not (workspace.goals_dir / "goal-b" / "run-state.json").exists()


# =============================================================================
# 11. existing crash reconciliation / happy autonomous flow remain green
# =============================================================================
# Covered by tests/integration/test_autodev_mvp.py
# (test_goal_run_survives_crash_and_resumes,
#  test_goal_prepare_approve_run_achieves) and
# tests/integration/test_run_state_worktree.py, unaffected by this file.
