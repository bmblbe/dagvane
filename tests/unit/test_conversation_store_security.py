"""Security regressions binding Conversation identity to storage path.

Every Conversation id must survive `validate_conversation_id` (the shared
filesystem-id contract, plus rejection of the reserved `"current"` value)
before it ever touches the filesystem. Any on-disk object that does not match
the identity it was requested under (symlink, mismatched manifest identity,
corrupt pointer) must fail closed with `SpecError`/`StorageError` rather than
being silently treated as absent or repaired.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dagvane.application.chat import ConversationStore
from dagvane.domain.models import SpecError, StorageError
from dagvane.ports.runtime import FixedClock
from dagvane.workspace.paths import Workspace


class _FixedIds:
    """Deterministic/malicious IdSource double: returns a queued value."""

    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def new_id(self, kind: str) -> str:
        return self._values.pop(0)


def _workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "project"
    root.mkdir()
    workspace = Workspace(root)
    workspace.ensure()
    return workspace


def _clock() -> FixedClock:
    return FixedClock(start="2026-08-16T00:00:00.000Z", step_ms=1000)


def _store(
    tmp_path: Path, *, ids_values: list[str] | None = None
) -> tuple[ConversationStore, Workspace, FixedClock, _FixedIds]:
    workspace = _workspace(tmp_path)
    clock = _clock()
    ids = _FixedIds(ids_values or ["conv-a"])
    return ConversationStore(workspace, clock, ids), workspace, clock, ids


def _sentinels(tmp_path: Path, workspace: Workspace) -> dict[str, Path]:
    """Bytes planted outside, at root/parent, and at a same-prefix sibling of
    the conversations dir — none of these must ever be touched by a negative
    path."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_bytes(b"outside")
    root_secret = workspace.root / "root-secret.txt"
    root_secret.write_bytes(b"root")
    parent_secret = tmp_path / "parent-secret.txt"
    parent_secret.write_bytes(b"parent")
    sibling_dir = workspace.state_dir / "conversationsX"
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


def _manifest_path(workspace: Workspace, conversation_id: str) -> Path:
    return workspace.conversations_dir / conversation_id / "manifest.json"


# -- invalid ids, before any effect ------------------------------------------


@pytest.mark.parametrize(
    "bad_id",
    [
        "/etc/passwd",
        "../escape",
        "a/b",
        "a\\b",
        "café",
        "id\x00null",
        "id\nnewline",
        "",
        "a" * 65,
        "current",
    ],
)
def test_invalid_id_rejected_before_effect(tmp_path: Path, bad_id: str) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    sentinels = _sentinels(tmp_path, workspace)
    with pytest.raises(SpecError):
        store.exists(bad_id)
    with pytest.raises(SpecError):
        store.messages(bad_id)
    with pytest.raises(SpecError):
        store.append_message(bad_id, "user", "hi")
    with pytest.raises(SpecError):
        store.record_session_ref(bad_id, "codex", "sess-1")
    with pytest.raises(SpecError):
        store.set_current(bad_id)
    assert list(workspace.conversations_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_invalid_non_string_id_rejected(tmp_path: Path) -> None:
    store, _workspace_, _clock_, _ids_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.exists(123)  # type: ignore[arg-type]


def test_current_reserved_value_never_becomes_a_conversation(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.exists("current")
    with pytest.raises(SpecError):
        store.set_current("current")
    assert list(workspace.conversations_dir.iterdir()) == []


# -- malicious/broken IdSource: generated invalid id fails before effect ----


@pytest.mark.parametrize("bad_generated", ["current", "../escape", "a/b", ""])
def test_create_rejects_invalid_generated_id_before_effect(
    tmp_path: Path, bad_generated: str
) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path, ids_values=[bad_generated])
    sentinels = _sentinels(tmp_path, workspace)
    with pytest.raises(SpecError):
        store.create()
    assert list(workspace.conversations_dir.iterdir()) == []
    _assert_sentinels_untouched(sentinels)


def test_create_rejects_colliding_generated_id(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path, ids_values=["conv-a", "conv-a"])
    first = store.create()
    assert first == "conv-a"
    with pytest.raises(StorageError):
        store.create()


# -- valid create/use/append/list/current flows remain functional ----------


def test_create_use_append_list_current_roundtrip(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(
        tmp_path, ids_values=["conv-a", "conv-b"]
    )
    conv_a = store.create(title="first")
    assert conv_a == "conv-a"
    assert store.current() == "conv-a"
    assert store.exists("conv-a") is True

    store.append_message("conv-a", "user", "hello there")
    store.append_message("conv-a", "assistant", "hi!")
    messages = store.messages("conv-a")
    assert [m["role"] for m in messages] == ["user", "assistant"]

    store.record_session_ref("conv-a", "codex", "sess-xyz")
    manifest = json.loads(_manifest_path(workspace, "conv-a").read_text(encoding="utf-8"))
    assert manifest["session_refs"] == {"codex": "sess-xyz"}

    conv_b = store.create(title="second")
    assert conv_b == "conv-b"
    assert store.current() == "conv-b"
    store.set_current("conv-a")
    assert store.current() == "conv-a"

    infos = {info.conversation_id: info for info in store.list()}
    assert set(infos) == {"conv-a", "conv-b"}
    assert infos["conv-a"].messages == 2
    assert infos["conv-a"].title == "first"


# -- truly absent conversation is ordinary false/None/unknown ---------------


def test_truly_absent_conversation_is_ordinary(tmp_path: Path) -> None:
    store, _workspace_, _clock_, _ids_ = _store(tmp_path)
    assert store.exists("conv-nope") is False
    assert store.current() is None
    with pytest.raises(SpecError):
        store.messages("conv-nope")
    with pytest.raises(SpecError):
        store.set_current("conv-nope")


# -- bare conversation directory (no manifest.json) is existing malformed   --
# -- storage, not absence --------------------------------------------------


def test_bare_conversation_directory_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    bare_dir = workspace.conversations_dir / "conv-bare"
    bare_dir.mkdir()
    with pytest.raises(StorageError):
        store.exists("conv-bare")
    with pytest.raises(StorageError):
        store.messages("conv-bare")
    with pytest.raises(StorageError):
        store.set_current("conv-bare")
    with pytest.raises(StorageError):
        store.append_message("conv-bare", "user", "hi")
    assert list(bare_dir.iterdir()) == []


# -- "current" entry that is a directory is corruption, not a valid pointer -


def test_current_rejects_directory_named_current(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    (workspace.conversations_dir / "current").mkdir()
    with pytest.raises(StorageError):
        store.current()


# -- manifest identity: missing/non-string/valid-different/case-different ---


def _create_conv(store: ConversationStore) -> str:
    conversation_id = store.create()
    return conversation_id


@pytest.mark.parametrize(
    "bad_identity",
    [None, 12345, "conv-other", "Conv-A", "a/b"],
)
def test_load_entry_points_reject_bad_manifest_identity(
    tmp_path: Path, bad_identity: object
) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    manifest_path = _manifest_path(workspace, conversation_id)
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    if bad_identity is None:
        del doc["conversation_id"]
    else:
        doc["conversation_id"] = bad_identity
    tampered_bytes = json.dumps(doc)
    manifest_path.write_text(tampered_bytes, encoding="utf-8")

    with pytest.raises(SpecError):
        store.exists(conversation_id)
    with pytest.raises(SpecError):
        store.messages(conversation_id)
    with pytest.raises(SpecError):
        store.append_message(conversation_id, "user", "hi")
    with pytest.raises(SpecError):
        store.record_session_ref(conversation_id, "codex", "sess-1")
    with pytest.raises(SpecError):
        store.set_current(conversation_id)
    with pytest.raises(SpecError):
        store.list()
    assert manifest_path.read_text(encoding="utf-8") == tampered_bytes


# -- symlink sentinels: conversations root, conversation dir, manifest,     --
# -- messages, pointer -------------------------------------------------------


def test_symlinked_conversations_root_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    real_dir = workspace.conversations_dir
    real_dir.rmdir()
    outside = tmp_path / "outside-conversations"
    outside.mkdir()
    real_dir.symlink_to(outside, target_is_directory=True)
    store = ConversationStore(workspace, clock, _FixedIds(["conv-a"]))
    with pytest.raises(StorageError):
        store.list()
    with pytest.raises(StorageError):
        store.exists("conv-a")
    assert list(outside.iterdir()) == []


def test_symlinked_conversation_directory_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    sentinels = _sentinels(tmp_path, workspace)
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (workspace.conversations_dir / "conv-a").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError):
        store.exists("conv-a")
    with pytest.raises(StorageError):
        store.messages("conv-a")
    with pytest.raises(StorageError):
        store.list()
    _assert_sentinels_untouched(sentinels)
    assert list(outside.iterdir()) == []


def test_symlinked_manifest_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    manifest_path = _manifest_path(workspace, conversation_id)
    manifest_path.unlink()
    target = tmp_path / "outside-manifest.json"
    target.write_bytes(b'{"conversation_id": "conv-a"}')
    manifest_path.symlink_to(target)
    with pytest.raises(StorageError):
        store.exists(conversation_id)
    with pytest.raises(StorageError):
        store.messages(conversation_id)
    with pytest.raises(StorageError):
        store.append_message(conversation_id, "user", "hi")
    with pytest.raises(StorageError):
        store.list()
    assert target.read_bytes() == b'{"conversation_id": "conv-a"}'
    assert manifest_path.is_symlink()


def test_symlinked_messages_jsonl_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    messages_path = workspace.conversations_dir / conversation_id / "messages.jsonl"
    target = tmp_path / "outside-messages.jsonl"
    target.write_bytes(b"tampered\n")
    messages_path.symlink_to(target)
    with pytest.raises(StorageError):
        store.messages(conversation_id)
    with pytest.raises(StorageError):
        store.append_message(conversation_id, "user", "hi")
    assert target.read_bytes() == b"tampered\n"
    assert messages_path.is_symlink()


def test_symlinked_current_pointer_fails_closed(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    pointer_path = workspace.conversations_dir / "current"
    pointer_path.unlink()
    target = tmp_path / "outside-current"
    target.write_bytes(f"{conversation_id}\n".encode())
    pointer_path.symlink_to(target)
    with pytest.raises(StorageError):
        store.current()
    with pytest.raises(StorageError):
        store.set_current(conversation_id)
    with pytest.raises(StorageError):
        store.list()
    assert target.read_bytes() == f"{conversation_id}\n".encode()
    assert pointer_path.is_symlink()


# -- pointer grammar: absent vs corrupt, unknown target, preserved on fail --


def test_current_is_none_when_pointer_absent(tmp_path: Path) -> None:
    store, _workspace_, _clock_, _ids_ = _store(tmp_path)
    assert store.current() is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"conv-a",  # missing trailing newline
        b"conv-a\nconv-b\n",  # multiline
        b"conv-a \n",  # whitespace padding
        b" conv-a\n",
        b"conv-a\n\n",
        b"../escape\n",
        b"current\n",
    ],
)
def test_current_rejects_corrupt_pointer_content(tmp_path: Path, raw: bytes) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    pointer_path = workspace.conversations_dir / "current"
    pointer_path.write_bytes(raw)
    with pytest.raises((SpecError, StorageError)):
        store.current()
    assert pointer_path.read_bytes() == raw


def test_current_rejects_unknown_target(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    pointer_path = workspace.conversations_dir / "current"
    pointer_path.write_bytes(b"conv-ghost\n")
    with pytest.raises(SpecError):
        store.current()


def test_set_current_preserves_old_pointer_on_failure(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path, ids_values=["conv-a"])
    conversation_id = store.create()
    pointer_path = workspace.conversations_dir / "current"
    before = pointer_path.read_bytes()
    with pytest.raises(SpecError):
        store.set_current("conv-ghost")
    assert pointer_path.read_bytes() == before
    assert store.current() == conversation_id


def test_set_current_reserved_current_fails_and_preserves_pointer(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path, ids_values=["conv-a"])
    store.create()
    pointer_path = workspace.conversations_dir / "current"
    before = pointer_path.read_bytes()
    with pytest.raises(SpecError):
        store.set_current("current")
    assert pointer_path.read_bytes() == before


# -- list(): invalid/malformed entries fail closed, reserved pointer skipped


def test_list_fails_closed_on_invalid_directory_name(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    bad_dir = workspace.conversations_dir / "..bad"
    bad_dir.mkdir()
    (bad_dir / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SpecError):
        store.list()


def test_list_fails_closed_on_dir_with_no_manifest(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    empty_dir = workspace.conversations_dir / "conv-empty"
    empty_dir.mkdir()
    with pytest.raises(StorageError):
        store.list()


def test_list_rejects_stray_file_in_conversations_dir(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    (workspace.conversations_dir / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(StorageError):
        store.list()


def test_list_skips_only_the_regular_current_pointer(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path, ids_values=["conv-a"])
    store.create()
    infos = store.list()
    assert [info.conversation_id for info in infos] == ["conv-a"]


def test_list_fails_closed_when_current_entry_is_a_directory(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    (workspace.conversations_dir / "current").mkdir()
    with pytest.raises(StorageError):
        store.list()


# -- negative paths never leave temp artifacts -------------------------------


def test_negative_append_message_on_unknown_conversation_leaves_no_artifact(
    tmp_path: Path,
) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.append_message("conv-ghost", "user", "hi")
    conv_dir = workspace.conversations_dir / "conv-ghost"
    assert not conv_dir.exists()


class _CountingClock:
    def __init__(self, delegate: FixedClock) -> None:
        self._delegate = delegate
        self.calls = 0

    def now_iso(self) -> str:
        self.calls += 1
        return self._delegate.now_iso()


def test_negative_append_message_does_not_advance_clock(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _CountingClock(_clock())
    store = ConversationStore(workspace, clock, _FixedIds([]))
    with pytest.raises(SpecError):
        store.append_message("conv-ghost", "user", "hi")
    assert clock.calls == 0
    messages_path = workspace.conversations_dir / "conv-ghost" / "messages.jsonl"
    assert not messages_path.exists()


def test_negative_record_session_ref_on_unknown_conversation_leaves_no_artifact(
    tmp_path: Path,
) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    with pytest.raises(SpecError):
        store.record_session_ref("conv-ghost", "codex", "sess-1")
    conv_dir = workspace.conversations_dir / "conv-ghost"
    assert not conv_dir.exists()


# -- create() preflights the current pointer before any clock/manifest effect

def _assert_create_refused(
    tmp_path: Path,
    workspace: Workspace,
    clock: _CountingClock,
    ids_values: list[str],
    exc_type: type[Exception] = StorageError,
) -> None:
    store = ConversationStore(workspace, clock, _FixedIds(ids_values))
    before_entries = sorted(p.name for p in workspace.conversations_dir.iterdir())
    with pytest.raises(exc_type):
        store.create()
    assert clock.calls == 0
    after_entries = sorted(p.name for p in workspace.conversations_dir.iterdir())
    assert after_entries == before_entries
    for name in after_entries:
        entry = workspace.conversations_dir / name
        if entry.is_dir() and not entry.is_symlink():
            assert name != "conv-a"


def test_create_refused_when_pointer_is_symlink(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside-current"
    outside.write_bytes(b"conv-ghost\n")
    (workspace.conversations_dir / "current").symlink_to(outside)
    _assert_create_refused(tmp_path, workspace, _CountingClock(_clock()), ["conv-a"])
    assert outside.read_bytes() == b"conv-ghost\n"


def test_create_refused_when_pointer_is_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.conversations_dir / "current").mkdir()
    _assert_create_refused(tmp_path, workspace, _CountingClock(_clock()), ["conv-a"])


def test_create_refused_when_pointer_is_corrupt_regular(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    pointer_path = workspace.conversations_dir / "current"
    pointer_path.write_bytes(b"conv-a \n")
    _assert_create_refused(
        tmp_path, workspace, _CountingClock(_clock()), ["conv-b"], exc_type=SpecError
    )
    assert pointer_path.read_bytes() == b"conv-a \n"


def test_create_refused_when_pointer_targets_unknown_conversation(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    pointer_path = workspace.conversations_dir / "current"
    pointer_path.write_bytes(b"conv-ghost\n")
    _assert_create_refused(
        tmp_path, workspace, _CountingClock(_clock()), ["conv-a"], exc_type=SpecError
    )
    assert pointer_path.read_bytes() == b"conv-ghost\n"


def test_create_refused_when_pointer_target_has_mismatched_manifest(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    clock = _clock()
    setup_store = ConversationStore(workspace, clock, _FixedIds(["conv-a"]))
    existing = setup_store.create()
    manifest_path = _manifest_path(workspace, existing)
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["conversation_id"] = "conv-other"
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    pointer_path = workspace.conversations_dir / "current"
    pointer_before = pointer_path.read_bytes()

    counting_clock = _CountingClock(_clock())
    store = ConversationStore(workspace, counting_clock, _FixedIds(["conv-b"]))
    with pytest.raises(SpecError):
        store.create()
    assert counting_clock.calls == 0
    assert pointer_path.read_bytes() == pointer_before
    assert not (workspace.conversations_dir / "conv-b").exists()


def test_create_allowed_when_pointer_absent(tmp_path: Path) -> None:
    store, workspace, clock, _ids_ = _store(tmp_path)
    conversation_id = store.create()
    assert conversation_id == "conv-a"
    assert store.current() == "conv-a"


def test_create_allowed_when_pointer_is_valid_and_advances_atomically(tmp_path: Path) -> None:
    store, workspace, clock, _ids_ = _store(tmp_path, ids_values=["conv-a", "conv-b"])
    first = store.create()
    assert first == "conv-a"
    second = store.create()
    assert second == "conv-b"
    assert store.current() == "conv-b"


# -- transcript validation is strict, fail-closed, and pre-clock -----------


def _messages_path(workspace: Workspace, conversation_id: str) -> Path:
    return workspace.conversations_dir / conversation_id / "messages.jsonl"


@pytest.mark.parametrize(
    "raw",
    [
        b"null\n",
        b"1\n",
        b'"just a string"\n',
        b"[1, 2]\n",
        b"{bad json\n",
        b"\xff\xfe not utf8\n",
    ],
)
def test_messages_rejects_corrupt_transcript_row(tmp_path: Path, raw: bytes) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    _messages_path(workspace, conversation_id).write_bytes(raw)
    with pytest.raises(StorageError):
        store.messages(conversation_id)
    with pytest.raises(StorageError):
        store.list()


def test_messages_rejects_directory_transcript(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    messages_path = _messages_path(workspace, conversation_id)
    messages_path.mkdir()
    with pytest.raises(StorageError):
        store.messages(conversation_id)


def test_append_message_rejects_corrupt_transcript_before_clock_or_write(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    setup_store = ConversationStore(workspace, _clock(), _FixedIds(["conv-a"]))
    conversation_id = setup_store.create()
    messages_path = _messages_path(workspace, conversation_id)
    messages_path.write_bytes(b"not json\n")
    before = messages_path.read_bytes()
    manifest_path = _manifest_path(workspace, conversation_id)
    manifest_before = manifest_path.read_bytes()

    counting_clock = _CountingClock(_clock())
    store = ConversationStore(workspace, counting_clock, _FixedIds([]))
    with pytest.raises(StorageError):
        store.append_message(conversation_id, "user", "hi")
    assert counting_clock.calls == 0
    assert messages_path.read_bytes() == before
    assert manifest_path.read_bytes() == manifest_before


def test_append_message_rejects_symlinked_transcript_before_clock(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    setup_store = ConversationStore(workspace, _clock(), _FixedIds(["conv-a"]))
    conversation_id = setup_store.create()
    messages_path = _messages_path(workspace, conversation_id)
    target = tmp_path / "outside-messages.jsonl"
    target.write_bytes(b"tampered\n")
    messages_path.symlink_to(target)
    manifest_path = _manifest_path(workspace, conversation_id)
    manifest_before = manifest_path.read_bytes()

    counting_clock = _CountingClock(_clock())
    store = ConversationStore(workspace, counting_clock, _FixedIds([]))
    with pytest.raises(StorageError):
        store.append_message(conversation_id, "user", "hi")
    assert counting_clock.calls == 0
    assert target.read_bytes() == b"tampered\n"
    assert manifest_path.read_bytes() == manifest_before


def test_messages_valid_and_missing_transcript_unaffected(tmp_path: Path) -> None:
    store, workspace, _clock_, _ids_ = _store(tmp_path)
    conversation_id = _create_conv(store)
    assert store.messages(conversation_id) == []
    store.append_message(conversation_id, "user", "hello")
    assert [m["role"] for m in store.messages(conversation_id)] == ["user"]


def test_create_via_full_composition_refuses_hostile_pointer(tmp_path: Path) -> None:
    """Exercises the normal ``Workspace.ensure()`` composition path so the
    regression models a real fresh invocation, not only a hand-built store."""
    from dagvane.cli_workspace import Composition

    root = tmp_path / "project"
    root.mkdir()
    comp = Composition(root)
    (comp.workspace.conversations_dir / "current").mkdir()
    with pytest.raises(StorageError):
        comp.conversations.create()
    assert list((comp.workspace.conversations_dir / "current").iterdir()) == []
    assert [
        p.name for p in comp.workspace.conversations_dir.iterdir() if p.is_dir()
    ] == ["current"]
