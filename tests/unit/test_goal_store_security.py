"""Security regressions binding Goal identity to storage path.

Every Goal name must survive `validate_filesystem_id` before it ever touches
the filesystem, and any on-disk object that does not match the identity it
was requested under (symlink, mismatched contract name, invalid directory
name) must fail closed with ``StorageError``/``SpecError`` rather than being
silently treated as absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dagvane.application.goals import (
    AcceptanceCheck,
    GoalContract,
    GoalLimits,
    GoalRecord,
    GoalStatus,
    GoalStore,
)
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.runtime import FixedClock
from dagvane.workspace.paths import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    workspace = Workspace(root)
    workspace.ensure()
    return workspace


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


def _store(tmp_path: Path) -> tuple[GoalStore, Workspace, FixedClock]:
    workspace = _workspace(tmp_path)
    clock = _clock()
    return GoalStore(workspace, clock), workspace, clock


def _contract(name: str, *, base_sha: str = "a" * 40) -> GoalContract:
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


def _record(name: str, clock: FixedClock) -> GoalRecord:
    now = clock.now_iso()
    return GoalRecord(
        contract=_contract(name),
        status=GoalStatus.DRAFT,
        created_ts=now,
        updated_ts=now,
        contract_sha256=None,
    )


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


def _assert_no_tmp_or_log(goal_dir: Path) -> None:
    if not goal_dir.exists():
        return
    for entry in goal_dir.iterdir():
        assert not entry.name.endswith(".tmp")
    assert not (goal_dir / "log.jsonl").exists()


# -- invalid names, before any effect ---------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "/etc/passwd",
        "../escape",
        "a/b",
        "a\\b",
        "café",
        "name\x00null",
        "name\nnewline",
        "",
    ],
)
def test_invalid_name_rejected_before_effect(tmp_path: Path, bad_name: str) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    sentinels = _sentinels(tmp_path, workspace)
    with pytest.raises(SpecError):
        store.goal_dir(bad_name)
    with pytest.raises(SpecError):
        store.exists(bad_name)
    with pytest.raises(SpecError):
        store.load(bad_name)
    with pytest.raises(SpecError):
        store.log_event(bad_name, {"event": "x"})
    assert list(workspace.goals_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_invalid_non_string_name_rejected(tmp_path: Path) -> None:
    store, _workspace_, _clock_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.goal_dir(123)  # type: ignore[arg-type]


# -- save: expected-name mismatch fails before timestamp/I/O ----------------


def test_save_name_mismatch_rejected_before_write(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    updated_before = record.updated_ts
    sentinels = _sentinels(tmp_path, workspace)
    with pytest.raises(SpecError):
        store.save("goal-b", record)
    assert record.updated_ts == updated_before
    assert list(workspace.goals_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store, _workspace_, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    loaded = store.load("goal-a")
    assert loaded.contract.name == "goal-a"
    assert store.exists("goal-a") is True
    assert store.list_names() == ["goal-a"]
    store.log_event("goal-a", {"event": "did-thing"})


# -- truly absent goal is ordinary false/unknown -----------------------------


def test_truly_absent_goal_is_ordinary_false(tmp_path: Path) -> None:
    store, _workspace_, _clock_ = _store(tmp_path)
    assert store.exists("nope") is False
    with pytest.raises(SpecError):
        store.load("nope")
    assert store.list_names() == []


# -- on-disk contract name valid-but-different / wrong type -----------------


def test_load_rejects_mismatched_valid_contract_name(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = "goal-other"
    goal_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SpecError):
        store.load("goal-a")
    with pytest.raises(SpecError):
        store.list_names()
    assert goal_path.read_text(encoding="utf-8") == json.dumps(doc)


def test_load_rejects_wrong_type_contract_name(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = 12345
    goal_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SpecError):
        store.load("goal-a")
    with pytest.raises(SpecError):
        store.list_names()
    assert goal_path.read_text(encoding="utf-8") == json.dumps(doc)


# -- symlink sentinels: dir, goal.json, log.jsonl ----------------------------


def test_symlinked_goal_directory_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    sentinels = _sentinels(tmp_path, workspace)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (workspace.goals_dir / "goal-a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        store.exists("goal-a")
    with pytest.raises(StorageError):
        store.load("goal-a")
    with pytest.raises(StorageError):
        store.list_names()
    _assert_sentinels_untouched(sentinels)
    assert list(outside.iterdir()) == []


def test_symlinked_goal_json_fails_closed(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_dir = workspace.goals_dir / "goal-a"
    goal_json = goal_dir / "goal.json"
    real_bytes = goal_json.read_bytes()
    goal_json.unlink()
    target = tmp_path / "outside-goal.json"
    target.write_bytes(b'{"contract": {"name": "goal-a"}}')
    goal_json.symlink_to(target)
    with pytest.raises(StorageError):
        store.exists("goal-a")
    with pytest.raises(StorageError):
        store.load("goal-a")
    with pytest.raises(StorageError):
        store.list_names()
    assert target.read_bytes() == b'{"contract": {"name": "goal-a"}}'
    assert goal_json.is_symlink()
    del real_bytes


def test_symlinked_log_jsonl_fails_closed(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_dir = workspace.goals_dir / "goal-a"
    log_path = goal_dir / "log.jsonl"
    target = tmp_path / "outside-log.jsonl"
    target.write_bytes(b"tampered\n")
    log_path.symlink_to(target)
    with pytest.raises(StorageError):
        store.log_event("goal-a", {"event": "x"})
    assert target.read_bytes() == b"tampered\n"
    assert log_path.is_symlink()


# -- invalid/mismatched objects make list fail closed ------------------------


def test_list_names_fails_closed_on_invalid_directory_name(tmp_path: Path) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    bad_dir = workspace.goals_dir / "..bad"
    bad_dir.mkdir()
    (bad_dir / "goal.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SpecError):
        store.list_names()


def test_list_names_fails_closed_on_dir_with_no_goal_json(tmp_path: Path) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    empty_dir = workspace.goals_dir / "goal-empty"
    empty_dir.mkdir()
    with pytest.raises(StorageError):
        store.list_names()


def test_list_names_rejects_stray_file_in_goals_dir(tmp_path: Path) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    (workspace.goals_dir / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(StorageError):
        store.list_names()


# -- negative paths never leave temp/log artifacts ---------------------------


def test_negative_save_leaves_no_temp_artifact(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    with pytest.raises(SpecError):
        store.save("goal-b", record)
    goal_dir = workspace.goals_dir / "goal-a"
    _assert_no_tmp_or_log(goal_dir)


def test_negative_log_event_on_unknown_goal_leaves_no_artifact(tmp_path: Path) -> None:
    store, workspace, _clock_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.log_event("goal-a", {"event": "x"})
    goal_dir = workspace.goals_dir / "goal-a"
    _assert_no_tmp_or_log(goal_dir)


# -- exists(): mismatched / wrong-type internal identity fails closed -------


def test_exists_rejects_mismatched_valid_contract_name(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = "goal-other"
    goal_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SpecError):
        store.exists("goal-a")
    assert goal_path.read_text(encoding="utf-8") == json.dumps(doc)


def test_exists_rejects_wrong_type_contract_name(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    doc = json.loads(goal_path.read_text(encoding="utf-8"))
    doc["contract"]["name"] = 12345
    goal_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(SpecError):
        store.exists("goal-a")
    assert goal_path.read_text(encoding="utf-8") == json.dumps(doc)


# -- save(): stale/mismatched durable manifest cannot be overwritten --------


def test_save_cannot_overwrite_mismatched_manifest(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    record = _record("goal-a", clock)
    store.save("goal-a", record)
    goal_path = workspace.goals_dir / "goal-a" / "goal.json"
    tampered_doc = json.loads(goal_path.read_text(encoding="utf-8"))
    tampered_doc["contract"]["name"] = "goal-other"
    tampered_bytes = json.dumps(tampered_doc)
    goal_path.write_text(tampered_bytes, encoding="utf-8")

    new_record = _record("goal-a", clock)
    updated_before = new_record.updated_ts
    with pytest.raises(SpecError):
        store.save("goal-a", new_record)
    assert new_record.updated_ts == updated_before
    assert goal_path.read_text(encoding="utf-8") == tampered_bytes


def test_save_cannot_adopt_pre_existing_empty_goal_directory(tmp_path: Path) -> None:
    store, workspace, clock = _store(tmp_path)
    empty_dir = workspace.goals_dir / "goal-a"
    empty_dir.mkdir()

    record = _record("goal-a", clock)
    updated_before = record.updated_ts
    with pytest.raises(StorageError):
        store.save("goal-a", record)
    assert record.updated_ts == updated_before
    assert list(empty_dir.iterdir()) == []
    with pytest.raises(StorageError):
        store.list_names()


# -- symlinked goals root fails closed ---------------------------------------


def test_symlinked_goals_dir_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    real_goals_dir = workspace.goals_dir
    real_goals_dir.rmdir()
    outside = tmp_path / "outside-goals"
    outside.mkdir()
    real_goals_dir.symlink_to(outside, target_is_directory=True)
    store = GoalStore(workspace, clock)
    with pytest.raises(StorageError):
        store.list_names()
    assert list(outside.iterdir()) == []
