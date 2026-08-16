"""Autonomous Developer MVP remediation — mandatory offline regressions.

Deterministic evidence for the Codex acceptance findings at ``b40b9fb``:
draft-only preparation, clean exact-SHA baseline, secret scrubbing across the
ExternalAgent boundary, minimal child environment, immutable-candidate
verification, fail-closed mutation detection, crash reconciliation at both
split-write boundaries, the one-writer lease, real cancellation with process
reaping, durable blocking findings, exact-SHA review binding, and honest
routing escalation. Only fake local subprocesses and synthetic secrets — the
suite stays offline.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from dagvane.adapters.localexec import GitOps
from dagvane.adapters.worktrees import ManagedWorktrees, WorktreePurpose, WorktreeSpec
from dagvane.application import autodev as autodev_module
from dagvane.application.autodev import RunState
from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    GoalStore,
    approve,
)
from dagvane.application.prepare import collect_baseline, prepare_goal
from dagvane.application.resources import ResourceSpec, RoutingDecision, route_task
from dagvane.cli import main
from dagvane.cli_workspace import Composition, _goal_runner
from dagvane.domain.models import SpecError
from dagvane.workspace.config import render_toml
from dagvane.workspace.lease import GoalLease
from dagvane.workspace.paths import atomic_write_json

WAIT_SECONDS = 30.0


def _git(args: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True
    )
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _init_repo(root: Path, *, gitignore: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    if gitignore is not None:
        (root / ".gitignore").write_text(gitignore, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)


def _agent_script(tmp_path: Path, name: str, body: str) -> list[str]:
    """One fake agent: ``body`` runs with ``prompt``/``output`` (Paths) and
    ``bump(name) -> int`` (persistent per-script counters) in scope."""
    script = tmp_path / name
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "prompt_path = Path(sys.argv[1])\n"
        "prompt = prompt_path.read_text(encoding='utf-8')\n"
        "output = Path(sys.argv[2])\n"
        "ctrl = Path(__file__).resolve().parent\n"
        "def bump(name):\n"
        "    counter = ctrl / name\n"
        "    value = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "    counter.write_text(str(value))\n"
        "    return value\n" + body,
        encoding="utf-8",
    )
    return [sys.executable, str(script), "{prompt_file}", "{output_file}"]


# The workspace config merges OVER engine defaults, so the default (real)
# resources would stay routable — an escalated tier could silently reach the
# real `codex` CLI from an offline test. Disable every default explicitly.
DISABLED_DEFAULT_RESOURCES: dict[str, Any] = {
    resource_id: {"enabled": False}
    for resource_id in (
        "codex-cheap",
        "codex-standard",
        "codex-strong",
        "codex-critical",
        "ollama-local",
        "agy-review",
    )
}


def _write_config(
    root: Path,
    *,
    resources: dict[str, Any],
    goal: dict[str, Any],
    chat_resource: str | None = None,
) -> None:
    doc: dict[str, Any] = {
        "router": {"local_enabled": False},
        "goal": {"agent_timeout_seconds": 60, **goal},
        "resources": {**DISABLED_DEFAULT_RESOURCES, **resources},
    }
    if chat_resource is not None:
        doc["chat"] = {"resource": chat_resource}
    dagvane_dir = root / ".dagvane"
    dagvane_dir.mkdir(exist_ok=True)
    (dagvane_dir / "config.toml").write_text(
        render_toml(doc) + "\n", encoding="utf-8"
    )


def _resource(command: list[str], tier: str = "STANDARD", **extra: Any) -> dict[str, Any]:
    return {
        "kind": "external_agent",
        "runtime": "command",
        "tier": tier,
        "command": command,
        **extra,
    }


def _approved_goal(
    comp: Composition,
    name: str,
    *,
    checks: list[tuple[str, str]],
    verify: list[str] | None = None,
    max_attempts: int = 4,
    max_failures: int = 3,
    baseline_completed: bool = True,
) -> GoalRecord:
    base = GitOps.head_sha(comp.workspace.root)
    contract = GoalContract(
        name=name,
        base_sha=base,
        objective="test objective",
        must_have=["the deliverable"],
        non_goals=["everything else"],
        checks=[
            AcceptanceCheck(check_id=check_id, description=check_id, command=command)
            for check_id, command in checks
        ],
        verify_commands=list(verify) if verify is not None else ["true"],
        limits=GoalLimits(
            max_wall_seconds=3600,
            max_agent_calls=40,
            max_attempts=max_attempts,
            max_consecutive_failures=max_failures,
        ),
    )
    now = comp.clock.now_iso()
    record = GoalRecord(
        contract=contract,
        status=GoalStatus.PREPARED,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
        baseline={
            "status": "completed" if baseline_completed else "pending",
            "base_sha": base,
        },
    )
    approve(record)
    comp.goals.save(name, record)
    return record


def _run_state(comp: Composition, name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (comp.goals.goal_dir(name) / "run-state.json").read_text(encoding="utf-8")
    )
    return doc


def _goal_doc(comp: Composition, name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (comp.goals.goal_dir(name) / "goal.json").read_text(encoding="utf-8")
    )
    return doc


def _wait_for(path: Path, *, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.time() + timeout
    while not path.exists():
        if time.time() > deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.02)


def _wait_pid_gone(pid: int, *, timeout: float = WAIT_SECONDS) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} is still alive")


PREPARER_WITH_SIDE_EFFECTS = """
contract = {
    "objective": "obj",
    "must_have": ["m"],
    "non_goals": ["n"],
    "checks": [{"check_id": "probe", "description": "d",
                "command": "touch PRE_APPROVAL_SIDE_EFFECT && test -f marker.txt"}],
    "verify_commands": ["touch PRE_APPROVAL_VERIFY"],
}
output.write_text(json.dumps(contract), encoding="utf-8")
"""

MARKER_WRITER = """
if "single implementation writer" in prompt:
    call = bump("implement-calls")
    Path("marker.txt").write_text(f"attempt {call}\\n", encoding="utf-8")
    output.write_text("created the marker", encoding="utf-8")
else:
    output.write_text("chat reply", encoding="utf-8")
"""


# -- 1. draft-only preparation + 2. dirty refusal / clean exact baseline -----


def test_prepare_is_draft_only_and_baseline_runs_post_approval_in_disposable_worktree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(tmp_path, "preparer.py", PREPARER_WITH_SIDE_EFFECTS)
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"prepare_resource": "fake-agent"},
    )
    comp = Composition(root)
    conversation = comp.conversations.create()
    comp.conversations.append_message(conversation, "user", "we need marker.txt")

    record = prepare_goal(
        workspace=comp.workspace,
        config=comp.config,
        conversations=comp.conversations,
        goals=comp.goals,
        catalog=comp.catalog,
        runner=comp.runner,
        clock=comp.clock,
        name="draft-goal",
        conversation_id=conversation,
        progress=lambda _line: None,
    )

    # The model-proposed side-effect commands were persisted for review but
    # NOT executed: preparation is draft-only.
    assert record.status is GoalStatus.PREPARED
    assert record.baseline["status"] == "pending"
    assert "PRE_APPROVAL_SIDE_EFFECT" in record.contract.checks[0].command
    assert not (root / "PRE_APPROVAL_SIDE_EFFECT").exists()
    assert not (root / "PRE_APPROVAL_VERIFY").exists()

    # After explicit approval the commands run — in a disposable worktree at
    # the exact approved base SHA, never in the canonical worktree.
    approve(record)
    comp.goals.save("draft-goal", record)
    collect_baseline(
        workspace=comp.workspace,
        config=comp.config,
        goals=comp.goals,
        record=record,
        expected_name="draft-goal",
        monotonic=comp.monotonic,
        progress=lambda _line: None,
    )
    # Reload from disk: completion is durable, not just in-memory.
    reloaded = comp.goals.load("draft-goal")
    assert reloaded.baseline["status"] == "completed"
    assert reloaded.baseline["base_sha"] == record.contract.base_sha
    checks_evidence = reloaded.baseline["checks"]
    assert isinstance(checks_evidence, dict)
    assert checks_evidence["probe"]["ok"] is False  # marker.txt absent at base
    assert not (root / "PRE_APPROVAL_SIDE_EFFECT").exists()
    assert not (root / "PRE_APPROVAL_VERIFY").exists()
    assert GitOps.is_clean(root)
    assert not (comp.workspace.worktrees_dir / "draft-goal-baseline").exists()


def test_interrupted_baseline_is_retried_at_goal_run(tmp_path: Path) -> None:
    """An approval interrupted mid-baseline leaves `pending`; `goal run`
    collects the baseline first — idempotently, in a fresh disposable
    worktree — before any run state is created."""
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    record = _approved_goal(
        comp,
        "pending-baseline",
        checks=[("marker", "test -f marker.txt")],
        baseline_completed=False,
    )
    # Simulate a *genuinely managed* interruption of the first collection:
    # the baseline worktree was created through the managed protocol (owner
    # record + exact registration) but the collection crashed before its
    # managed cleanup. An unowned stray at the same path would be refused.
    stale = comp.workspace.worktrees_dir / "pending-baseline-baseline"
    manager = ManagedWorktrees(
        repo_root=comp.workspace.root, worktrees_root=comp.workspace.worktrees_dir
    )
    spec = WorktreeSpec(
        goal_name="pending-baseline",
        purpose=WorktreePurpose.BASELINE,
        sha=record.contract.base_sha,
    )
    stale_handle = manager.create(spec)
    assert stale_handle.path == stale
    stale_handle.close()  # simulate the interrupted collector process

    status = _goal_runner(comp).start("pending-baseline")
    assert status is GoalStatus.ACHIEVED
    reloaded = comp.goals.load("pending-baseline")
    assert reloaded.baseline["status"] == "completed"
    retried_checks = reloaded.baseline["checks"]
    assert isinstance(retried_checks, dict)
    assert retried_checks["marker"]["ok"] is False  # unmet at base
    assert not stale.exists()  # the retry replaced and removed the stale worktree
    owner_path = (
        comp.workspace.worktrees_dir / ".owners" / "pending-baseline-baseline.json"
    )
    latest, _valid = ManagedWorktrees._latest_record_document(  # noqa: SLF001
        owner_path.read_bytes(), "test owner record"
    )
    assert json.loads(latest)["state"] == "removed"


def test_prepare_refuses_a_dirty_repository(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(tmp_path, "preparer.py", PREPARER_WITH_SIDE_EFFECTS)
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"prepare_resource": "fake-agent"},
    )
    comp = Composition(root)
    conversation = comp.conversations.create()
    comp.conversations.append_message(conversation, "user", "we need marker.txt")
    (root / "README.md").write_text("uncommitted local change\n", encoding="utf-8")

    with pytest.raises(SpecError, match="dirty"):
        prepare_goal(
            workspace=comp.workspace,
            config=comp.config,
            conversations=comp.conversations,
            goals=comp.goals,
            catalog=comp.catalog,
            runner=comp.runner,
            clock=comp.clock,
            name="dirty-goal",
            conversation_id=conversation,
            progress=lambda _line: None,
        )
    assert not comp.goals.exists("dirty-goal")


# -- 3. registered secrets never persist + 4. minimal child environment ------


def test_registered_secret_survives_in_no_durable_byte_or_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "synthetic-credential-4f9a1c77e0b2"
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(
        tmp_path,
        "leaker.py",
        """
token = os.environ.get("FAKE_SECRET_TOKEN", "MISSING")
print("stream sees", token)
sys.stderr.write("stderr sees " + token + "\\n")
output.write_text("reply quoting " + token, encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={
            "fake-agent": _resource(command, secret_env=["FAKE_SECRET_TOKEN"])
        },
        goal={},
        chat_resource="fake-agent",
    )
    monkeypatch.setenv("FAKE_SECRET_TOKEN", secret)
    monkeypatch.chdir(root)
    assert main(["chat", "first message"]) == 0
    # The second exchange forwards the (scrubbed) first reply to the next
    # resource prompt — the known credential must not cross that boundary.
    assert main(["chat", "second message"]) == 0

    secret_bytes = secret.encode("utf-8")
    scanned = 0
    for path in sorted((root / ".dagvane").rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        assert secret_bytes not in path.read_bytes(), f"secret persisted in {path}"
    assert scanned >= 6  # config, conversation, prompts, outputs, stream logs

    comp = Composition(root)
    conversation = comp.conversations.current()
    assert conversation is not None
    messages = comp.conversations.messages(conversation)
    replies = [m for m in messages if m.get("role") == "assistant"]
    assert len(replies) == 2
    assert "[redacted]" in str(replies[0]["text"])
    # The durable prompt artifact of exchange 2 embeds exchange 1 scrubbed.
    second_prompt = Path(str(messages[2]["prompt_path"])).read_text(encoding="utf-8")
    assert secret not in second_prompt
    assert "[redacted]" in second_prompt


def test_child_environment_is_minimal_and_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(
        tmp_path,
        "envdump.py",
        "output.write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n",
    )
    _write_config(
        root,
        resources={
            "fake-agent": _resource(command, env_passthrough=["APPROVED_PLAIN"])
        },
        goal={},
        chat_resource="fake-agent",
    )
    monkeypatch.setenv("UNAPPROVED_SECRET", "must-not-be-inherited-77")
    monkeypatch.setenv("APPROVED_PLAIN", "visible-42")
    monkeypatch.chdir(root)
    assert main(["chat", "dump your environment"]) == 0

    comp = Composition(root)
    conversation = comp.conversations.current()
    assert conversation is not None
    reply = str(comp.conversations.messages(conversation)[-1]["text"])
    child_env = json.loads(reply)
    assert "UNAPPROVED_SECRET" not in child_env
    assert child_env.get("APPROVED_PLAIN") == "visible-42"
    assert "PATH" in child_env


# -- 5. ignored deliverable + 6. mutating verify command ---------------------


def test_ignored_writer_only_deliverable_cannot_achieve(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root, gitignore="ignored-deliverable.txt\n")
    command = _agent_script(
        tmp_path,
        "ignored-writer.py",
        """
Path("ignored-deliverable.txt").write_text("only in the writer tree",
                                           encoding="utf-8")
output.write_text("created the (ignored) deliverable", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    record = _approved_goal(
        comp,
        "ignored-goal",
        checks=[("marker", "test -f ignored-deliverable.txt")],
        max_attempts=3,
        max_failures=3,
    )
    status = _goal_runner(comp).start("ignored-goal")

    # Writer-worktree checks pass, but the immutable verification of the
    # committed candidate cannot see an ignored file: never ACHIEVED, never
    # a tested_sha, and no commit beyond the base.
    assert status is GoalStatus.BLOCKED
    state = _run_state(comp, "ignored-goal")
    assert state["tested_sha"] is None
    assert state["candidate_sha"] == record.contract.base_sha
    assert state["verification"]["ok"] is False
    worktree = Path(state["worktree"])
    assert (worktree / "ignored-deliverable.txt").exists()  # the deception
    assert GitOps.head_sha(worktree) == record.contract.base_sha


def test_verify_command_mutating_tracked_bytes_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(
        tmp_path,
        "writer.py",
        """
Path("m.txt").write_text("deliverable\\n", encoding="utf-8")
output.write_text("created m.txt", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    _approved_goal(
        comp,
        "mutating-goal",
        checks=[("m-exists", "test -f m.txt")],
        verify=["echo mutated >> README.md"],  # exit 0 but mutates tracked bytes
        max_attempts=3,
        max_failures=3,
    )
    status = _goal_runner(comp).start("mutating-goal")

    assert status is GoalStatus.BLOCKED
    state = _run_state(comp, "mutating-goal")
    assert state["tested_sha"] is None
    verification = state["verification"]
    assert verification["ok"] is False
    assert any("README.md" in line for line in verification["tracked_mutations"])
    # The mutated bytes never became part of any commit.
    candidate = state["candidate_sha"]
    worktree = Path(state["worktree"])
    committed_readme = _git(["show", f"{candidate}:README.md"], worktree)
    assert committed_readme == "hello"
    assert (worktree / "README.md").read_text(encoding="utf-8") == "hello\n"


# -- 7. crash reconciliation at both split-write boundaries ------------------


def _marker_goal_composition(tmp_path: Path, goal_name: str) -> Composition:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    _approved_goal(comp, goal_name, checks=[("marker", "test -f marker.txt")])
    return comp


def test_crash_between_start_writes_reconciles_on_resume(tmp_path: Path) -> None:
    comp = _marker_goal_composition(tmp_path, "crash-start")
    original_save = GoalStore.save
    fired = {"done": False}

    def exploding_save(self: GoalStore, expected_name: str, record: GoalRecord) -> None:
        if record.status is GoalStatus.RUNNING and not fired["done"]:
            fired["done"] = True
            raise RuntimeError("injected crash between start writes")
        original_save(self, expected_name, record)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(GoalStore, "save", exploding_save)
        with pytest.raises(RuntimeError, match="injected crash"):
            _goal_runner(comp).start("crash-start")

    # Split-brain: run-state says running, goal.json still approved.
    assert _run_state(comp, "crash-start")["status"] == "running"
    assert _goal_doc(comp, "crash-start")["status"] == "approved"

    # A fresh process resumes: the state is repaired without manual edits
    # and the run completes.
    comp2 = Composition(comp.workspace.root)
    assert _goal_runner(comp2).resume("crash-start") is GoalStatus.ACHIEVED
    assert _goal_doc(comp2, "crash-start")["status"] == "achieved"
    assert _run_state(comp2, "crash-start")["status"] == "achieved"


def test_crash_between_finish_writes_reconciles_without_losing_the_result(
    tmp_path: Path,
) -> None:
    comp = _marker_goal_composition(tmp_path, "crash-finish")
    original_save = GoalStore.save

    def exploding_save(self: GoalStore, expected_name: str, record: GoalRecord) -> None:
        if record.status is GoalStatus.ACHIEVED:
            raise RuntimeError("injected crash between finish writes")
        original_save(self, expected_name, record)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(GoalStore, "save", exploding_save)
        with pytest.raises(RuntimeError, match="injected crash"):
            _goal_runner(comp).start("crash-finish")

    # Split-brain: run-state holds the terminal result, goal.json lags.
    state = _run_state(comp, "crash-finish")
    assert state["status"] == "achieved"
    assert _goal_doc(comp, "crash-finish")["status"] == "running"
    implement_calls = int((tmp_path / "implement-calls").read_text())

    # Resume replays the finish from durable run-state: the terminal result
    # is preserved and no new agent work happens.
    comp2 = Composition(comp.workspace.root)
    assert _goal_runner(comp2).resume("crash-finish") is GoalStatus.ACHIEVED
    doc = _goal_doc(comp2, "crash-finish")
    assert doc["status"] == "achieved"
    assert doc["evidence"]["tested_sha"] == state["tested_sha"]
    assert int((tmp_path / "implement-calls").read_text()) == implement_calls


def test_cancelled_goal_with_running_state_reconciles(tmp_path: Path) -> None:
    comp = _marker_goal_composition(tmp_path, "crash-cancel")
    record = comp.goals.load("crash-cancel")
    state = RunState(
        run_id="goalrun-fabricated",
        goal_name="crash-cancel",
        base_sha=record.contract.base_sha,
        status="running",
        started_ts=comp.clock.now_iso(),
    )
    atomic_write_json(
        comp.goals.goal_dir("crash-cancel") / "run-state.json", state.to_doc()
    )
    record.status = GoalStatus.CANCELLED
    comp.goals.save("crash-cancel", record)

    assert _goal_runner(comp).resume("crash-cancel") is GoalStatus.CANCELLED
    assert _run_state(comp, "crash-cancel")["status"] == "cancelled"


# -- 8. one-writer lease under concurrency -----------------------------------


def test_two_concurrent_resumes_admit_only_one_writer(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(
        tmp_path,
        "blocking-writer.py",
        """
(ctrl / "writer-started").write_text(str(os.getpid()), encoding="utf-8")
deadline = time.time() + 30
while not (ctrl / "release").exists() and time.time() < deadline:
    time.sleep(0.02)
Path("marker.txt").write_text("done\\n", encoding="utf-8")
output.write_text("done", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    _approved_goal(comp, "one-writer", checks=[("marker", "test -f marker.txt")])

    results: dict[str, GoalStatus] = {}

    def first_entrant() -> None:
        results["first"] = _goal_runner(Composition(root)).start("one-writer")

    thread = threading.Thread(target=first_entrant, daemon=True)
    thread.start()
    _wait_for(tmp_path / "writer-started")

    # While the first writer is mid-implementation, a second process-alike
    # entrant must be refused — not admitted alongside.
    with pytest.raises(SpecError, match="another process|one writer"):
        _goal_runner(Composition(root)).resume("one-writer")

    (tmp_path / "release").write_text("", encoding="utf-8")
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert results["first"] is GoalStatus.ACHIEVED


def test_lease_refuses_second_holder_and_releases_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "lease.lock"
    first = GoalLease(path, allowed_root=tmp_path)
    second = GoalLease(path, allowed_root=tmp_path)
    first.acquire(owner="test-first")
    with pytest.raises(SpecError, match="another process"):
        second.acquire(owner="test-second")
    first.release()
    second.acquire(owner="test-second")  # freed lease is reacquirable
    second.release()


# -- 9. cancellation stops and reaps the active writer -----------------------


def test_owner_cancel_kills_writer_tree_and_discards_post_cancel_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    command = _agent_script(
        tmp_path,
        "cancellable-writer.py",
        """
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
(ctrl / "pids").write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
Path("pre-cancel.txt").write_text("before\\n", encoding="utf-8")
(ctrl / "writer-started").write_text("1", encoding="utf-8")
deadline = time.time() + 300
while not (ctrl / "release").exists() and time.time() < deadline:
    time.sleep(0.02)
Path("post-cancel.txt").write_text("after\\n", encoding="utf-8")
output.write_text("finished anyway", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={"fake-agent": _resource(command)},
        goal={"implement_resource": "fake-agent", "review_policy": "never"},
    )
    comp = Composition(root)
    record = _approved_goal(
        comp, "cancel-goal", checks=[("done", "test -f post-cancel.txt")]
    )

    results: dict[str, GoalStatus] = {}

    def run_goal() -> None:
        results["status"] = _goal_runner(Composition(root)).start("cancel-goal")

    thread = threading.Thread(target=run_goal, daemon=True)
    thread.start()
    _wait_for(tmp_path / "writer-started")
    writer_pid, child_pid = (
        int(part) for part in (tmp_path / "pids").read_text().split()
    )

    monkeypatch.chdir(root)
    assert main(["goal", "cancel", "cancel-goal"]) == 0

    thread.join(timeout=60)
    assert not thread.is_alive()
    assert results["status"] is GoalStatus.CANCELLED

    # The whole fake process tree is gone — writer and its spawned child.
    _wait_pid_gone(writer_pid)
    _wait_pid_gone(child_pid)

    # No post-cancel effect exists, let alone becomes an accepted candidate.
    state = _run_state(comp, "cancel-goal")
    worktree = Path(state["worktree"])
    assert not (worktree / "post-cancel.txt").exists()
    assert GitOps.head_sha(worktree) == record.contract.base_sha
    # The managed candidate generation is durably bound to the unchanged base
    # SHA before any writer effect; cancellation retains that exact anchor.
    assert state["candidate_sha"] == record.contract.base_sha
    assert _goal_doc(comp, "cancel-goal")["status"] == "cancelled"
    # Releasing the barrier afterwards changes nothing: the writer is dead.
    (tmp_path / "release").write_text("", encoding="utf-8")
    time.sleep(0.2)
    assert not (worktree / "post-cancel.txt").exists()


# -- 10. blocking findings are durable; no-op remediation is no progress -----


def test_blocker_plus_noop_remediation_cannot_become_achieved(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(
        tmp_path,
        "noop-remediator.py",
        """
if "Independent review findings" in prompt:
    bump("remediation-calls")  # deliberate no-op: no file, no commit
    output.write_text("nothing changed", encoding="utf-8")
else:
    Path("marker.txt").write_text("v1\\n", encoding="utf-8")
    output.write_text("created the marker", encoding="utf-8")
""",
    )
    reviewer = _agent_script(
        tmp_path,
        "one-shot-blocker.py",
        """
call = bump("review-calls")
if call == 1:
    findings = [{"severity": "BLOCKER", "description": "must fix", "file": ""}]
else:
    findings = []  # a stochastic later review would come back clean
output.write_text(json.dumps({"findings": findings}), encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={
            "fake-writer": _resource(writer),
            "fake-reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-reviewer",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    _approved_goal(
        comp,
        "durable-blocker",
        checks=[("marker", "test -f marker.txt")],
        max_attempts=5,
        max_failures=2,
    )
    status = _goal_runner(comp).start("durable-blocker")

    # The unchanged candidate was reviewed exactly once: the recorded BLOCKER
    # stays durable and the empty second review never happened.
    assert status is GoalStatus.BLOCKED
    assert int((tmp_path / "review-calls").read_text()) == 1
    assert int((tmp_path / "remediation-calls").read_text()) >= 2
    state = _run_state(comp, "durable-blocker")
    reviews = state["reviews"]
    assert len(reviews) == 1
    assert reviews[0]["valid"] is True
    assert reviews[0]["findings"][0]["severity"] == "BLOCKER"
    assert reviews[0]["candidate_sha"] == state["candidate_sha"]
    assert state["review_passed_for"] is None
    assert _goal_doc(comp, "durable-blocker")["status"] == "blocked"
    # The finding also survives into the terminal evidence.
    evidence = _goal_doc(comp, "durable-blocker")["evidence"]
    assert evidence["reviews"][0]["findings"][0]["severity"] == "BLOCKER"


def test_malformed_reviewer_output_is_infrastructure_failure_not_a_code_defect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    reviewer = _agent_script(
        tmp_path,
        "broken-reviewer.py",
        'output.write_text("utterly not json", encoding="utf-8")\n',
    )
    _write_config(
        root,
        resources={
            "fake-writer": _resource(writer),
            "fake-reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-reviewer",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    _approved_goal(comp, "broken-review", checks=[("marker", "test -f marker.txt")])
    status = _goal_runner(comp).start("broken-review")

    assert status is GoalStatus.FAILED
    doc = _goal_doc(comp, "broken-review")
    assert "reviewer/infrastructure" in doc["evidence"]["reason"]
    state = _run_state(comp, "broken-review")
    # No fabricated finding was recorded against the candidate, and the
    # writer was never asked to "fix" the reviewer.
    assert state["review_findings"] == []
    assert all(entry["valid"] is False for entry in state["reviews"])
    assert int((tmp_path / "implement-calls").read_text()) == 1


# -- 11. review evidence is bound to the exact candidate SHA -----------------


def test_review_is_bound_to_the_exact_candidate_sha(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    reviewer = _agent_script(
        tmp_path,
        "clean-reviewer.py",
        "output.write_text(json.dumps({'findings': []}), encoding='utf-8')\n",
    )
    _write_config(
        root,
        resources={
            "fake-writer": _resource(writer),
            "fake-reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-reviewer",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    record = _approved_goal(
        comp, "bound-review", checks=[("marker", "test -f marker.txt")]
    )
    status = _goal_runner(comp).start("bound-review")

    assert status is GoalStatus.ACHIEVED
    state = _run_state(comp, "bound-review")
    assert state["contributor_resource_ids"] == ["fake-writer"]
    candidate = state["candidate_sha"]
    assert state["tested_sha"] == candidate
    entry = state["reviews"][-1]
    assert entry["candidate_sha"] == candidate
    assert entry["base_sha"] == record.contract.base_sha
    assert entry["head_before"] == candidate
    assert entry["head_after"] == candidate
    assert entry["head_verified"] is True

    # The recorded diff hash matches the full untruncated diff of exactly
    # base..candidate.
    worktree = Path(state["worktree"])
    diff_bytes = subprocess.run(
        ["git", "diff", f"{record.contract.base_sha}..{candidate}"],
        cwd=worktree,
        check=True,
        capture_output=True,
    ).stdout
    assert entry["diff_sha256"] == hashlib.sha256(diff_bytes).hexdigest()

    # The reviewer prompt itself carried the exact SHAs and the diff hash.
    review_prompts = [
        path.read_text(encoding="utf-8")
        for path in (root / ".dagvane" / "agent-runs").rglob("prompt.md")
        if "independent reviewer" in path.read_text(encoding="utf-8")
    ]
    assert len(review_prompts) == 1
    assert candidate in review_prompts[0]
    assert record.contract.base_sha in review_prompts[0]
    assert str(entry["diff_sha256"]) in review_prompts[0]


def test_pending_writer_is_durable_before_commit_state_and_excluded_on_resume(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    reviewer = _agent_script(
        tmp_path,
        "reviewer.py",
        "output.write_text(json.dumps({'findings': []}), encoding='utf-8')\n",
    )
    _write_config(
        root,
        resources={
            "fake-writer": _resource(writer),
            "fake-reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-reviewer",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    record = _approved_goal(comp, "pending-commit", checks=[("marker", "test -f marker.txt")])
    runner = _goal_runner(comp)
    original_save = runner._save_state

    def crash_after_commit(record_arg: GoalRecord, state_arg: RunState) -> None:
        if (
            state_arg.pending_writer_resource_id is None
            and state_arg.contributor_resource_ids == ["fake-writer"]
            and state_arg.candidate_sha != record_arg.contract.base_sha
        ):
            raise KeyboardInterrupt("crash after managed commit")
        original_save(record_arg, state_arg)

    runner._save_state = crash_after_commit  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt, match="crash after managed commit"):
        runner.start("pending-commit")

    crashed = _run_state(comp, "pending-commit")
    assert crashed["pending_writer_resource_id"] == "fake-writer"
    assert crashed["contributor_resource_ids"] == []

    comp2 = Composition(root)
    assert _goal_runner(comp2).resume("pending-commit") is GoalStatus.ACHIEVED
    state = _run_state(comp2, "pending-commit")
    assert state["pending_writer_resource_id"] is None
    assert state["contributor_resource_ids"] == ["fake-writer"]
    assert state["reviews"][-1]["resource"] == "fake-reviewer"
    assert state["reviews"][-1]["valid"] is True
    assert state["candidate_sha"] != record.contract.base_sha


def test_pending_dirty_bytes_are_discarded_before_resume_attempt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(
        tmp_path,
        "retrying-writer.py",
        """
call = bump("writer-calls")
if call == 1:
    Path("partial.txt").write_text("discard me\\n", encoding="utf-8")
else:
    Path("marker.txt").write_text("complete\\n", encoding="utf-8")
output.write_text("writer output", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={"fake-writer": _resource(writer)},
        goal={
            "implement_resource": "fake-writer",
            "review_policy": "never",
        },
    )
    comp = Composition(root)
    _approved_goal(comp, "pending-dirty", checks=[("marker", "test -f marker.txt")])
    runner = _goal_runner(comp)
    original_commit = runner._commit_candidate

    def crash_with_dirty_bytes(
        record_arg: GoalRecord,
        state_arg: RunState,
        use: Any,
        message: str,
    ) -> tuple[str, bool]:
        if not GitOps.is_clean(use.path):
            raise KeyboardInterrupt("crash after writer bytes")
        return original_commit(record_arg, state_arg, use, message)

    runner._commit_candidate = crash_with_dirty_bytes  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt, match="crash after writer bytes"):
        runner.start("pending-dirty")

    comp2 = Composition(root)
    assert _goal_runner(comp2).resume("pending-dirty") is GoalStatus.ACHIEVED
    state = _run_state(comp2, "pending-dirty")
    assert state["contributor_resource_ids"] == ["fake-writer"]
    worktree = Path(state["worktree"])
    assert not (worktree / "partial.txt").exists()
    assert (worktree / "marker.txt").read_text(encoding="utf-8") == "complete\n"


@pytest.mark.parametrize("invalid_id", ["", None])
def test_empty_or_null_writer_id_is_rejected_before_pending_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_id: Any,
) -> None:
    comp = _marker_goal_composition(tmp_path, "invalid-writer")
    real_route = route_task
    calls = {"save": 0}
    runner = _goal_runner(comp)
    original_save = runner._save_state

    def counting_save(record_arg: GoalRecord, state_arg: RunState) -> None:
        calls["save"] += 1
        original_save(record_arg, state_arg)

    runner._save_state = counting_save  # type: ignore[assignment]

    def invalid_route(catalog: Any, task_kind: str, **kwargs: Any) -> RoutingDecision:
        if task_kind in ("implement", "remediate"):
            return RoutingDecision(
                resource=ResourceSpec(
                    resource_id=invalid_id,
                    kind="external_agent",
                    runtime="command",
                    tier="STANDARD",
                ),
                tier="STANDARD",
                reason="invalid test writer",
            )
        return real_route(catalog, task_kind, **kwargs)

    monkeypatch.setattr(autodev_module, "route_task", invalid_route)
    with pytest.raises(SpecError, match="writer resource_id"):
        runner.start("invalid-writer")

    state = _run_state(comp, "invalid-writer")
    assert state["pending_writer_resource_id"] is None
    assert state["agent_calls"] == 0
    assert calls["save"] > 0


def test_two_contributors_make_either_reviewer_fatal_without_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer_a = _agent_script(
        tmp_path,
        "writer-a.py",
        'Path("irrelevant-a.txt").write_text("a\\n", encoding="utf-8")\n'
        'output.write_text("a", encoding="utf-8")\n',
    )
    writer_b = _agent_script(
        tmp_path,
        "writer-b.py",
        'Path("marker.txt").write_text("b\\n", encoding="utf-8")\n'
        'output.write_text("b", encoding="utf-8")\n',
    )
    reviewer = _agent_script(
        tmp_path,
        "reviewer.py",
        "output.write_text(json.dumps({'findings': []}), encoding='utf-8')\n",
    )
    _write_config(
        root,
        resources={
            "writer-a": _resource(writer_a),
            "writer-b": _resource(writer_b),
            "reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "review_resource": "writer-a",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    _approved_goal(
        comp,
        "two-contributors",
        checks=[("marker", "test -f marker.txt")],
        max_attempts=3,
    )
    real_route = route_task

    def alternating_route(catalog: Any, task_kind: str, **kwargs: Any) -> Any:
        if task_kind in ("implement", "remediate"):
            resource_id = "writer-a" if kwargs["attempt"] == 1 else "writer-b"
            return RoutingDecision(
                resource=catalog.get(resource_id),
                tier=catalog.get(resource_id).tier,
                reason=f"test route {resource_id}",
            )
        return real_route(catalog, task_kind, **kwargs)

    monkeypatch.setattr(autodev_module, "route_task", alternating_route)
    status = _goal_runner(comp).start("two-contributors")

    assert status is GoalStatus.FAILED
    state = _run_state(comp, "two-contributors")
    assert state["contributor_resource_ids"] == ["writer-a", "writer-b"]
    assert state["reviews"] == []
    assert "contributor set" in str(_goal_doc(comp, "two-contributors")["evidence"]["reason"])

    # The same durable set rejects the other contributor as well, without
    # creating a review checkout or append-only review entry.
    comp.config.set("goal.review_resource", '"writer-b"')
    record = comp.goals.load("two-contributors")
    state_obj = RunState.from_doc(state)
    kind, _blocking, reason = _goal_runner(comp)._review_candidate(  # noqa: SLF001
        record, state_obj, str(state["candidate_sha"])
    )
    assert kind == "fatal"
    assert "contributor set" in reason


def test_reviewer_mutating_the_pinned_checkout_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    reviewer = _agent_script(
        tmp_path,
        "tampering-reviewer.py",
        """
subprocess.run(
    ["git", "commit", "--allow-empty", "-m", "tamper", "--no-verify"],
    check=True, capture_output=True,
)
output.write_text(json.dumps({"findings": []}), encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={
            "fake-writer": _resource(writer),
            "fake-reviewer": _resource(reviewer, tier="STRONG"),
        },
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-reviewer",
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    _approved_goal(comp, "tampered-review", checks=[("marker", "test -f marker.txt")])
    status = _goal_runner(comp).start("tampered-review")

    # A review whose checkout HEAD moved is never attributed to the
    # candidate: it is an infrastructure failure and the run fails closed.
    assert status is GoalStatus.FAILED
    state = _run_state(comp, "tampered-review")
    assert all(entry["valid"] is False for entry in state["reviews"])
    assert all("HEAD moved" in str(entry["error"]) for entry in state["reviews"])
    assert state["review_passed_for"] is None


def test_reviewer_identical_to_writer_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    writer = _agent_script(tmp_path, "writer.py", MARKER_WRITER)
    _write_config(
        root,
        resources={"fake-writer": _resource(writer)},
        goal={
            "implement_resource": "fake-writer",
            "review_resource": "fake-writer",  # same identity as the writer
            "review_policy": "always",
        },
    )
    comp = Composition(root)
    _approved_goal(comp, "self-review", checks=[("marker", "test -f marker.txt")])
    status = _goal_runner(comp).start("self-review")
    assert status is GoalStatus.FAILED
    doc = _goal_doc(comp, "self-review")
    assert "distinct" in doc["evidence"]["reason"]


# -- 12. repeated unsuccessful attempts escalate routing ----------------------


def test_repeated_failures_escalate_even_with_irrelevant_commits(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _init_repo(root)
    junk_writer = _agent_script(
        tmp_path,
        "junk-writer.py",
        """
call = bump("junk-calls")
Path(f"junk-{call}.txt").write_text("irrelevant\\n", encoding="utf-8")
output.write_text("committed something irrelevant", encoding="utf-8")
""",
    )
    _write_config(
        root,
        resources={
            "fake-std": _resource(junk_writer, tier="STANDARD"),
            "fake-strong": _resource(junk_writer, tier="STRONG"),
            "fake-crit": _resource(junk_writer, tier="CRITICAL"),
        },
        goal={"implement_resource": "fake-std", "review_policy": "never"},
    )
    comp = Composition(root)
    record = _approved_goal(
        comp,
        "escalating-goal",
        checks=[("impossible", "test -f never-created.txt")],
        max_attempts=8,
        max_failures=5,
    )
    status = _goal_runner(comp).start("escalating-goal")

    assert status is GoalStatus.BLOCKED
    state = _run_state(comp, "escalating-goal")
    # Every attempt produced a commit, yet none was progress: the ladder
    # escalated through the stronger configured tiers anyway.
    assert state["candidate_sha"] != record.contract.base_sha
    routing = state["routing_log"]
    assert any("tier STRONG" in line for line in routing), routing
    assert any("tier CRITICAL" in line for line in routing), routing
    assert state["consecutive_failures"] >= 5
