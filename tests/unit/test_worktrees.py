"""R1-A SEC-001 slice: managed disposable Git worktrees.

Unit coverage of the ``ManagedWorktrees`` protocol against an offline fake
of the exact Git plumbing seam: owner-record lifecycle, exclusive claims,
fail-closed refusal of everything unowned/mismatched/corrupt/symlinked,
strict porcelain parsing, exact argv, and crash-barrier convergence."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dagvane.adapters.localexec import GitOps
from dagvane.adapters.worktrees import (
    ManagedWorktreeHandle,
    ManagedWorktrees,
    WorktreePurpose,
    WorktreeSpec,
    parse_worktree_list_z,
)
from dagvane.domain.models import SpecError, StorageError
from dagvane.workspace import paths as paths_module

SHA1 = "ab" * 20
SHA2 = "cd" * 20
MAIN_SHA = "ef" * 20


class FakeGit:
    """Offline stand-in for the exact plumbing seam: keeps a registration
    list, materializes/removes real target directories, emits strict
    porcelain ``-z`` bytes, and records every call it receives."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.common = repo / ".git"
        self.calls: list[tuple[str, ...]] = []
        self.entries: list[dict[str, Any]] = [
            {"path": str(repo), "head": MAIN_SHA, "branch": "refs/heads/main"}
        ]
        self.fail_add: str | None = None  # "before" | "after"
        self.fail_remove_before = False
        self.add_sha_override: str | None = None
        self.add_detached_flag = True
        self.before_add: Any = None
        self.before_move: Any = None
        self.before_remove: Any = None
        self.clean = True
        self.parents: dict[str, tuple[str, ...]] = {SHA2: (SHA1,)}

    def common_dir(self, repo: Path, repo_fd: int) -> Path:
        assert Path(f"/proc/self/fd/{repo_fd}").resolve() == repo
        self.calls.append(("common_dir", str(repo)))
        return self.common

    def list_z(self, repo: Path, repo_fd: int, common_fd: int) -> bytes:
        assert Path(f"/proc/self/fd/{repo_fd}").resolve() == repo
        assert Path(f"/proc/self/fd/{common_fd}").resolve() == self.common
        self.calls.append(("list_z", str(repo)))
        chunks: list[bytes] = []
        for entry in self.entries:
            lines = [f"worktree {entry['path']}".encode()]
            if entry.get("head"):
                lines.append(f"HEAD {entry['head']}".encode())
            if entry.get("branch"):
                lines.append(f"branch {entry['branch']}".encode())
            if entry.get("detached"):
                lines.append(b"detached")
            chunks.append(b"\0".join(lines) + b"\0\0")
        return b"".join(chunks)

    def add_detached(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int, sha: str
    ) -> None:
        assert os.fstat(repo_fd).st_ino
        assert os.fstat(common_fd).st_ino
        if self.before_add is not None:
            self.before_add()
        target = str(Path(f"/proc/self/fd/{target_fd}").resolve())
        self.calls.append(("add", str(repo), target, sha))
        if self.fail_add == "before":
            raise RuntimeError("simulated crash before worktree add")
        # ManagedWorktrees establishes and records the target inode before
        # Git population. Real Git accepts an already-existing empty target.
        Path(target).mkdir(exist_ok=True)
        registered_sha = self.add_sha_override or sha
        entry: dict[str, Any] = {"path": target, "head": registered_sha}
        if self.add_detached_flag:
            entry["detached"] = True
        else:
            entry["branch"] = "refs/heads/oops"
        self.entries.append(entry)
        if self.fail_add == "after":
            raise RuntimeError("simulated crash after worktree add")

    def move_exact(
        self,
        repo: Path,
        repo_fd: int,
        common_fd: int,
        source_fd: int,
        target_parent_fd: int,
        target_leaf: str,
    ) -> None:
        assert os.fstat(repo_fd).st_ino
        assert os.fstat(common_fd).st_ino
        if self.before_move is not None:
            self.before_move()
        source = str(Path(f"/proc/self/fd/{source_fd}").resolve())
        target_parent = Path(f"/proc/self/fd/{target_parent_fd}").resolve()
        target = str(target_parent / target_leaf)
        self.calls.append(("move", str(repo), source, target))
        registered = next(
            (entry for entry in self.entries if entry["path"] == source), None
        )
        if registered is None:
            raise RuntimeError("fd source is no longer the registered worktree")
        Path(source).rename(target)
        registered["path"] = target

    def remove_forced_fd(
        self, repo: Path, repo_fd: int, common_fd: int, target_fd: int
    ) -> None:
        assert os.fstat(repo_fd).st_ino
        assert os.fstat(common_fd).st_ino
        if self.fail_remove_before:
            raise RuntimeError("simulated crash before worktree remove")
        if self.before_remove is not None:
            self.before_remove()
        target = str(Path(f"/proc/self/fd/{target_fd}").resolve())
        self.calls.append(("remove", str(repo), target))
        if not any(entry["path"] == target for entry in self.entries):
            raise RuntimeError("fd target is no longer the registered worktree")
        path = Path(target)
        if path.exists():
            path.rmdir()
        self.entries = [e for e in self.entries if e["path"] != target]

    def is_clean(self, worktree: Path) -> bool:
        self.calls.append(("is_clean", str(worktree)))
        return self.clean

    def commit_parents(
        self, repo: Path, repo_fd: int, common_fd: int, sha: str
    ) -> tuple[str, ...]:
        assert os.fstat(repo_fd).st_ino
        assert os.fstat(common_fd).st_ino
        self.calls.append(("commit_parents", str(repo), sha))
        return self.parents.get(sha, ())


class Env:
    def __init__(self, tmp_path: Path) -> None:
        base = tmp_path.resolve()
        self.repo = base / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.root = self.repo / ".dagvane" / "worktrees"
        self.root.mkdir(parents=True)
        self.git = FakeGit(self.repo)
        self.manager = ManagedWorktrees(
            repo_root=self.repo, worktrees_root=self.root, git=self.git
        )
        self.spec = WorktreeSpec(
            goal_name="goal-a", purpose=WorktreePurpose.BASELINE, sha=SHA1
        )
        self.target = self.root / "goal-a-baseline"
        self.record = self.root / ".owners" / "goal-a-baseline.json"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _record_doc(env: Env) -> dict[str, Any]:
    latest, _valid = ManagedWorktrees._latest_record_document(  # noqa: SLF001
        env.record.read_bytes(), "test owner record"
    )
    doc: dict[str, Any] = json.loads(latest)
    return doc


def _rewrite_record(env: Env, doc: dict[str, Any]) -> None:
    env.record.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _valid_record(env: Env, state: str = "ready") -> dict[str, Any]:
    owners = env.root / ".owners"
    owners.mkdir(exist_ok=True)
    lease = owners / "goal-a-baseline.lock"
    lease.touch(exist_ok=True)
    repo_st = env.repo.stat()
    common_st = (env.repo / ".git").stat()
    root_st = env.root.stat()
    owners_st = owners.stat()
    lease_st = lease.stat()
    target_st = env.target.stat() if env.target.exists() else root_st
    return {
        "version": 5,
        "state": state,
        "repo": str(env.repo),
        "repo_dev": repo_st.st_dev,
        "repo_ino": repo_st.st_ino,
        "common_dir": str(env.repo / ".git"),
        "common_dir_dev": common_st.st_dev,
        "common_dir_ino": common_st.st_ino,
        "worktrees_root": str(env.root),
        "worktrees_root_dev": root_st.st_dev,
        "worktrees_root_ino": root_st.st_ino,
        "owners_dir": str(owners),
        "owners_dir_dev": owners_st.st_dev,
        "owners_dir_ino": owners_st.st_ino,
        "lease": str(lease),
        "lease_dev": lease_st.st_dev,
        "lease_ino": lease_st.st_ino,
        "target": str(env.target),
        "target_dev": target_st.st_dev,
        "target_ino": target_st.st_ino,
        "goal": "goal-a",
        "run_id": None,
        "purpose": "baseline",
        "sha": SHA1,
        "advance_token": None,
        "nonce": "0" * 32,
        "location": "public",
    }


def _plant_record(env: Env, doc: dict[str, Any]) -> bytes:
    env.record.parent.mkdir(exist_ok=True)
    _rewrite_record(env, doc)
    return env.record.read_bytes()


def _candidate_spec(sha: str = SHA1) -> WorktreeSpec:
    return WorktreeSpec(
        goal_name="goal-a",
        run_id="run-1",
        purpose=WorktreePurpose.CANDIDATE,
        sha=sha,
    )


def _candidate_record(env: Env) -> Path:
    return env.root / ".owners" / "goal-a-run-1.json"


def _read_doc(path: Path) -> dict[str, Any]:
    latest, _valid = ManagedWorktrees._latest_record_document(  # noqa: SLF001
        path.read_bytes(), "test owner record"
    )
    loaded: dict[str, Any] = json.loads(latest)
    return loaded


def _set_registered_head(env: Env, target: Path, sha: str) -> None:
    entry = next(item for item in env.git.entries if item["path"] == str(target))
    entry["head"] = sha


# -- 1. normal lifecycle ------------------------------------------------------


def test_create_produces_ready_worktree_and_exact_record(env: Env) -> None:
    assert env.manager.target_path(env.spec) == env.target
    handle = env.manager.create(env.spec)
    assert isinstance(handle, ManagedWorktreeHandle)
    assert handle.path == env.target
    assert handle.active
    assert env.target.is_dir()
    doc = _record_doc(env)
    reference = _valid_record(env)
    assert set(doc) == set(reference)
    nonce = doc.pop("nonce")
    reference.pop("nonce")
    assert isinstance(nonce, str) and len(nonce) == 32
    assert int(nonce, 16) >= 0  # 32 lowercase hex chars
    assert doc == reference
    assert stat.S_IMODE(env.record.stat().st_mode) == 0o600
    assert ("add", str(env.repo), str(env.target), SHA1) in env.git.calls
    handle.close()


def test_publish_claim_preserves_foreign_racing_target(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _candidate_spec()
    target = env.manager.target_path(spec)
    real_renameat2 = paths_module._renameat2
    foreign_identity: tuple[int, int] | None = None
    injected = False

    def collide_before_publish(
        old_dir_fd: int,
        old_name: str,
        new_dir_fd: int,
        new_name: str,
        flags: int,
    ) -> None:
        nonlocal foreign_identity, injected
        if not injected and new_name == spec.target_leaf:
            injected = True
            target.mkdir()
            st = target.stat()
            foreign_identity = (st.st_dev, st.st_ino)
        real_renameat2(old_dir_fd, old_name, new_dir_fd, new_name, flags)

    monkeypatch.setattr(paths_module, "_renameat2", collide_before_publish)
    with pytest.raises(StorageError):
        env.manager.create_candidate(spec)

    assert injected
    assert foreign_identity is not None
    current = target.stat()
    assert (current.st_dev, current.st_ino) == foreign_identity
    assert env.git.entries == [
        {"path": str(env.repo), "head": MAIN_SHA, "branch": "refs/heads/main"}
    ]


def test_remove_clears_worktree_and_leaves_durable_tombstone(env: Env) -> None:
    handle = env.manager.create(env.spec)
    env.manager.remove(handle)
    assert not env.target.exists()
    assert _record_doc(env)["state"] == "removed"
    assert _record_doc(env)["location"] == "absent"
    removes = [c for c in env.git.calls if c[0] == "remove"]
    assert len(removes) == 1
    assert removes[0][1] == str(env.repo)
    assert removes[0][2].startswith(str(env.root / ".goal-a-baseline."))
    assert removes[0][2].endswith(".remove")
    assert removes[0][2] != str(env.target)
    assert [e["path"] for e in env.git.entries] == [str(env.repo)]
    # No prune-like or rmtree-like escape hatch exists on the seam at all.
    assert {c[0] for c in env.git.calls} <= {
        "common_dir",
        "list_z",
        "add",
        "move",
        "remove",
    }


def test_remove_without_record_refuses(env: Env) -> None:
    invalid_authority: Any = env.spec
    with pytest.raises(SpecError, match="generation handle"):
        env.manager.remove(invalid_authority)
    assert env.git.calls == []


# -- 1a. retained mutable candidate lifecycle -------------------------------


def test_candidate_reopens_same_generation_with_manager_authoritative_sha(
    env: Env,
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    target = env.manager.target_path(spec)
    generation = handle.generation
    identity = (target.stat().st_dev, target.stat().st_ino)
    handle.close()

    # Caller state may lag or otherwise disagree. Owner/run/purpose identity,
    # not this SHA hint, selects the retained generation.
    recovered = env.manager.acquire_existing(_candidate_spec(SHA2))
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA1
    assert (target.stat().st_dev, target.stat().st_ino) == identity
    assert env.manager.prove_for_use(
        recovered, expected_bound_sha=SHA1
    ) == target
    recovered.close()


def test_candidate_begin_without_commit_recovers_old_binding(env: Env) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    generation = handle.generation
    env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    assert _read_doc(_candidate_record(env))["state"] == "advancing"
    handle.close()

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA1
    doc = _read_doc(_candidate_record(env))
    assert doc["state"] == "ready"
    assert doc["sha"] == SHA1
    assert doc["advance_token"] is None
    recovered.close()


def test_candidate_commit_before_complete_recovers_new_binding(env: Env) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    target = handle.path
    generation = handle.generation
    identity = (target.stat().st_dev, target.stat().st_ino)
    env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    _set_registered_head(env, target, SHA2)
    handle.close()  # process death before complete_sha_advance

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA2
    assert (target.stat().st_dev, target.stat().st_ino) == identity
    doc = _read_doc(_candidate_record(env))
    assert doc["state"] == "ready"
    assert doc["sha"] == SHA2
    assert doc["advance_token"] is None
    recovered.close()


def test_candidate_complete_before_caller_checkpoint_reopens_new_sha(
    env: Env,
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    target = handle.path
    generation = handle.generation
    token = env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    _set_registered_head(env, target, SHA2)
    env.manager.complete_sha_advance(handle, token, new_sha=SHA2)
    assert handle.bound_sha == SHA2
    handle.close()  # caller RunState still says SHA1

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA2
    recovered.close()


@pytest.mark.parametrize("failure", ["dirty", "not-one-child"])
def test_candidate_recovery_rejects_unproven_advanced_head(
    env: Env, failure: str
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    _set_registered_head(env, handle.path, SHA2)
    if failure == "dirty":
        env.git.clean = False
    else:
        env.git.parents[SHA2] = (SHA1, MAIN_SHA)
    handle.close()

    with pytest.raises(StorageError, match="not clean|exactly one parent"):
        env.manager.acquire_existing(spec)

    doc = _read_doc(_candidate_record(env))
    assert doc["state"] == "advancing"
    assert doc["sha"] == SHA1
    assert isinstance(doc["advance_token"], str)


def test_candidate_proof_rejects_public_target_replacement(env: Env) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    original = env.root / "retained-owned-candidate"
    handle.path.rename(original)
    handle.path.mkdir()
    sentinel = handle.path / "foreign"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(StorageError, match="owned directory"):
        env.manager.prove_for_use(handle, expected_bound_sha=SHA1)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert original.is_dir()
    handle.close()


def test_candidate_create_refuses_retained_generation(env: Env) -> None:
    spec = _candidate_spec()
    first = env.manager.create_candidate(spec)
    target = first.path
    generation = first.generation
    first.close()

    with pytest.raises(StorageError, match="acquire it"):
        env.manager.create_candidate(spec)

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.path == target
    recovered.close()


def test_candidate_acquire_pristine_creates_no_owner_authority(env: Env) -> None:
    spec = _candidate_spec()
    owners = env.root / ".owners"

    assert not owners.exists()
    assert env.manager.acquire_existing(spec) is None
    assert not owners.exists()
    assert not env.manager.target_path(spec).exists()
    assert not any(call[0] in ("add", "move", "remove") for call in env.git.calls)

    owners.mkdir()
    unrelated = owners / "unrelated.lock"
    unrelated.write_bytes(b"keep\n")
    env.git.calls.clear()
    assert env.manager.acquire_existing(spec) is None
    assert sorted(path.name for path in owners.iterdir()) == ["unrelated.lock"]
    assert unrelated.read_bytes() == b"keep\n"
    assert not any(call[0] in ("add", "move", "remove") for call in env.git.calls)


@pytest.mark.parametrize("leave_staging", [False, True])
def test_candidate_acquire_preclaim_is_no_create_and_explicit_create_advances(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    leave_staging: bool,
) -> None:
    spec = _candidate_spec()
    original_finish = env.manager._finish_preclaim

    def stop_preclaim(session: Any) -> bool:
        if leave_staging:
            staging = env.root / (
                f".{spec.target_leaf}.{session.generation.nonce}.claim"
            )
            staging.mkdir()
        raise StorageError("injected stop after durable preclaim")

    monkeypatch.setattr(env.manager, "_finish_preclaim", stop_preclaim)
    with pytest.raises(StorageError, match="durable preclaim"):
        env.manager.create_candidate(spec)
    preclaim = _read_doc(_candidate_record(env))
    assert preclaim["state"] == "preclaim"
    env.git.calls.clear()
    monkeypatch.setattr(env.manager, "_finish_preclaim", original_finish)

    assert env.manager.acquire_existing(spec) is None
    abandoned = _read_doc(_candidate_record(env))
    assert abandoned["state"] == "abandoned"
    assert abandoned["nonce"] == preclaim["nonce"]
    assert not any(call[0] == "add" for call in env.git.calls)
    assert env.manager.acquire_existing(spec) is None
    assert _read_doc(_candidate_record(env))["nonce"] == preclaim["nonce"]
    assert not any(call[0] == "add" for call in env.git.calls)

    created = env.manager.create_candidate(spec)
    assert created.generation != preclaim["nonce"]
    assert len([call for call in env.git.calls if call[0] == "add"]) == 1
    created.close()


def test_candidate_acquire_claimed_without_registration_never_calls_add(
    env: Env,
) -> None:
    spec = _candidate_spec()
    env.git.fail_add = "before"
    with pytest.raises(RuntimeError, match="before worktree add"):
        env.manager.create_candidate(spec)
    claimed = _read_doc(_candidate_record(env))
    assert claimed["state"] == "claimed"
    env.git.fail_add = None
    env.git.calls.clear()

    assert env.manager.acquire_existing(spec) is None
    assert _read_doc(_candidate_record(env)) == claimed
    assert not any(call[0] == "add" for call in env.git.calls)

    created = env.manager.create_candidate(spec)
    assert created.generation == claimed["nonce"]
    assert len([call for call in env.git.calls if call[0] == "add"]) == 1
    created.close()


# -- 2. spec validation and derivation ---------------------------------------


def test_target_derivation_preserves_existing_names() -> None:
    run = WorktreeSpec(
        goal_name="g", purpose=WorktreePurpose.CANDIDATE, sha=SHA1, run_id="r1"
    )
    assert run.target_leaf == "g-r1"
    verify = WorktreeSpec(
        goal_name="g", purpose=WorktreePurpose.VERIFY, sha=SHA1, run_id="r1"
    )
    assert verify.target_leaf == "g-r1-verify"
    review = WorktreeSpec(
        goal_name="g", purpose=WorktreePurpose.REVIEW, sha=SHA1, run_id="r1"
    )
    assert review.target_leaf == "g-r1-review"


def test_spec_run_id_rules() -> None:
    with pytest.raises(SpecError):
        WorktreeSpec(
            goal_name="g", purpose=WorktreePurpose.BASELINE, sha=SHA1, run_id="r1"
        )
    with pytest.raises(SpecError):
        WorktreeSpec(goal_name="g", purpose=WorktreePurpose.CANDIDATE, sha=SHA1)


@pytest.mark.parametrize(
    "bad_sha",
    ["AB" * 20, "ab" * 19, "ab" * 21, "zz" * 20, SHA1 + "\n", 42, None, b"ab" * 20],
)
def test_spec_rejects_inexact_sha(bad_sha: Any) -> None:
    with pytest.raises(SpecError):
        WorktreeSpec(goal_name="g", purpose=WorktreePurpose.BASELINE, sha=bad_sha)


# -- 3. exclusive collision and unowned state --------------------------------


def test_foreign_record_collision_refused_and_preserved(env: Env) -> None:
    before = _plant_record(env, {**_valid_record(env), "sha": SHA2})
    with pytest.raises(StorageError, match="SHA"):
        env.manager.create(env.spec)
    assert env.record.read_bytes() == before
    assert not any(c[0] in ("add", "remove") for c in env.git.calls)


def test_unowned_directory_refused_preserved_without_any_git_call(env: Env) -> None:
    env.target.mkdir()
    (env.target / "keep.txt").write_text("keep\n", encoding="utf-8")
    with pytest.raises(StorageError, match="no owner record"):
        env.manager.create(env.spec)
    assert (env.target / "keep.txt").read_text(encoding="utf-8") == "keep\n"
    assert env.git.calls == []  # refused before the first Git invocation


def test_unowned_raw_registration_refused_and_preserved(env: Env) -> None:
    env.git.entries.append({"path": str(env.target), "head": SHA1, "detached": True})
    with pytest.raises(StorageError, match="without this generation"):
        env.manager.create(env.spec)
    assert not any(c[0] in ("add", "remove") for c in env.git.calls)
    assert {"path": str(env.target), "head": SHA1, "detached": True} in env.git.entries


# -- 4. record corruption and field mismatches -------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        {"version": 2},
        {"version": True},
        {"state": "weird"},
        {"repo": "/elsewhere"},
        {"common_dir": "/elsewhere/.git"},
        {"worktrees_root": "/elsewhere/worktrees"},
        {"target": "/elsewhere/goal-a-baseline"},
        {"goal": "goal-b"},
        {"run_id": "r1"},
        {"purpose": "candidate"},
        {"sha": SHA2},
        {"nonce": "not-a-nonce"},
        {"extra_field": 1},
    ],
)
def test_record_field_mismatches_refused_and_preserved(
    env: Env, mutate: dict[str, Any]
) -> None:
    before = _plant_record(env, {**_valid_record(env), **mutate})
    with pytest.raises(StorageError):
        env.manager.create(env.spec)
    assert env.record.read_bytes() == before
    assert not any(c[0] in ("add", "remove") for c in env.git.calls)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]\n",
        b'"a string"\n',
        json.dumps({"version": 1}).encode(),  # missing fields
        b"\xff\xfe",
        b"{" + b" " * 20000 + b"}",  # oversized
    ],
)
def test_corrupt_records_refused_and_preserved(env: Env, raw: bytes) -> None:
    env.record.parent.mkdir(exist_ok=True)
    env.record.write_bytes(raw)
    with pytest.raises(StorageError):
        env.manager.create(env.spec)
    assert env.record.read_bytes() == raw
    assert not any(c[0] in ("add", "remove") for c in env.git.calls)


# -- 5. symlink sentinels -----------------------------------------------------


def test_symlinked_owners_directory_refused(env: Env, tmp_path: Path) -> None:
    outside = tmp_path.resolve() / "outside-owners"
    outside.mkdir()
    (env.root / ".owners").symlink_to(outside)
    with pytest.raises(StorageError):
        env.manager.create(env.spec)
    assert (env.root / ".owners").is_symlink()
    assert env.git.calls == []


def test_symlinked_record_refused(env: Env, tmp_path: Path) -> None:
    env.record.parent.mkdir()
    outside = tmp_path.resolve() / "outside-record.json"
    outside.write_text("{}", encoding="utf-8")
    env.record.symlink_to(outside)
    with pytest.raises(StorageError):
        env.manager.create(env.spec)
    assert env.record.is_symlink()
    assert outside.read_text(encoding="utf-8") == "{}"
    assert env.git.calls == []


def test_symlinked_lifecycle_lease_is_refused_and_preserved(
    env: Env, tmp_path: Path
) -> None:
    owners = env.root / ".owners"
    owners.mkdir()
    outside = tmp_path.resolve() / "outside.lock"
    outside.write_text("sentinel\n", encoding="utf-8")
    lock = owners / "goal-a-baseline.lock"
    lock.symlink_to(outside)

    with pytest.raises(StorageError, match="lifecycle lease"):
        env.manager.create(env.spec)

    assert lock.is_symlink()
    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert env.git.calls == []


def test_symlinked_target_with_ready_record_refused_before_git(env: Env) -> None:
    handle = env.manager.create(env.spec)
    env.target.rmdir()
    outside = env.repo / "victim"
    outside.mkdir()
    env.target.symlink_to(outside)
    env.git.calls.clear()
    with pytest.raises(StorageError, match="owned directory"):
        env.manager.remove(handle)
    assert env.target.is_symlink()
    assert outside.is_dir()
    assert not any(c[0] == "remove" for c in env.git.calls)


# -- 6. strict porcelain parsing ---------------------------------------------


def test_parse_worktree_list_z_accepts_main_and_detached_entries() -> None:
    raw = (
        b"worktree /repo\0HEAD " + MAIN_SHA.encode() + b"\0branch refs/heads/main\0\0"
        b"worktree /repo/.dagvane/worktrees/g-baseline\0HEAD "
        + SHA1.encode()
        + b"\0detached\0locked reason with spaces\0\0"
    )
    entries = parse_worktree_list_z(raw)
    assert set(entries) == {"/repo", "/repo/.dagvane/worktrees/g-baseline"}
    entry = entries["/repo/.dagvane/worktrees/g-baseline"]
    assert entry.head == SHA1 and entry.detached and not entry.bare


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"worktree /a\0HEAD " + SHA1.encode() + b"\0detached\0",  # missing terminator
        b"\0\0",  # empty entry
        b"HEAD " + SHA1.encode() + b"\0\0",  # entry not starting with worktree
        b"worktree relative/path\0HEAD " + SHA1.encode() + b"\0detached\0\0",
        b"worktree /a\0HEAD short\0detached\0\0",  # invalid HEAD
        b"worktree /a\0HEAD " + SHA1.upper().encode() + b"\0detached\0\0",
        b"worktree /a\0detached\0\0",  # missing HEAD
        b"worktree /a\0HEAD " + SHA1.encode() + b"\0\0",  # neither branch nor detached
        b"worktree /a\0HEAD "
        + SHA1.encode()
        + b"\0branch refs/heads/x\0detached\0\0",  # ambiguous
        b"worktree /a\0HEAD "
        + SHA1.encode()
        + b"\0HEAD "
        + SHA2.encode()
        + b"\0detached\0\0",  # duplicate field
        b"worktree /a\0worktree /b\0HEAD " + SHA1.encode() + b"\0detached\0\0",
        b"worktree /a\0HEAD " + SHA1.encode() + b"\0detached\0mystery attr\0\0",
        b"worktree /a\0HEAD " + SHA1.encode() + b"\0detached\0\0"
        b"worktree /a\0HEAD " + SHA2.encode() + b"\0detached\0\0",  # duplicate target
        b"worktree /a\0bare\0HEAD " + SHA1.encode() + b"\0\0",  # bare with checkout
        b"worktree /a\0HEAD \xff\xfe\0detached\0\0",  # non-UTF-8
    ],
)
def test_parse_worktree_list_z_rejects_malformed(raw: bytes) -> None:
    with pytest.raises(StorageError):
        parse_worktree_list_z(raw)


def test_wrong_registration_sha_after_add_fails_before_ready(env: Env) -> None:
    env.git.add_sha_override = SHA2
    with pytest.raises(StorageError, match="does not exactly match"):
        env.manager.create(env.spec)
    assert _record_doc(env)["state"] == "claimed"


def test_non_detached_registration_after_add_fails_before_ready(env: Env) -> None:
    env.git.add_detached_flag = False
    with pytest.raises(StorageError, match="does not exactly match"):
        env.manager.create(env.spec)
    assert _record_doc(env)["state"] == "claimed"


def test_fd_bound_add_never_populates_or_registers_public_replacement(
    env: Env,
) -> None:
    displaced = env.root / "displaced-owned-add-target"
    replacement_identity: tuple[int, int] | None = None

    def replace_after_fd_pin() -> None:
        nonlocal replacement_identity
        env.target.rename(displaced)
        env.target.mkdir()
        (env.target / "sentinel").write_text("foreign\n", encoding="utf-8")
        replacement_identity = (env.target.stat().st_dev, env.target.stat().st_ino)

    env.git.before_add = replace_after_fd_pin
    with pytest.raises(StorageError, match="owned directory"):
        env.manager.create(env.spec)

    assert replacement_identity is not None
    assert (env.target.stat().st_dev, env.target.stat().st_ino) == replacement_identity
    assert (env.target / "sentinel").read_text(encoding="utf-8") == "foreign\n"
    assert not any(entry["path"] == str(env.target) for entry in env.git.entries)
    assert any(entry["path"] == str(displaced) for entry in env.git.entries)


def test_fd_bound_move_never_moves_public_replacement(
    env: Env,
) -> None:
    handle = env.manager.create(env.spec)
    displaced = env.root / "displaced-owned-move-source"
    replacement_identity: tuple[int, int] | None = None

    def replace_after_fd_pin() -> None:
        nonlocal replacement_identity
        env.target.rename(displaced)
        env.target.mkdir()
        (env.target / "sentinel").write_text("foreign\n", encoding="utf-8")
        replacement_identity = (env.target.stat().st_dev, env.target.stat().st_ino)

    env.git.before_move = replace_after_fd_pin
    with pytest.raises(RuntimeError, match="no longer the registered"):
        env.manager.remove(handle)

    assert replacement_identity is not None
    assert (env.target.stat().st_dev, env.target.stat().st_ino) == replacement_identity
    assert (env.target / "sentinel").read_text(encoding="utf-8") == "foreign\n"
    assert displaced.is_dir()
    assert not any(".remove" in entry["path"] for entry in env.git.entries)
    assert _record_doc(env)["state"] == "removing"


# -- 7. crash barriers converge safely ---------------------------------------


def test_barrier_after_claim_retry_finishes_create(env: Env) -> None:
    env.git.fail_add = "before"
    with pytest.raises(RuntimeError):
        env.manager.create(env.spec)
    assert _record_doc(env)["state"] == "claimed"
    nonce = _record_doc(env)["nonce"]
    claimed_identity = (
        _record_doc(env)["target_dev"],
        _record_doc(env)["target_ino"],
    )
    assert env.target.is_dir()  # owned inode exists before Git population
    assert (env.target.stat().st_dev, env.target.stat().st_ino) == claimed_identity
    env.git.fail_add = None
    handle = env.manager.create(env.spec)
    assert handle.path == env.target
    doc = _record_doc(env)
    assert doc["state"] == "ready"
    assert doc["nonce"] == nonce  # the same claim was finished, not replaced
    assert (doc["target_dev"], doc["target_ino"]) == claimed_identity
    assert env.target.is_dir()
    handle.close()


def test_barrier_add_before_ready_retry_transitions_only(env: Env) -> None:
    env.git.fail_add = "after"
    with pytest.raises(RuntimeError):
        env.manager.create(env.spec)
    assert _record_doc(env)["state"] == "claimed"
    assert env.target.is_dir()
    env.git.fail_add = None
    handle = env.manager.create(env.spec)
    assert handle.path == env.target
    assert _record_doc(env)["state"] == "ready"
    assert len([c for c in env.git.calls if c[0] == "add"]) == 1  # no second add
    handle.close()


def test_barrier_removing_before_remove_retry_finishes_cleanup(env: Env) -> None:
    handle = env.manager.create(env.spec)
    env.git.fail_remove_before = True
    with pytest.raises(RuntimeError):
        env.manager.remove(handle)
    assert _record_doc(env)["state"] == "removing"
    assert _record_doc(env)["location"] == "quarantine"
    quarantine = env.root / (
        f".goal-a-baseline.{_record_doc(env)['nonce']}.remove"
    )
    assert not env.target.exists()
    assert quarantine.is_dir()
    env.git.fail_remove_before = False
    recovered = env.manager.create(env.spec)
    env.manager.remove(recovered)
    assert not env.target.exists()
    assert _record_doc(env)["state"] == "removed"


def test_barrier_remove_before_tombstone_retry_uses_no_git(env: Env) -> None:
    abandoned = env.manager.create(env.spec)
    abandoned.close()
    doc = _record_doc(env)
    doc["state"] = "removing"
    doc["location"] = "quarantine"
    _rewrite_record(env, doc)
    env.target.rmdir()
    env.git.entries = [e for e in env.git.entries if e["path"] != str(env.target)]
    env.git.calls.clear()
    recovered = env.manager.create(env.spec)
    # Recovery tombstoned the old removing record and made a new generation;
    # it did not run Git removal against already-absent old state.
    assert not any(c[0] == "remove" for c in env.git.calls)
    env.manager.remove(recovered)
    assert _record_doc(env)["state"] == "removed"


def test_matching_ready_baseline_is_removed_and_recreated_fresh(env: Env) -> None:
    first = env.manager.create(env.spec)
    first_nonce = _record_doc(env)["nonce"]
    with pytest.raises(StorageError, match="lifecycle is active"):
        env.manager.create(env.spec)
    first.close()  # model a crashed/abandoned baseline holder
    second = env.manager.create(env.spec)
    assert second.path == env.target
    doc = _record_doc(env)
    assert doc["state"] == "ready"
    assert doc["nonce"] != first_nonce  # a genuinely fresh claim
    assert len([c for c in env.git.calls if c[0] == "remove"]) == 1
    assert len([c for c in env.git.calls if c[0] == "add"]) == 2
    before = env.record.read_bytes()
    with pytest.raises(StorageError, match="stale or closed"):
        env.manager.remove(first)
    assert env.record.read_bytes() == before
    assert second.active
    env.manager.remove(second)


def test_unrelated_registrations_stay_untouched(env: Env) -> None:
    other = {"path": str(env.repo / "elsewhere"), "head": SHA2, "detached": True}
    env.git.entries.append(dict(other))
    handle = env.manager.create(env.spec)
    env.manager.remove(handle)
    assert other in env.git.entries
    removes = [c for c in env.git.calls if c[0] == "remove"]
    assert len(removes) == 1 and removes[0][2] != str(env.target)


# -- 8. generation/identity adversarial regressions -------------------------


def test_record_nonce_and_inode_cas_refuses_stale_remover(env: Env) -> None:
    handle = env.manager.create(env.spec)
    replacement = _record_doc(env)
    replacement["nonce"] = "1" * 32
    replacement_path = env.record.with_name("replacement.json")
    replacement_path.write_text(
        json.dumps(replacement, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(replacement_path, env.record)
    before = env.record.read_bytes()
    env.git.calls.clear()

    with pytest.raises(StorageError, match="CAS refused"):
        env.manager.remove(handle)

    assert env.record.read_bytes() == before
    assert env.target.is_dir()
    assert not any(call[0] == "remove" for call in env.git.calls)


def test_record_replaced_immediately_before_unlink_is_not_unlinked(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = env.manager.create(env.spec)
    original_unlink = env.manager._cas_unlink
    replacement_bytes: bytes | None = None

    def replace_then_unlink(session: Any) -> None:
        nonlocal replacement_bytes
        replacement = _record_doc(env)
        replacement["nonce"] = "2" * 32
        replacement_path = env.record.with_name("new-generation.json")
        replacement_path.write_text(
            json.dumps(replacement, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(replacement_path, env.record)
        replacement_bytes = env.record.read_bytes()
        original_unlink(session)

    monkeypatch.setattr(env.manager, "_cas_unlink", replace_then_unlink)
    with pytest.raises(StorageError, match="CAS refused"):
        env.manager.remove(handle)

    assert replacement_bytes is not None
    assert env.record.read_bytes() == replacement_bytes


def test_ready_sha_mutation_is_discarded_only_via_owned_quarantine(env: Env) -> None:
    handle = env.manager.create(env.spec)
    target_entry = next(
        entry for entry in env.git.entries if entry["path"] == str(env.target)
    )
    target_entry["head"] = SHA2
    env.git.calls.clear()

    env.manager.remove(handle)

    assert _record_doc(env)["state"] == "removed"
    assert not env.target.exists()
    removes = [call for call in env.git.calls if call[0] == "remove"]
    assert len(removes) == 1
    assert removes[0][2] != str(env.target)


def test_renamed_owned_target_and_copied_git_replacement_are_never_deleted(
    env: Env,
) -> None:
    handle = env.manager.create(env.spec)
    original = env.root / "original-owned"
    env.target.rename(original)
    (original / "original-sentinel").write_text("owned\n", encoding="utf-8")
    (env.target / ".git").mkdir(parents=True)
    victim = env.target / "victim"
    victim.write_text("foreign\n", encoding="utf-8")
    env.git.calls.clear()

    with pytest.raises(StorageError, match="owned directory"):
        env.manager.remove(handle)

    assert (original / "original-sentinel").read_text(encoding="utf-8") == "owned\n"
    assert victim.read_text(encoding="utf-8") == "foreign\n"
    assert _record_doc(env)["state"] == "ready"
    assert not any(call[0] == "remove" for call in env.git.calls)


def test_common_directory_inode_replacement_refuses_and_preserves_target(
    env: Env,
) -> None:
    handle = env.manager.create(env.spec)
    original_common = env.repo / ".git-owned"
    (env.repo / ".git").rename(original_common)
    (env.repo / ".git").mkdir()
    foreign = env.target / "foreign-sentinel"
    foreign.write_text("keep\n", encoding="utf-8")
    env.git.calls.clear()

    with pytest.raises(StorageError, match="no longer names the pinned"):
        env.manager.remove(handle)

    assert foreign.read_text(encoding="utf-8") == "keep\n"
    assert not any(call[0] == "remove" for call in env.git.calls)


def test_copied_owners_lock_and_record_cannot_rebind_active_generation(
    env: Env,
) -> None:
    handle = env.manager.create(env.spec)
    owners = env.root / ".owners"
    original = env.root / ".owners-original"
    owners.rename(original)
    owners.mkdir()
    shutil.copy2(original / env.record.name, owners / env.record.name)
    shutil.copy2(
        original / "goal-a-baseline.lock",
        owners / "goal-a-baseline.lock",
    )
    copied_record = (owners / env.record.name).read_bytes()
    env.git.calls.clear()

    with pytest.raises(StorageError, match="owner-record directory"):
        env.manager.remove(handle)

    assert (owners / env.record.name).read_bytes() == copied_record
    assert env.target.is_dir()
    assert not any(call[0] in ("move", "remove") for call in env.git.calls)

    replacement_manager = ManagedWorktrees(
        repo_root=env.repo, worktrees_root=env.root, git=env.git
    )
    with pytest.raises(StorageError, match="owners_dir_(dev|ino)|lease_(dev|ino)"):
        replacement_manager.create(env.spec)
    assert env.target.is_dir()


def test_public_replacement_after_quarantine_is_never_removed(env: Env) -> None:
    handle = env.manager.create(env.spec)

    def plant_public_victim() -> None:
        assert not env.target.exists()
        env.target.mkdir()
        (env.target / "sentinel").write_text("keep\n", encoding="utf-8")

    env.git.before_remove = plant_public_victim
    env.manager.remove(handle)

    assert (env.target / "sentinel").read_text(encoding="utf-8") == "keep\n"
    remove_calls = [call for call in env.git.calls if call[0] == "remove"]
    assert len(remove_calls) == 1
    assert remove_calls[0][2] != str(env.target)


def test_claim_write_failure_leaves_no_unowned_public_target_and_retry_succeeds(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.manager._write_preclaim
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise StorageError("injected claim persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(env.manager, "_write_preclaim", fail_once)
    with pytest.raises(StorageError, match="claim persistence failure"):
        env.manager.create(env.spec)

    assert not env.target.exists()
    assert not env.record.exists()
    handle = env.manager.create(env.spec)
    assert handle.path == env.target
    env.manager.remove(handle)


def test_mkdir_then_claim_failure_abandons_ambiguous_inode_and_uses_new_nonce(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = env.manager._cas_transition
    injected = False

    def fail_after_mkdir(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if (
            not injected
            and kwargs.get("expected_state") == "preclaim"
            and kwargs.get("new_state") == "claimed"
        ):
            injected = True
            raise StorageError("injected mkdir-to-claim failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(env.manager, "_cas_transition", fail_after_mkdir)
    with pytest.raises(StorageError, match="mkdir-to-claim failure"):
        env.manager.create(env.spec)

    preclaim = _record_doc(env)
    assert preclaim["state"] == "preclaim"
    assert preclaim["target_dev"] == 0 and preclaim["target_ino"] == 0
    assert not env.target.exists()
    staging = env.root / f".goal-a-baseline.{preclaim['nonce']}.claim"
    staging_identity = (staging.stat().st_dev, staging.stat().st_ino)

    handle = env.manager.create(env.spec)
    ready = _record_doc(env)
    assert ready["nonce"] != preclaim["nonce"]
    assert (ready["target_dev"], ready["target_ino"]) != staging_identity
    assert (staging.stat().st_dev, staging.stat().st_ino) == staging_identity
    env.manager.remove(handle)


def test_preclaim_replacement_is_preserved_and_never_adopted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_transition = env.manager._cas_transition
    injected = False

    def crash_after_mkdir(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        if (
            not injected
            and kwargs.get("expected_state") == "preclaim"
            and kwargs.get("new_state") == "claimed"
        ):
            injected = True
            raise StorageError("crash after staging mkdir")
        return original_transition(*args, **kwargs)

    monkeypatch.setattr(env.manager, "_cas_transition", crash_after_mkdir)
    with pytest.raises(StorageError, match="crash after staging mkdir"):
        env.manager.create(env.spec)
    preclaim = _record_doc(env)
    staging = env.root / f".goal-a-baseline.{preclaim['nonce']}.claim"
    original = env.root / "original-unbound-staging"
    staging.rename(original)
    staging.mkdir()
    replacement_identity = (staging.stat().st_dev, staging.stat().st_ino)
    monkeypatch.setattr(env.manager, "_cas_transition", original_transition)

    handle = env.manager.create(env.spec)
    ready = _record_doc(env)
    assert handle.generation != preclaim["nonce"]
    assert (ready["target_dev"], ready["target_ino"]) != replacement_identity
    assert staging.is_dir()
    assert original.is_dir()
    handle.close()


def test_transition_record_swap_preserves_foreign_bytes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    record = _candidate_record(env)
    foreign = record.with_name("foreign-owner-record")
    foreign_bytes = b"foreign owner bytes\n"
    foreign.write_bytes(foreign_bytes)
    real_pwrite = os.pwrite
    swapped = False

    def swap_then_write(fd: int, data: Any, offset: int) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            os.replace(foreign, record)
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", swap_then_write)
    with pytest.raises(StorageError, match="inode changed"):
        env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)

    assert record.read_bytes() == foreign_bytes
    assert handle.path.is_dir()
    handle.close()


def test_removed_record_is_tombstoned_without_pathname_unlink(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = env.manager.create(env.spec)
    real_unlink = os.unlink
    record_unlinks = 0

    def guard_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal record_unlinks
        if path == env.record.name and kwargs.get("dir_fd") is not None:
            record_unlinks += 1
            raise AssertionError("owner record pathname must never be unlinked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", guard_unlink)
    env.manager.remove(handle)

    assert record_unlinks == 0
    assert _record_doc(env)["state"] == "removed"


def test_quarantine_swap_after_fd_pin_preserves_replacement_and_owned_inode(
    env: Env,
) -> None:
    handle = env.manager.create(env.spec)
    replacement_identity: tuple[int, int] | None = None
    displaced = env.root / "displaced-owned-quarantine"

    def swap_quarantine() -> None:
        nonlocal replacement_identity
        doc = _record_doc(env)
        quarantine = env.root / f".goal-a-baseline.{doc['nonce']}.remove"
        quarantine.rename(displaced)
        quarantine.mkdir()
        replacement_identity = (
            quarantine.stat().st_dev,
            quarantine.stat().st_ino,
        )

    env.git.before_remove = swap_quarantine
    with pytest.raises(RuntimeError, match="no longer the registered"):
        env.manager.remove(handle)

    doc = _record_doc(env)
    quarantine = env.root / f".goal-a-baseline.{doc['nonce']}.remove"
    assert replacement_identity is not None
    assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == replacement_identity
    assert displaced.is_dir()
    assert doc["state"] == "removing"
    assert doc["location"] == "quarantine"


def test_partial_inactive_checkpoint_recovers_prior_state(env: Env) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    record = _candidate_record(env)
    doc = _read_doc(record)
    partial = env.manager._checkpoint_frame(  # noqa: SLF001
        env.manager._record_bytes(  # noqa: SLF001
            {**doc, "state": "advancing", "advance_token": "1" * 32}
        ),
        100,
    )
    with record.open("r+b", buffering=0) as stream:
        stream.seek(2 * 32768)
        stream.write(b"\0" + partial[1:100])
        os.fsync(stream.fileno())
    handle.close()

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    env.manager.begin_sha_advance(recovered, expected_old_sha=SHA1)
    assert _read_doc(record)["state"] == "advancing"
    assert record.stat().st_size == 262144
    recovered.close()


@pytest.mark.parametrize("crash_write", [1, 2, 3])
def test_checkpoint_rollover_crash_windows_keep_exact_prior_authority(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    crash_write: int,
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    generation = handle.generation
    record = _candidate_record(env)
    record_identity = (record.stat().st_dev, record.stat().st_ino)
    real_pwrite = os.pwrite
    calls = 0

    def crash_in_inactive_slot(fd: int, data: Any, offset: int) -> int:
        nonlocal calls
        calls += 1
        if calls == crash_write:
            if crash_write == 2:
                raw = bytes(data)
                real_pwrite(fd, raw[: max(1, len(raw) // 2)], offset)
            raise OSError(f"injected checkpoint crash {crash_write}")
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", crash_in_inactive_slot)
    with pytest.raises(StorageError, match="checkpoint crash"):
        env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    handle.close()
    monkeypatch.setattr(os, "pwrite", real_pwrite)

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA1
    assert _read_doc(record)["state"] == "ready"
    assert (record.stat().st_dev, record.stat().st_ino) == record_identity
    assert record.stat().st_size == 262144
    recovered.close()


def test_committed_checkpoint_survives_crash_before_in_memory_update(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _candidate_spec()
    handle = env.manager.create_candidate(spec)
    generation = handle.generation
    record = _candidate_record(env)
    original_checkpoint = env.manager._checkpoint_record_document

    def checkpoint_then_crash(*args: Any, **kwargs: Any) -> None:
        original_checkpoint(*args, **kwargs)
        raise StorageError("injected post-checkpoint crash")

    monkeypatch.setattr(
        env.manager, "_checkpoint_record_document", checkpoint_then_crash
    )
    with pytest.raises(StorageError, match="post-checkpoint crash"):
        env.manager.begin_sha_advance(handle, expected_old_sha=SHA1)
    handle.close()
    monkeypatch.setattr(
        env.manager, "_checkpoint_record_document", original_checkpoint
    )

    assert _read_doc(record)["state"] == "advancing"
    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA1
    assert _read_doc(record)["state"] == "ready"
    recovered.close()


@pytest.mark.parametrize("crash_write", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("near_limit", [False, True])
def test_legacy_journal_migration_crash_keeps_v4_authority(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    crash_write: int,
    near_limit: bool,
) -> None:
    spec = _candidate_spec()
    created = env.manager.create_candidate(spec)
    generation = created.generation
    created.close()
    record = _candidate_record(env)
    legacy = _read_doc(record)
    legacy["version"] = 4
    legacy_bytes = (
        json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    legacy_frame = env.manager._record_frame(legacy_bytes)  # noqa: SLF001
    legacy_journal = bytearray(legacy_bytes)
    if near_limit:
        while len(legacy_journal) + len(legacy_frame) <= 262144:
            legacy_journal.extend(legacy_frame)
        assert len(legacy_journal) > 229376
    record.write_bytes(legacy_journal)
    record_identity = (record.stat().st_dev, record.stat().st_ino)
    acquired = env.manager.acquire_existing(spec)
    assert acquired is not None
    real_pwrite = os.pwrite
    calls = 0

    def crash_migration(fd: int, data: Any, offset: int) -> int:
        nonlocal calls
        calls += 1
        if calls == crash_write:
            if crash_write in (2, 5):
                raw = bytes(data)
                real_pwrite(fd, raw[: max(1, len(raw) // 2)], offset)
            raise OSError(f"injected v4 migration crash {crash_write}")
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", crash_migration)
    with pytest.raises(StorageError, match="migration crash"):
        env.manager.begin_sha_advance(acquired, expected_old_sha=SHA1)
    acquired.close()
    monkeypatch.setattr(os, "pwrite", real_pwrite)

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.generation == generation
    assert recovered.bound_sha == SHA1
    if crash_write <= 3:
        assert _read_doc(record)["version"] == 4
        assert _read_doc(record)["state"] == "ready"
    token = env.manager.begin_sha_advance(recovered, expected_old_sha=SHA1)
    assert _read_doc(record)["version"] == 5
    assert _read_doc(record)["state"] == "advancing"
    assert (record.stat().st_dev, record.stat().st_ino) == record_identity
    assert record.stat().st_size == 262144
    assert token is not None
    recovered.close()


def test_record_slots_never_cross_limit_and_survive_many_generations(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pwrite = os.pwrite
    greatest_end = 0

    def bounded_pwrite(fd: int, data: Any, offset: int) -> int:
        nonlocal greatest_end
        greatest_end = max(greatest_end, offset + len(data))
        assert greatest_end <= 262144
        return real_pwrite(fd, data, offset)

    monkeypatch.setattr(os, "pwrite", bounded_pwrite)
    record_identity: tuple[int, int] | None = None
    seen_generations: set[str] = set()
    for _ in range(300):
        handle = env.manager.create(env.spec)
        seen_generations.add(handle.generation)
        current_identity = (env.record.stat().st_dev, env.record.stat().st_ino)
        if record_identity is None:
            record_identity = current_identity
        assert current_identity == record_identity
        assert env.record.stat().st_size == 262144
        env.manager.remove(handle)

    assert len(seen_generations) == 300
    assert greatest_end <= 262144
    assert env.record.stat().st_size == 262144
    assert _record_doc(env)["state"] == "removed"


def test_checkpoint_sequence_wrap_keeps_newest_authority(env: Env) -> None:
    spec = _candidate_spec()
    created = env.manager.create_candidate(spec)
    created.close()
    record = _candidate_record(env)
    payload = env.manager._record_bytes(_read_doc(record))  # noqa: SLF001
    frame = env.manager._checkpoint_frame(  # noqa: SLF001
        payload, (1 << 64) - 1
    )
    slots = bytearray(262144)
    slots[: len(frame)] = frame
    record.write_bytes(slots)

    acquired = env.manager.acquire_existing(spec)
    assert acquired is not None
    env.manager.begin_sha_advance(acquired, expected_old_sha=SHA1)
    assert _read_doc(record)["state"] == "advancing"
    acquired.close()

    recovered = env.manager.acquire_existing(spec)
    assert recovered is not None
    assert recovered.bound_sha == SHA1
    assert _read_doc(record)["state"] == "ready"
    recovered.close()


def test_crash_before_add_does_not_adopt_foreign_exact_worktree(env: Env) -> None:
    env.git.fail_add = "before"
    with pytest.raises(RuntimeError):
        env.manager.create(env.spec)
    claimed = _record_doc(env)
    owned = env.root / "owned-before-crash"
    env.target.rename(owned)
    env.target.mkdir()
    (env.target / "foreign-sentinel").write_text("keep\n", encoding="utf-8")
    env.git.entries.append(
        {"path": str(env.target), "head": SHA1, "detached": True}
    )
    env.git.fail_add = None
    env.git.calls.clear()

    with pytest.raises(StorageError, match="target identity changed"):
        env.manager.create(env.spec)

    assert (env.target / "foreign-sentinel").read_text(encoding="utf-8") == "keep\n"
    assert owned.is_dir()
    assert _record_doc(env) == claimed
    assert not any(call[0] == "remove" for call in env.git.calls)


def test_duplicate_owner_json_key_fails_closed(env: Env) -> None:
    doc = _valid_record(env)
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    raw = raw.replace(
        '"nonce": "00000000000000000000000000000000"',
        '"nonce": "00000000000000000000000000000000", '
        '"nonce": "11111111111111111111111111111111"',
    )
    env.record.parent.mkdir(exist_ok=True)
    env.record.write_text(raw, encoding="utf-8")
    before = env.record.read_bytes()

    with pytest.raises(StorageError, match="duplicate key"):
        env.manager.create(env.spec)

    assert env.record.read_bytes() == before
    assert not any(call[0] in ("add", "remove") for call in env.git.calls)


def test_new_owners_directory_is_parent_fsynced(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_mkdir = os.mkdir
    real_fsync = os.fsync
    root_identity = (env.root.stat().st_dev, env.root.stat().st_ino)

    def spy_mkdir(path: str, *args: Any, **kwargs: Any) -> None:
        real_mkdir(path, *args, **kwargs)
        if path == ".owners":
            events.append("mkdir-owners")

    def spy_fsync(fd: int) -> None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) == root_identity:
            events.append("fsync-root")
        real_fsync(fd)

    monkeypatch.setattr(os, "mkdir", spy_mkdir)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    handle = env.manager.create(env.spec)
    handle.close()

    assert events[:2] == ["mkdir-owners", "fsync-root"]


# -- 9. exact argv of the real plumbing --------------------------------------


class _Completed:
    returncode = 0
    stdout = b""
    stderr = b""


def test_gitops_worktree_plumbing_uses_exact_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[list[str]] = []
    inherited: list[tuple[int, ...]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> _Completed:
        recorded.append(list(argv))
        if "pass_fds" in kwargs:
            inherited.append(kwargs["pass_fds"])
        return _Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    target = tmp_path / "target"
    common = tmp_path / "common"
    target.mkdir()
    common.mkdir()
    repo_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    common_fd = os.open(common, os.O_RDONLY | os.O_DIRECTORY)
    target_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    source_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with GitOps.pinned_worktree_authority(repo_fd, common_fd):
            GitOps.worktree_add_detached_exact(tmp_path, source_fd, SHA1)
            GitOps.worktree_move_exact(
                tmp_path, source_fd, target_fd, "exact-quarantine"
            )
            GitOps.worktree_remove_forced_fd(tmp_path, target_fd)
            GitOps.worktree_list_porcelain_z(tmp_path)
    finally:
        os.close(source_fd)
        os.close(target_fd)
        os.close(common_fd)
        os.close(repo_fd)
    authority = [
        f"--git-dir=/proc/self/fd/{common_fd}",
        f"--work-tree=/proc/self/fd/{repo_fd}",
    ]
    assert recorded == [
        [
            "git",
            *authority,
            "worktree",
            "add",
            "--detach",
            "--",
            f"/proc/self/fd/{source_fd}",
            SHA1,
        ],
        [
            "git",
            *authority,
            "worktree",
            "move",
            "--",
            f"/proc/self/fd/{source_fd}",
            f"/proc/self/fd/{target_fd}/exact-quarantine",
        ],
        [
            "git",
            *authority,
            "worktree",
            "remove",
            "--force",
            "--",
            f"/proc/self/fd/{target_fd}",
        ],
        ["git", *authority, "worktree", "list", "--porcelain", "-z"],
    ]
    assert inherited == [
        (repo_fd, common_fd, source_fd),
        (repo_fd, common_fd, source_fd, target_fd),
        (repo_fd, common_fd, target_fd),
        (repo_fd, common_fd),
    ]


def test_gitops_commit_parents_uses_one_exact_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded: list[list[str]] = []

    class CompletedParents:
        returncode = 0
        stdout = f"{SHA2} {SHA1}\n".encode()
        stderr = b""

    def fake_run(argv: list[str], **_kwargs: Any) -> CompletedParents:
        recorded.append(list(argv))
        return CompletedParents()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert GitOps.commit_parents(tmp_path, SHA2) == (SHA1,)
    assert recorded == [
        ["git", "rev-list", "--parents", "--max-count=1", SHA2]
    ]


def test_managed_worktrees_module_never_touches_shutil_or_subprocess() -> None:
    import dagvane.adapters.worktrees as module

    assert not hasattr(module, "shutil")
    assert not hasattr(module, "subprocess")
