"""Autonomous Developer MVP — offline integration tests.

External agents are faked with a local Python script driven through the
generic ``command`` runtime, so the whole chat → prepare → approve → run →
crash → resume workflow is exercised without any network or real agent CLI.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from dagvane.application.resources import ResourceCatalog, route_task
from dagvane.cli import main
from dagvane.domain.models import SpecError

# The fake agent: reads the prompt, decides which stage it is serving, and
# behaves accordingly. Counters persist next to the script so a test can make
# call N behave differently from call N+1.
FAKE_AGENT = r'''
import json
import os
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text(encoding="utf-8")
output = Path(sys.argv[2])
state_dir = Path(__file__).resolve().parent

def bump(name):
    counter = state_dir / name
    value = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(value))
    return value

if "preparing a frozen Goal Contract" in prompt:
    contract = {
        "objective": "Create the marker file proving autonomous implementation.",
        "must_have": ["marker file exists"],
        "non_goals": ["anything else"],
        "checks": [
            {
                "check_id": "marker-exists",
                "description": "the marker file must exist",
                "command": "test -f dagvane-marker.txt",
            }
        ],
        "verify_commands": ["true"],
    }
    output.write_text(json.dumps(contract), encoding="utf-8")
elif "single implementation writer" in prompt:
    call = bump("implement-calls")
    # Attempt-numbered content: a remediation attempt must be able to produce
    # a genuinely new candidate commit (same bytes would commit nothing).
    Path("dagvane-marker.txt").write_text(
        f"made by fake agent, attempt {call}\n", encoding="utf-8"
    )
    output.write_text("Created the marker file.", encoding="utf-8")
elif "independent reviewer" in prompt:
    call = bump("review-calls")
    if call == 1 and os.environ.get("FAKE_REVIEW_BLOCKS"):
        findings = [{"severity": "BLOCKER", "description": "must fix", "file": ""}]
    else:
        findings = []
    output.write_text(json.dumps({"findings": findings}), encoding="utf-8")
else:
    output.write_text("scripted chat reply", encoding="utf-8")
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git workspace whose config routes every resource to the fake agent."""
    root = tmp_path / "project"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "user.email", "test@example.test"], root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "init"], root)

    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    command = [sys.executable, str(script), "{prompt_file}", "{output_file}"]
    config = {
        "chat": {"resource": "fake-agent"},
        "goal": {
            "implement_resource": "fake-agent",
            "review_resource": "fake-reviewer",
            "prepare_resource": "fake-agent",
            "agent_timeout_seconds": 60,
        },
        "router": {"local_enabled": False},
        "resources": {
            # Workspace config merges OVER engine defaults: disable every
            # default (real) resource so no routing decision can escape the
            # offline fakes.
            **{
                resource_id: {"enabled": False}
                for resource_id in (
                    "codex-cheap",
                    "codex-standard",
                    "codex-strong",
                    "codex-critical",
                    "ollama-local",
                    "agy-review",
                )
            },
            "fake-agent": {
                "kind": "external_agent",
                "runtime": "command",
                "tier": "STANDARD",
                "command": command,
            },
            "fake-reviewer": {
                "kind": "external_agent",
                "runtime": "command",
                "tier": "STRONG",
                "command": command,
                # The child no longer inherits the host environment; the
                # test toggle must be granted explicitly.
                "env_passthrough": ["FAKE_REVIEW_BLOCKS"],
            },
        },
    }
    dagvane_dir = root / ".dagvane"
    dagvane_dir.mkdir()
    from dagvane.workspace.config import render_toml

    (dagvane_dir / "config.toml").write_text(
        render_toml(config) + "\n", encoding="utf-8"
    )
    monkeypatch.chdir(root)
    return root


def test_config_get_set_list_roundtrip(workspace: Path, capsys: Any) -> None:
    assert main(["config", "get", "goal.review_policy"]) == 0
    assert capsys.readouterr().out.strip() == '"substantial"'
    assert main(["config", "set", "goal.review_policy", '"never"']) == 0
    capsys.readouterr()
    assert main(["config", "get", "goal.review_policy"]) == 0
    assert capsys.readouterr().out.strip() == '"never"'
    assert main(["config", "list"]) == 0
    listing = capsys.readouterr().out
    assert 'goal.review_policy = "never"' in listing
    assert "resources.fake-agent.kind" in listing


def test_chat_persists_and_continues_conversations(workspace: Path, capsys: Any) -> None:
    assert main(["chat", "analyze the repository"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "scripted chat reply"

    assert main(["conversations", "current"]) == 0
    conversation_id = capsys.readouterr().out.strip()

    # A second chat continues the same conversation and carries history.
    assert main(["chat", "now focus on authentication"]) == 0
    capsys.readouterr()
    assert main(["conversations", "show", conversation_id]) == 0
    transcript = capsys.readouterr().out
    assert "analyze the repository" in transcript
    assert "now focus on authentication" in transcript
    assert transcript.count("scripted chat reply") == 2

    # The second exchange's durable prompt artifact embeds the first exchange:
    # Dagvane-owned history, no provider session needed.
    messages = (
        workspace
        / ".dagvane"
        / "conversations"
        / conversation_id
        / "messages.jsonl"
    ).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in messages.splitlines()]
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    prompt_path = Path(str(rows[2]["prompt_path"]))
    prompt = prompt_path.read_text(encoding="utf-8")
    assert "analyze the repository" in prompt
    assert "scripted chat reply" in prompt

    assert main(["chat", "--new", "fresh start"]) == 0
    capsys.readouterr()
    assert main(["conversations", "current"]) == 0
    assert capsys.readouterr().out.strip() != conversation_id
    assert main(["conversations", "use", conversation_id]) == 0
    capsys.readouterr()
    assert main(["conversations", "current"]) == 0
    assert capsys.readouterr().out.strip() == conversation_id


def test_router_ladder_is_deterministic() -> None:
    catalog = ResourceCatalog(
        {
            "cheap": {"kind": "external_agent", "runtime": "codex", "tier": "CHEAP"},
            "standard": {
                "kind": "external_agent",
                "runtime": "codex",
                "tier": "STANDARD",
            },
            "strong": {"kind": "external_agent", "runtime": "codex", "tier": "STRONG"},
            "critical": {
                "kind": "external_agent",
                "runtime": "codex",
                "tier": "CRITICAL",
            },
        }
    )
    assert route_task(catalog, "analyze").resource.resource_id == "cheap"
    assert route_task(catalog, "implement").tier == "STANDARD"
    # attempt 2: same tier (change strategy); attempt 3: escalate one tier.
    assert route_task(catalog, "implement", attempt=2).tier == "STANDARD"
    assert route_task(catalog, "implement", attempt=3).tier == "STRONG"
    assert route_task(catalog, "implement", attempt=4).tier == "CRITICAL"
    assert route_task(catalog, "review").tier == "STRONG"
    assert route_task(catalog, "review", risk="high").tier == "CRITICAL"
    # LOCAL degrades to CHEAP when no local model is available.
    assert route_task(catalog, "classify").tier == "CHEAP"
    catalog.mark_local_available(True)  # still no LOCAL resource → CHEAP
    assert route_task(catalog, "classify").resource.resource_id == "cheap"
    reason = route_task(catalog, "implement", attempt=3).reason
    assert "attempt 3" in reason


def _prepare_and_approve(capsys: Any) -> None:
    assert main(["chat", "we need a marker file"]) == 0
    capsys.readouterr()
    assert main(["goal", "prepare", "--name", "marker-goal"]) == 0
    shown = capsys.readouterr()
    doc = json.loads(shown.out)
    assert doc["status"] == "prepared"
    assert doc["contract"]["checks"][0]["check_id"] == "marker-exists"
    # Draft-only preparation: no proposed command has run yet, so the
    # baseline is honestly labeled pending.
    assert doc["baseline"]["status"] == "pending"
    assert "checks" not in doc["baseline"]
    assert main(["goal", "approve", "marker-goal"]) == 0
    capsys.readouterr()
    # Baseline evidence is collected after approval, at the exact base SHA:
    # the check is unmet there.
    assert main(["goal", "show", "marker-goal"]) == 0
    shown_doc = json.loads(capsys.readouterr().out)
    assert shown_doc["baseline"]["status"] == "completed"
    assert shown_doc["baseline"]["base_sha"] == shown_doc["contract"]["base_sha"]
    assert shown_doc["baseline"]["checks"]["marker-exists"]["ok"] is False


def test_goal_prepare_approve_run_achieves(workspace: Path, capsys: Any) -> None:
    _prepare_and_approve(capsys)
    assert main(["goal", "run", "marker-goal"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "achieved"

    assert main(["goal", "show", "marker-goal"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "achieved"
    run = doc["run"]
    assert run["candidate_sha"]
    assert run["tested_sha"] == run["candidate_sha"]
    worktree = Path(run["worktree"])
    assert (worktree / "dagvane-marker.txt").exists()
    # The candidate SHA is a real commit in the worktree, not the base.
    assert run["candidate_sha"] != doc["contract"]["base_sha"]
    # Evidence records the deterministic check outcomes.
    assert doc["evidence"]["check_results"]["marker-exists"] is True


def test_goal_run_survives_crash_and_resumes(workspace: Path, capsys: Any) -> None:
    """Kill the process mid-run (simulated KeyboardInterrupt inside the agent
    stage); a fresh process resumes from durable state and achieves the goal
    without any provider-native session."""
    from dagvane.cli_workspace import Composition, _goal_runner

    _prepare_and_approve(capsys)

    comp = Composition(workspace)
    runner = _goal_runner(comp)
    real_run = comp.runner.run
    calls = {"n": 0}

    def crashing_run(invocation: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt  # the process dies mid-implementation
        return real_run(invocation)

    comp.runner.run = crashing_run  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        runner.start("marker-goal")

    # Durable state says the run is still active; the goal is RUNNING.
    state_doc = json.loads(
        (workspace / ".dagvane" / "goals" / "marker-goal" / "run-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state_doc["status"] == "running"

    # A brand-new composition (new process) resumes and finishes.
    assert main(["goal", "resume", "marker-goal"]) == 0
    assert capsys.readouterr().out.strip() == "achieved"


def test_goal_review_blocker_triggers_remediation(
    workspace: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REVIEW_BLOCKS", "1")
    _prepare_and_approve(capsys)
    assert main(["config", "set", "goal.review_policy", '"always"']) == 0
    capsys.readouterr()
    assert main(["goal", "run", "marker-goal"]) == 0
    assert capsys.readouterr().out.strip() == "achieved"
    assert main(["goal", "show", "marker-goal"]) == 0
    doc = json.loads(capsys.readouterr().out)
    # Review passed for the final candidate after one remediation round.
    assert doc["run"]["review_passed_for"] == doc["run"]["candidate_sha"]


def test_contract_is_frozen_after_approval(workspace: Path, capsys: Any) -> None:
    _prepare_and_approve(capsys)
    goal_path = workspace / ".dagvane" / "goals" / "marker-goal" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["objective"] = "silently weakened objective"
    goal_path.write_text(json.dumps(doc), encoding="utf-8")
    from dagvane.cli_workspace import Composition

    comp = Composition(workspace)
    with pytest.raises(SpecError, match="CONTRACT_AMENDMENT_REQUIRED"):
        comp.goals.load("marker-goal")


def test_goal_cancel_blocks_resume(workspace: Path, capsys: Any) -> None:
    _prepare_and_approve(capsys)
    assert main(["goal", "cancel", "marker-goal"]) == 0
    capsys.readouterr()
    # A cancelled goal cannot be resumed: usage error (exit 2).
    assert main(["goal", "resume", "marker-goal"]) == 2
    assert main(["goal", "show", "marker-goal"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "cancelled"
