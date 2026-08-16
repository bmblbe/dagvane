"""Focused security tests for workspace path/I-O primitives."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from typing import Any, cast

import pytest

from dagvane.domain.models import StorageError
from dagvane.workspace import paths as paths_module
from dagvane.workspace.paths import (
    Workspace,
    append_jsonl,
    atomic_write_bytes,
    atomic_write_bytes_at,
    ensure_expected_descendant,
    read_json,
    read_jsonl,
)


def test_root_itself_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        ensure_expected_descendant(tmp_path, tmp_path)


def test_parent_of_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        ensure_expected_descendant(tmp_path, tmp_path.parent)


def test_traversal_component_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageError):
        ensure_expected_descendant(tmp_path, tmp_path / "child" / ".." / ".." / "escape")


def test_same_string_prefix_sibling_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    sibling = tmp_path / "rootling" / "x"
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, sibling)


def test_absolute_outside_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, Path("/etc/passwd"))


def test_relative_target_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, Path("child"))


def test_existing_intermediate_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "mid"
    link.symlink_to(outside)
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, link / "child")


def test_leaf_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside_file = tmp_path / "secret"
    outside_file.write_text("secret")
    leaf = root / "leaf"
    leaf.symlink_to(outside_file)
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, leaf)


def test_valid_missing_descendant_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    ensure_expected_descendant(root, root / "a" / "b")


def test_symlinked_allowed_root_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real_root"
    real_root.mkdir()
    root_link = tmp_path / "root_link"
    root_link.symlink_to(real_root)
    with pytest.raises(StorageError):
        ensure_expected_descendant(root_link, root_link / "missing" / "child")


def test_missing_allowed_root_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "does_not_exist"
    with pytest.raises(StorageError):
        ensure_expected_descendant(root, root / "child")


def test_collection_root_escaping_trusted_parent_is_rejected(tmp_path: Path) -> None:
    trusted_parent = tmp_path / "parent"
    trusted_parent.mkdir()
    escaping_collection = tmp_path / "elsewhere" / "collection"
    with pytest.raises(StorageError):
        ensure_expected_descendant(trusted_parent, escaping_collection)


def test_atomic_write_rejects_predictable_tmp_symlink_sentinel(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    sentinel = tmp_path / "outside_via_tmp"
    sentinel.write_bytes(b"untouched")
    predictable_tmp = tmp_path / (target.name + ".tmp")
    predictable_tmp.symlink_to(sentinel)

    atomic_write_bytes(target, b"payload")

    assert target.read_bytes() == b"payload"
    assert sentinel.read_bytes() == b"untouched"
    assert not predictable_tmp.exists() or predictable_tmp.is_symlink()
    leftover = [
        p
        for p in tmp_path.iterdir()
        if p != predictable_tmp
        and p.name.startswith("state.json.")
        and p.name.endswith(".tmp")
    ]
    assert leftover == []


def test_atomic_write_rejects_destination_symlink_sentinel(tmp_path: Path) -> None:
    sentinel = tmp_path / "outside_via_dest"
    sentinel.write_bytes(b"untouched")
    target = tmp_path / "state.json"
    target.symlink_to(sentinel)

    atomic_write_bytes(target, b"payload")

    # os.replace onto a symlink replaces the link itself, never the target.
    assert sentinel.read_bytes() == b"untouched"
    assert not target.is_symlink()
    assert target.read_bytes() == b"payload"


def test_atomic_write_with_allowed_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    with pytest.raises(StorageError):
        atomic_write_bytes(outside, b"{}", allowed_root=root)
    assert not outside.exists()


def test_append_jsonl_rejects_symlink_leaf_with_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sentinel = tmp_path / "outside.jsonl"
    sentinel.write_bytes(b"untouched\n")
    link = root / "log.jsonl"
    link.symlink_to(sentinel)

    with pytest.raises(StorageError):
        append_jsonl(link, {"a": 1}, allowed_root=root)
    assert sentinel.read_bytes() == b"untouched\n"


def test_read_json_rejects_symlink_leaf_with_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sentinel = tmp_path / "outside.json"
    sentinel.write_text('{"a": 1}')
    link = root / "state.json"
    link.symlink_to(sentinel)

    with pytest.raises(StorageError):
        read_json(link, allowed_root=root)


def test_read_jsonl_rejects_symlink_leaf_with_allowed_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sentinel = tmp_path / "outside.jsonl"
    sentinel.write_text('{"a": 1}\n')
    link = root / "log.jsonl"
    link.symlink_to(sentinel)

    with pytest.raises(StorageError):
        read_jsonl(link, allowed_root=root)


def test_append_jsonl_parent_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    saved_parent = tmp_path / "nested-original"
    root.mkdir()
    outside.mkdir()
    outside_target = outside / "events.jsonl"
    outside_target.write_bytes(b"sentinel\n")

    real_mkdir = Path.mkdir
    swapped = False

    def mkdir_then_swap(self: Path, *args: object, **kwargs: object) -> None:
        nonlocal swapped
        cast(Any, real_mkdir)(self, *args, **kwargs)
        if self == parent and not swapped:
            swapped = True
            parent.rename(saved_parent)
            parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(Path, "mkdir", mkdir_then_swap)
    with pytest.raises(StorageError):
        append_jsonl(parent / "events.jsonl", {"attacker": True}, allowed_root=root)

    assert swapped
    assert outside_target.read_bytes() == b"sentinel\n"
    assert not (saved_parent / "events.jsonl").exists()


@pytest.mark.parametrize("reader", [read_json, read_jsonl], ids=["json", "jsonl"])
def test_read_json_parent_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: Any
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    saved_parent = tmp_path / "nested-original"
    root.mkdir()
    parent.mkdir()
    outside.mkdir()
    target = parent / "state.json"
    target.write_text('{"owned": true}\n', encoding="utf-8")
    (outside / target.name).write_text('{"foreign": true}\n', encoding="utf-8")
    real_open = os.open
    swapped = False

    def open_then_swap(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == parent and flags & os.O_DIRECTORY and not swapped:
            swapped = True
            parent.rename(saved_parent)
            parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", open_then_swap)
    with pytest.raises(StorageError):
        reader(target, allowed_root=root)

    assert swapped
    assert (saved_parent / target.name).read_text(encoding="utf-8") == '{"owned": true}\n'
    assert (outside / target.name).read_text(encoding="utf-8") == '{"foreign": true}\n'


def test_workspace_ensure_rejects_dagvane_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside_dagvane"
    outside.mkdir()
    (project / ".dagvane").symlink_to(outside)

    with pytest.raises(StorageError):
        Workspace(project).ensure()


def test_workspace_ensure_rejects_collection_symlink_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".dagvane").mkdir(parents=True)
    outside = tmp_path / "outside_goals"
    outside.mkdir()
    (project / ".dagvane" / "goals").symlink_to(outside)

    with pytest.raises(StorageError):
        Workspace(project).ensure()


def test_workspace_ensure_rejects_hostile_gitignore_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".dagvane").mkdir(parents=True)
    sentinel = tmp_path / "outside_gitignore"
    sentinel.write_bytes(b"untouched")
    (project / ".dagvane" / ".gitignore").symlink_to(sentinel)

    with pytest.raises(StorageError):
        Workspace(project).ensure()
    assert sentinel.read_bytes() == b"untouched"


def test_workspace_ensure_resolves_symlinked_root_safely(tmp_path: Path) -> None:
    real_project = tmp_path / "real_project"
    real_project.mkdir()
    project_link = tmp_path / "project_link"
    project_link.symlink_to(real_project)

    workspace = Workspace(project_link)
    workspace.ensure()

    assert workspace.root == real_project
    assert workspace.state_dir.is_dir()
    assert workspace.state_dir.parent == real_project


def test_workspace_ensure_normal_lifecycle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace(project)

    workspace.ensure()

    assert workspace.state_dir.is_dir()
    assert workspace.conversations_dir.is_dir()
    assert workspace.goals_dir.is_dir()
    assert workspace.agent_runs_dir.is_dir()
    assert workspace.worktrees_dir.is_dir()
    gitignore = workspace.state_dir / ".gitignore"
    assert gitignore.read_bytes() == b"*\n"

    # Idempotent re-ensure does not fail and does not disturb the gitignore.
    workspace.ensure()
    assert gitignore.read_bytes() == b"*\n"


# =============================================================================
# atomic_write_bytes_at: dirfd-relative fixed-leaf writes.
# =============================================================================


def test_atomic_write_bytes_at_rejects_non_simple_leaf_names(tmp_path: Path) -> None:
    real_dir = tmp_path / "pinned"
    real_dir.mkdir()
    outside = tmp_path / "outside.txt"
    dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for bad_name in (
            "",
            ".",
            "..",
            "a/b",
            "../escape.txt",
            "sub/escape.txt",
            "a\\b",
            str(outside),  # absolute
            "/abs.txt",
        ):
            with pytest.raises(StorageError):
                atomic_write_bytes_at(dir_fd, bad_name, b"data")
        # Nothing was ever written anywhere, inside or outside the pinned dir.
        assert list(real_dir.iterdir()) == []
        assert not outside.exists()
    finally:
        os.close(dir_fd)


def test_atomic_write_bytes_at_writes_only_inside_pinned_directory(
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "pinned"
    real_dir.mkdir()
    dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_write_bytes_at(dir_fd, "leaf.txt", b"hello")
        assert (real_dir / "leaf.txt").read_bytes() == b"hello"
        assert list(real_dir.iterdir()) == [real_dir / "leaf.txt"]
    finally:
        os.close(dir_fd)


def test_atomic_write_bytes_at_cleans_up_and_does_not_leak_fd_on_fdopen_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dir = tmp_path / "pinned"
    real_dir.mkdir()
    dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY)

    captured: dict[str, int] = {}
    proc_fd_dir = Path(f"/proc/{os.getpid()}/fd")
    has_procfs = proc_fd_dir.is_dir()

    def _boom(fd: int, mode: str) -> None:
        # Leave the descriptor open, exactly as a real os.fdopen failure
        # would (fdopen never closes on a failed wrap): the helper itself
        # is responsible for closing it, not this stand-in.
        captured["fd"] = fd
        raise OSError("synthetic fdopen failure")

    before = set(proc_fd_dir.iterdir()) if has_procfs else set()
    monkeypatch.setattr(os, "fdopen", _boom)

    try:
        with pytest.raises(StorageError):
            atomic_write_bytes_at(dir_fd, "leaf.txt", b"data")
    finally:
        monkeypatch.undo()

    assert "fd" in captured
    leaked_fd = captured["fd"]
    # The helper must have closed the fd it opened before raising: fstat on
    # it now fails with a bad-file-descriptor error, not silently succeeds.
    with pytest.raises(OSError):
        os.fstat(leaked_fd)

    if has_procfs:
        after = set(proc_fd_dir.iterdir())
        # No net-new fd survives beyond the dir_fd this test itself owns
        # (readdir's own transient fd aside): specifically, the leaked
        # candidate fd must not still be listed as open.
        assert not any(entry.name == str(leaked_fd) for entry in after - before)

    # No leftover temp leaf, no destination leaf.
    assert list(real_dir.iterdir()) == []
    os.close(dir_fd)


def test_atomic_write_create_replace_fsyncs_parent_and_leaks_no_fds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    proc_fd_dir = Path(f"/proc/{os.getpid()}/fd")
    before = set(os.listdir(proc_fd_dir)) if proc_fd_dir.is_dir() else set()
    real_fsync = os.fsync
    fsynced_directories = 0

    def recording_fsync(fd: int) -> None:
        nonlocal fsynced_directories
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            fsynced_directories += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    atomic_write_bytes(target, b"created")
    atomic_write_bytes(target, b"replaced")

    assert target.read_bytes() == b"replaced"
    assert fsynced_directories >= 2
    assert list(tmp_path.iterdir()) == [target]
    if proc_fd_dir.is_dir():
        assert set(os.listdir(proc_fd_dir)) == before


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_atomic_write_refuses_parent_replacement_while_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "state.json"
    target.write_bytes(b"original")
    moved_parent = tmp_path / "parent-original"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_target = outside / target.name
    outside_target.write_bytes(b"unrelated")
    real_open = os.open
    fired = False

    def swapping_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal fired
        if not fired and path == parent and flags & os.O_DIRECTORY:
            fired = True
            parent.rename(moved_parent)
            if replacement_kind == "directory":
                parent.mkdir()
                (parent / target.name).write_bytes(b"replacement")
            else:
                parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(StorageError):
        atomic_write_bytes(target, b"new", allowed_root=tmp_path)

    assert fired
    assert (moved_parent / target.name).read_bytes() == b"original"
    assert outside_target.read_bytes() == b"unrelated"
    if replacement_kind == "directory":
        assert (parent / target.name).read_bytes() == b"replacement"


@pytest.mark.parametrize("replacement_kind", ["directory", "symlink"])
def test_atomic_write_rolls_back_if_parent_changes_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "state.json"
    target.write_bytes(b"original")
    moved_parent = tmp_path / "parent-original"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    (replacement / target.name).write_bytes(b"unrelated")
    real_renameat2 = paths_module._renameat2
    fired = False

    def swapping_renameat2(
        old_dir_fd: int,
        old_name: str,
        new_dir_fd: int,
        new_name: str,
        flags: int,
    ) -> None:
        nonlocal fired
        if not fired and new_name == target.name:
            fired = True
            parent.rename(moved_parent)
            if replacement_kind == "directory":
                replacement.rename(parent)
            else:
                outside = tmp_path / "outside"
                replacement.rename(outside)
                parent.symlink_to(outside, target_is_directory=True)
        real_renameat2(old_dir_fd, old_name, new_dir_fd, new_name, flags)

    monkeypatch.setattr(paths_module, "_renameat2", swapping_renameat2)

    with pytest.raises(StorageError, match="parent identity changed"):
        atomic_write_bytes(target, b"new", allowed_root=tmp_path)

    assert fired
    assert (moved_parent / target.name).read_bytes() == b"original"
    assert (parent / target.name).read_bytes() == b"unrelated"
    assert sorted(path.name for path in moved_parent.iterdir()) == [target.name]


def test_atomic_write_create_refuses_leaf_collision_at_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    real_renameat2 = paths_module._renameat2
    fired = False

    def colliding_renameat2(
        old_dir_fd: int,
        old_name: str,
        new_dir_fd: int,
        new_name: str,
        flags: int,
    ) -> None:
        nonlocal fired
        if not fired and new_name == target.name:
            fired = True
            target.write_bytes(b"unrelated collision")
        real_renameat2(old_dir_fd, old_name, new_dir_fd, new_name, flags)

    monkeypatch.setattr(paths_module, "_renameat2", colliding_renameat2)

    with pytest.raises(StorageError):
        atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"unrelated collision"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_replace_refuses_leaf_identity_change_at_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"original")
    original_saved = tmp_path / "original-saved"
    real_renameat2 = paths_module._renameat2
    fired = False

    def colliding_renameat2(
        old_dir_fd: int,
        old_name: str,
        new_dir_fd: int,
        new_name: str,
        flags: int,
    ) -> None:
        nonlocal fired
        if not fired and new_name == target.name:
            fired = True
            target.rename(original_saved)
            target.write_bytes(b"unrelated collision")
        real_renameat2(old_dir_fd, old_name, new_dir_fd, new_name, flags)

    monkeypatch.setattr(paths_module, "_renameat2", colliding_renameat2)

    with pytest.raises(StorageError, match="identity changed"):
        atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"unrelated collision"
    assert original_saved.read_bytes() == b"original"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "original-saved",
        target.name,
    ]


def test_atomic_write_preserves_temp_collision_and_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"original")
    token = "fixed-token"
    collision = tmp_path / f".{target.name}.{token}.tmp"
    collision.write_bytes(b"unrelated temp")
    monkeypatch.setattr(secrets, "token_hex", lambda _size: token)

    with pytest.raises(StorageError, match="temporary leaf"):
        atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"original"
    assert collision.read_bytes() == b"unrelated temp"


def test_atomic_write_parent_fsync_failure_rolls_back_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"original")
    real_fsync = os.fsync
    failed = False

    def failing_parent_fsync(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError("synthetic parent fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_parent_fsync)

    with pytest.raises(StorageError, match="synthetic parent fsync failure"):
        atomic_write_bytes(target, b"new")

    assert failed
    assert target.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [target]
