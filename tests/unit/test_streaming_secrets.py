"""StreamingSecretScrubber / SecretScrubber.open_stream unit tests (R1-B1)."""

from __future__ import annotations

import threading

import pytest

from dagvane.domain import secrets as secrets_module
from dagvane.domain.models import SpecError
from dagvane.domain.secrets import REDACTION_MARKER, SecretScrubber, StreamingSecretScrubber

SECRET = "sk-tr\"ick\\y'key-42"


def _new_scrubber(*values: str) -> SecretScrubber:
    scrubber = SecretScrubber()
    for value in values:
        scrubber.register(value)
    return scrubber


def _drain(
    stream: StreamingSecretScrubber, text: str, *, source_eof: bool = True
) -> tuple[str, bool]:
    out = stream.feed(text)
    tail, discarded = stream.finish(source_eof=source_eof)
    return out + tail, discarded


# --- split-position coverage across raw + every encoded rendering ------


@pytest.mark.parametrize("split", list(range(0, len(SECRET) + 8)))
def test_raw_secret_redacted_at_every_split_position(split: int) -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    text = f"prefix {SECRET} suffix"
    split = min(split, len(text))
    out = stream.feed(text[:split]) + stream.feed(text[split:])
    tail, discarded = stream.finish(source_eof=True)
    result = out + tail
    assert SECRET not in result
    assert REDACTION_MARKER in result
    assert discarded is False


def test_every_registered_rendering_redacted_at_every_split_position() -> None:
    scrubber = _new_scrubber(SECRET)
    variants = scrubber._variants
    assert len(variants) > 1  # sanity: raw + JSON/repr/unicode-escape forms
    for variant in variants:
        text = f"x{variant}y"
        for split in range(len(text) + 1):
            stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
            out = stream.feed(text[:split]) + stream.feed(text[split:])
            tail, _ = stream.finish(source_eof=True)
            result = out + tail
            assert variant not in result, (variant, split)
            assert SECRET not in result, (variant, split)


def test_one_char_at_a_time_feed_resolves_secret() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=100, max_variants=20)
    text = f"before {SECRET} after"
    out = "".join(stream.feed(ch) for ch in text)
    tail, discarded = stream.finish(source_eof=True)
    result = out + tail
    assert SECRET not in result
    assert discarded is False


# --- adjacent / overlapping variants, leftmost-longest -------------------


def test_overlapping_variants_take_longest_leftmost_match() -> None:
    scrubber = _new_scrubber("AB", "ABC", "BCX")
    stream = scrubber.open_stream(max_variant_chars=10, max_variants=10)
    out, discarded = _drain(stream, "zABCXq")
    assert out == f"z{REDACTION_MARKER}Xq"
    assert discarded is False


@pytest.mark.parametrize("split", list(range(len("zABCXq") + 1)))
def test_overlapping_variants_resolve_identically_at_every_split(split: int) -> None:
    # Regression: a chunk cut inside "ABC" must not let the shorter "AB"
    # match commit early — leftmost-longest may not depend on split position.
    scrubber = _new_scrubber("AB", "ABC", "BCX")
    stream = scrubber.open_stream(max_variant_chars=10, max_variants=10)
    text = "zABCXq"
    out = stream.feed(text[:split]) + stream.feed(text[split:])
    tail, discarded = stream.finish(source_eof=True)
    assert (out + tail) == f"z{REDACTION_MARKER}Xq", split
    assert discarded is False


def test_eof_confirms_a_shorter_variant_held_for_a_longer_one() -> None:
    # "AB" alone could still grow into "ABC", so feed holds it; at real EOF
    # it is a complete registered variant and must be redacted, not discarded.
    scrubber = _new_scrubber("AB", "ABC")
    stream = scrubber.open_stream(max_variant_chars=10, max_variants=10)
    out = stream.feed("zAB")
    assert out == "z"
    tail, discarded = stream.finish(source_eof=True)
    assert (out + tail) == f"z{REDACTION_MARKER}"
    assert discarded is False


def test_non_eof_finish_discards_a_held_ambiguous_short_match() -> None:
    scrubber = _new_scrubber("AB", "ABC")
    stream = scrubber.open_stream(max_variant_chars=10, max_variants=10)
    out = stream.feed("zAB")
    tail, discarded = stream.finish(source_eof=False)
    assert out == "z"
    assert tail == ""
    assert discarded is True


def test_immediately_adjacent_variants_both_redacted() -> None:
    scrubber = _new_scrubber("SECRETONE9", "SECRETTWO9")
    stream = scrubber.open_stream(max_variant_chars=20, max_variants=10)
    out, discarded = _drain(stream, "xSECRETONE9SECRETTWO9y")
    assert out == f"x{REDACTION_MARKER}{REDACTION_MARKER}y"
    assert discarded is False


# --- EOF vs non-EOF partial-suffix handling -------------------------------


def test_eof_redacts_a_complete_trailing_variant() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=100, max_variants=20)
    out = stream.feed(f"pre {SECRET}")
    tail, discarded = stream.finish(source_eof=True)
    assert (out + tail) == f"pre {REDACTION_MARKER}"
    assert discarded is False


def test_eof_discards_proper_prefix_suffix_and_reports_it() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=100, max_variants=20)
    partial = SECRET[: len(SECRET) - 3]
    out = stream.feed(f"pre {partial}")
    tail, discarded = stream.finish(source_eof=True)
    result = out + tail
    assert SECRET not in result
    assert partial not in result
    assert result == "pre "
    assert discarded is True


def test_non_eof_finish_never_flushes_carry() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=100, max_variants=20)
    partial = SECRET[: len(SECRET) - 3]
    out = stream.feed(f"pre {partial}")
    tail, discarded = stream.finish(source_eof=False)
    assert out == "pre "
    assert tail == ""
    assert discarded is True


def test_non_eof_finish_with_no_carry_reports_no_discard() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=100, max_variants=20)
    stream.feed("plain text, nothing secret-like at the end")
    tail, discarded = stream.finish(source_eof=False)
    assert tail == ""
    assert discarded is False


# --- registry mutation after open -----------------------------------------


def test_registry_mutation_after_open_fails_closed_on_feed() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    scrubber.register("a-brand-new-credential-000")
    with pytest.raises(SpecError, match="stale"):
        stream.feed("hello")


def test_registry_mutation_after_open_fails_closed_on_finish() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    stream.feed("hello ")
    scrubber.register("a-brand-new-credential-000")
    with pytest.raises(SpecError, match="stale"):
        stream.finish(source_eof=True)


def test_stale_detected_at_entry_clears_carry_and_closes_stream() -> None:
    # Regression: a stale feed()/finish() must not just fail its own call —
    # it must clear the buffered fragment and close the stream immediately,
    # so a caller that swallows the exception cannot later recover the
    # fragment or keep feeding into a stream whose variant snapshot no
    # longer matches the registry.
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    partial = SECRET[: len(SECRET) - 3]
    stream.feed(f"pre {partial}")
    assert stream._carry != ""  # sanity: a fragment is buffered awaiting more data

    scrubber.register("a-brand-new-credential-333")

    with pytest.raises(SpecError, match="stale"):
        stream.feed("more")
    assert stream._carry == ""
    assert stream._finished is True

    # The stream is now closed outright, not merely "still stale": every
    # further call fails the ordinary already-finished way.
    with pytest.raises(SpecError, match=r"finish\(\) called after finish\(\)"):
        stream.finish(source_eof=True)
    with pytest.raises(SpecError, match=r"feed\(\) called after finish\(\)"):
        stream.feed("x")


# --- register() races: linearization against feed()/finish()/scrub() ------
#
# These tests prove the MAJOR fix directly: register() cannot mutate the
# registry while a feed()/finish()/scrub() call is mid-scan holding the
# shared lock. Each test blocks the module-level ``_scan``/``_replace_all``
# seam mid-call (the exact window the race exploited — after a freshness
# check but before the scan it guards), starts a concurrent register() on a
# second thread, proves that thread cannot complete while the scan is
# in-flight, then releases the scan and confirms the racing registration
# lands cleanly afterward with no credential ever escaping unredacted.

_JOIN_TIMEOUT = 5.0


def _join_started_threads(*threads: threading.Thread) -> None:
    """Join threads that the test has already started at its exact race seam."""
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)
    live = [t for t in threads if t.is_alive()]
    assert not live, f"{len(live)} test thread(s) still alive after join timeout"


def test_register_cannot_mutate_registry_while_feed_scan_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    new_secret = "brand-new-credential-during-feed-000"

    scan_entered = threading.Event()
    release_scan = threading.Event()
    real_scan = secrets_module._scan

    def blocking_scan(*args: object, **kwargs: object) -> tuple[str, int]:
        scan_entered.set()
        assert release_scan.wait(timeout=_JOIN_TIMEOUT), "test deadlocked: scan never released"
        return real_scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_module, "_scan", blocking_scan)

    feed_result: dict[str, str] = {}

    def feed_target() -> None:
        feed_result["out"] = stream.feed(f"hello {new_secret} world")

    feed_thread = threading.Thread(target=feed_target)
    feed_thread.start()
    assert scan_entered.wait(timeout=_JOIN_TIMEOUT), "feed() never reached _scan"

    register_done = threading.Event()

    def register_target() -> None:
        scrubber.register(new_secret)
        register_done.set()

    register_thread = threading.Thread(target=register_target)
    register_thread.start()

    # While the scan holds the shared lock, register() must not be able to
    # acquire it and mutate the registry — prove it stays blocked.
    assert not register_done.wait(timeout=0.2)
    generation_during_scan = scrubber._generation

    release_scan.set()
    _join_started_threads(feed_thread, register_thread)

    assert register_done.is_set()
    assert scrubber._generation == generation_during_scan + 1
    # The in-flight feed() captured its variant snapshot strictly before the
    # registration could land, so it never redacts the not-yet-registered
    # secret — and, critically, it never partially/incorrectly redacts it
    # either (no mixed pre/post state).
    assert new_secret in feed_result["out"]

    # Post-race, the session is provably stale: it must fail closed rather
    # than silently scrub against the now-outdated snapshot.
    with pytest.raises(SpecError, match="stale"):
        stream.feed("more")
    assert stream._carry == ""
    assert stream._finished is True


def test_register_cannot_mutate_registry_while_finish_scan_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    # Leave a genuine unresolved fragment buffered so finish(source_eof=True)
    # has real work to do in ``_scan`` rather than returning on an empty carry.
    partial = SECRET[: len(SECRET) - 3]
    stream.feed(f"pre {partial}")
    new_secret = "brand-new-credential-during-finish-000"

    scan_entered = threading.Event()
    release_scan = threading.Event()
    real_scan = secrets_module._scan

    def blocking_scan(*args: object, **kwargs: object) -> tuple[str, int]:
        scan_entered.set()
        assert release_scan.wait(timeout=_JOIN_TIMEOUT), "test deadlocked: scan never released"
        return real_scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_module, "_scan", blocking_scan)

    finish_result: dict[str, tuple[str, bool]] = {}

    def finish_target() -> None:
        finish_result["out"] = stream.finish(source_eof=True)

    finish_thread = threading.Thread(target=finish_target)
    finish_thread.start()
    assert scan_entered.wait(timeout=_JOIN_TIMEOUT), "finish() never reached _scan"

    register_done = threading.Event()

    def register_target() -> None:
        scrubber.register(new_secret)
        register_done.set()

    register_thread = threading.Thread(target=register_target)
    register_thread.start()

    assert not register_done.wait(timeout=0.2)
    generation_during_scan = scrubber._generation

    release_scan.set()
    _join_started_threads(finish_thread, register_thread)

    assert register_done.is_set()
    assert scrubber._generation == generation_during_scan + 1
    # finish() legitimately completed under the pre-registration generation
    # (it held the lock across its whole freshness-check-and-scan), so its
    # own result is unaffected — this is the correct linearization, not a
    # stale failure: finish() happened-before register() in real time.
    tail, discarded = finish_result["out"]
    assert SECRET not in tail
    assert discarded is True  # trailing 3 chars of SECRET never resolve to a full variant

    # Registration completed only after finish() released the lock, and the
    # session is now finished anyway — every further call is rejected.
    with pytest.raises(SpecError, match=r"feed\(\) called after finish\(\)"):
        stream.feed("x")


def test_register_cannot_mutate_registry_while_scrub_replace_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = _new_scrubber(SECRET)
    new_secret = "brand-new-credential-during-scrub-000"

    replace_entered = threading.Event()
    release_replace = threading.Event()
    real_replace_all = secrets_module._replace_all

    def blocking_replace_all(text: str, variants: tuple[str, ...]) -> str:
        replace_entered.set()
        assert release_replace.wait(
            timeout=_JOIN_TIMEOUT
        ), "test deadlocked: _replace_all never released"
        return real_replace_all(text, variants)

    monkeypatch.setattr(secrets_module, "_replace_all", blocking_replace_all)

    text = f"leaked={new_secret} known={SECRET}"
    scrub_result: dict[str, str] = {}

    def scrub_target() -> None:
        scrub_result["out"] = scrubber.scrub(text)

    scrub_thread = threading.Thread(target=scrub_target)
    scrub_thread.start()
    assert replace_entered.wait(timeout=_JOIN_TIMEOUT), "scrub() never reached _replace_all"

    register_done = threading.Event()

    def register_target() -> None:
        scrubber.register(new_secret)
        register_done.set()

    register_thread = threading.Thread(target=register_target)
    register_thread.start()

    assert not register_done.wait(timeout=0.2)
    generation_during_scrub = scrubber._generation

    release_replace.set()
    _join_started_threads(scrub_thread, register_thread)

    assert register_done.is_set()
    assert scrubber._generation == generation_during_scrub + 1

    # Linearization proof: the in-flight scrub() read its variants strictly
    # before the registration landed, so its output is *exactly* the
    # pre-registration snapshot applied to the whole text — the
    # already-registered SECRET is fully redacted while the not-yet-
    # registered credential is left untouched. Never a mixed/torn state
    # (e.g. only half of the new credential's renderings redacted, or
    # _values/_variants disagreeing with each other).
    out = scrub_result["out"]
    assert SECRET not in out
    assert new_secret in out
    assert REDACTION_MARKER in out

    # The post-registration snapshot is now fully and durably applied: a
    # later scrub() call redacts the newly registered credential too, so no
    # raw newly-registered credential could ever be persisted going forward.
    later = scrubber.scrub(text)
    assert new_secret not in later
    assert SECRET not in later


# --- lock-ordering races: finish() linearizes terminal closure ------------
#
# These tests prove the MAJOR fix directly: before the fix, feed()/finish()
# each read ``self._finished`` *before* acquiring the shared lock, so a call
# blocked at lock entry while a concurrent finish() ran could still observe
# a stale "not finished yet" verdict once it finally acquired the lock, and
# proceed to scan/emit after finish() had already returned. Moving the check
# inside the lock — atomic with the ``_finished = True`` transition — makes
# finish() linearize terminal closure: any call that only reaches the lock
# after finish() has released it must see the terminal state and reject.


def test_finish_wins_lock_race_late_feed_raises_and_emits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)

    scan_entered = threading.Event()
    release_scan = threading.Event()
    real_scan = secrets_module._scan

    def blocking_scan(*args: object, **kwargs: object) -> tuple[str, int]:
        scan_entered.set()
        assert release_scan.wait(timeout=_JOIN_TIMEOUT), "test deadlocked: scan never released"
        return real_scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_module, "_scan", blocking_scan)

    finish_result: dict[str, tuple[str, bool]] = {}

    def finish_target() -> None:
        finish_result["out"] = stream.finish(source_eof=True)

    finish_thread = threading.Thread(target=finish_target)
    finish_thread.start()
    assert scan_entered.wait(timeout=_JOIN_TIMEOUT), "finish() never reached _scan"

    # finish() is holding the shared lock mid-scan. A feed() started now
    # must block trying to acquire that same lock — it must not be able to
    # read a pre-finish ``_finished`` value and run ahead of finish().
    feed_outcome: dict[str, object] = {}

    def feed_target() -> None:
        try:
            feed_outcome["out"] = stream.feed(f"late arrival {SECRET}")
        except SpecError as exc:
            feed_outcome["error"] = exc

    feed_thread = threading.Thread(target=feed_target)
    feed_thread.start()

    feed_thread.join(timeout=0.2)
    assert feed_thread.is_alive(), "feed() must stay blocked while finish() holds the lock"
    assert "out" not in feed_outcome and "error" not in feed_outcome

    release_scan.set()
    _join_started_threads(finish_thread, feed_thread)

    # finish() legitimately won the race and produced the terminal result.
    tail, discarded = finish_result["out"]
    assert SECRET not in tail
    assert discarded is False

    # feed(), having queued behind finish() on the lock, must observe the
    # terminal state and raise — never emit or buffer the secret it carried.
    assert "error" in feed_outcome, "feed() must raise after finish() won the lock race"
    assert isinstance(feed_outcome["error"], SpecError)
    assert "finish" in str(feed_outcome["error"])
    assert "out" not in feed_outcome
    assert SECRET not in str(feed_outcome["error"])
    assert stream._carry == ""
    assert stream._finished is True


def test_concurrent_finish_calls_yield_exactly_one_terminal_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    stream.feed(f"pre {SECRET[: len(SECRET) - 3]}")  # leave real unresolved carry for _scan

    scan_entered = threading.Event()
    release_scan = threading.Event()
    real_scan = secrets_module._scan

    def blocking_scan(*args: object, **kwargs: object) -> tuple[str, int]:
        scan_entered.set()
        assert release_scan.wait(timeout=_JOIN_TIMEOUT), "test deadlocked: scan never released"
        return real_scan(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secrets_module, "_scan", blocking_scan)

    results: list[dict[str, object]] = [{}, {}]

    def finish_target(slot: int) -> None:
        try:
            results[slot]["out"] = stream.finish(source_eof=True)
        except SpecError as exc:
            results[slot]["error"] = exc

    first_thread = threading.Thread(target=finish_target, args=(0,))
    first_thread.start()
    assert scan_entered.wait(timeout=_JOIN_TIMEOUT), "first finish() never reached _scan"

    second_thread = threading.Thread(target=finish_target, args=(1,))
    second_thread.start()

    # The second finish() must stay blocked on the shared lock while the
    # first still holds it mid-scan — it cannot race ahead and both "win".
    second_thread.join(timeout=0.2)
    assert second_thread.is_alive()
    assert "out" not in results[1] and "error" not in results[1]

    release_scan.set()
    _join_started_threads(first_thread, second_thread)

    winners = [slot for slot in results if "out" in slot]
    losers = [slot for slot in results if "error" in slot]
    assert len(winners) == 1, "exactly one concurrent finish() must produce a terminal result"
    assert len(losers) == 1, "the other concurrent finish() must reject, not silently no-op"
    assert isinstance(losers[0]["error"], SpecError)
    assert "finish" in str(losers[0]["error"])

    finish_result = winners[0]["out"]
    assert isinstance(finish_result, tuple) and len(finish_result) == 2
    tail, discarded = finish_result
    assert isinstance(tail, str) and isinstance(discarded, bool)
    assert SECRET not in tail
    assert discarded is True  # trailing 3 chars of SECRET never resolve to a full variant
    assert stream._finished is True


# --- open_stream ceilings: fail closed before returning a stream ---------


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_open_stream_rejects_invalid_limit_types(bad: object) -> None:
    scrubber = _new_scrubber(SECRET)
    with pytest.raises(SpecError):
        scrubber.open_stream(max_variant_chars=bad, max_variants=10)  # type: ignore[arg-type]
    with pytest.raises(SpecError):
        scrubber.open_stream(max_variant_chars=10, max_variants=bad)  # type: ignore[arg-type]


def test_open_stream_ceiling_boundary_is_exact() -> None:
    scrubber = _new_scrubber(SECRET)
    longest = scrubber.longest_variant_chars
    variant_count = len(scrubber._variants)
    scrubber.open_stream(max_variant_chars=longest, max_variants=variant_count)
    with pytest.raises(SpecError):
        scrubber.open_stream(max_variant_chars=longest - 1, max_variants=variant_count)
    with pytest.raises(SpecError):
        scrubber.open_stream(max_variant_chars=longest, max_variants=variant_count - 1)


def test_open_stream_with_no_registered_secrets() -> None:
    scrubber = SecretScrubber()
    stream = scrubber.open_stream(max_variant_chars=10, max_variants=10)
    out, discarded = _drain(stream, "nothing secret here")
    assert out == "nothing secret here"
    assert discarded is False


# --- immutability / no leakage --------------------------------------------


def test_stream_repr_never_exposes_variants() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    assert SECRET not in repr(stream)
    assert "variants=" in repr(stream)


def test_stream_is_slotted() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    with pytest.raises(AttributeError):
        stream.new_attr = 1  # type: ignore[attr-defined]


def test_feed_and_finish_after_finish_fail_closed() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    stream.finish(source_eof=True)
    with pytest.raises(SpecError):
        stream.feed("x")
    with pytest.raises(SpecError):
        stream.finish(source_eof=True)


def test_exceptions_never_include_secret_or_variant_text() -> None:
    scrubber = _new_scrubber(SECRET)
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    scrubber.register("another-one-000")
    with pytest.raises(SpecError) as excinfo:
        stream.feed(SECRET)
    assert SECRET not in str(excinfo.value)


# --- large deterministic stream: bounded carry, correct redaction --------


def test_large_deterministic_stream_stays_bounded_and_correct() -> None:
    scrubber = _new_scrubber(SECRET, "SECOND-KEY-XYZ-000")
    stream = scrubber.open_stream(max_variant_chars=200, max_variants=50)
    block = (
        "filler " * 37
        + SECRET
        + " more filler " * 41
        + "SECOND-KEY-XYZ-000"
        + "z" * 53
    )
    text = block * 200
    out_parts = []
    for i in range(0, len(text), 97):  # odd-sized chunks exercise many splits
        out_parts.append(stream.feed(text[i : i + 97]))
        assert len(stream._carry) < 200  # unresolved carry stays under the ceiling
    tail, discarded = stream.finish(source_eof=True)
    result = "".join(out_parts) + tail
    assert SECRET not in result
    assert "SECOND-KEY-XYZ-000" not in result
    assert result.count(REDACTION_MARKER) == 200 * 2
    assert discarded is False
