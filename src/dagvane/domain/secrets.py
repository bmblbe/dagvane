"""Ephemeral secret registry and scrubbing boundary (G1 hardening, round 2).

One process-wide ``SecretScrubber`` holds every configured credential value —
in memory only, never persisted — and is shared by all backend adapters and
the durable-event path. Everything provider-derived (response content, model
names, request ids, error text) passes through ``scrub()`` before it may be
truncated, persisted, or forwarded to another provider.

Scrubbing replaces each registered value and the closure of its common
encoded renderings up to two nesting levels (JSON-in-JSON reflections):

- the raw value;
- ``unicode_escape`` (transport libraries ``repr()`` header values);
- the JSON-escaped inner form (values reflected inside JSON bodies);
- the ``repr()`` inner form (mixed-quote values);
- every second-level composition of the above (e.g. JSON-escaped twice).

The replacement marker cannot itself defeat a literal secret-byte scan: a
credential that overlaps the marker (either direction, in any rendering) is
*refused at registration* with a configuration error, so after scrubbing no
registered rendering can remain. ``scrub()`` re-applies passes until the text
is stable, so juxtaposition around inserted markers cannot resurrect a
rendering. Scrub-before-truncate is the caller's contract: a truncation
applied first could keep an identifying prefix of a secret that straddles the
boundary.
"""

from __future__ import annotations

import json
import threading

from dagvane.domain.models import SpecError

_MAX_SCRUB_PASSES = 10

# Public so callers that must recognize the marker post-hoc (e.g. a bounded
# retention window that must never split it) do not duplicate the literal.
REDACTION_MARKER = "[redacted]"


def _renderings(value: str) -> set[str]:
    """The closure of common encoded forms of ``value``, depth 2."""

    def json_inner(text: str) -> str:
        return json.dumps(text)[1:-1]

    def unicode_escaped(text: str) -> str:
        return text.encode("unicode_escape").decode("ascii")

    def repr_inner(text: str) -> str:
        return repr(text)[1:-1]

    encoders = (json_inner, unicode_escaped, repr_inner)
    level_one = {value} | {encode(value) for encode in encoders}
    level_two = {encode(form) for form in level_one for encode in encoders}
    return {form for form in level_one | level_two if form}


class SecretScrubber:
    """Holds registered secret values ephemerally; replaces them in text.

    The registry is process-local state. It must never be serialized; its
    ``repr`` deliberately reveals only the number of registered secrets.
    """

    __slots__ = ("_values", "_variants", "_generation", "_lock")

    _REPLACEMENT = REDACTION_MARKER

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._variants: tuple[str, ...] = ()
        # Bumped on every registration that changes ``_variants``; lets a
        # streaming session snapshot-and-detect mutation instead of racing a
        # mid-stream registry change (see ``open_stream``).
        self._generation = 0
        # Linearization boundary shared with every StreamingSecretScrubber
        # opened from this registry: register()/scrub()/open_stream() and a
        # stream's feed()/finish() each hold this lock for their entire body,
        # so a mutation can never land strictly between a stream's freshness
        # check and the scan it guards (see StreamingSecretScrubber).
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # never expose values, even in debug output
        return f"SecretScrubber(secrets={len(self._values)})"

    @classmethod
    def _overlaps_marker(cls, rendering: str) -> bool:
        """True when the rendering could survive or regenerate around the
        marker: containment either way, or a boundary overlap (a rendering
        prefix equal to a marker suffix, or a rendering suffix equal to a
        marker prefix). Boundary overlaps matter because an inserted marker
        abutting untouched text could otherwise recreate the rendering
        faster than scrub passes remove it.
        """
        marker = cls._REPLACEMENT
        if rendering in marker or marker in rendering:
            return True
        for k in range(1, min(len(rendering), len(marker)) + 1):
            if marker.endswith(rendering[:k]) or marker.startswith(rendering[-k:]):
                return True
        return False

    def register(self, value: str) -> None:
        """Register one credential value (idempotent). Empty values are ignored.

        A value whose renderings overlap the replacement marker — containment
        or boundary overlap — is refused: replacing such a secret could leave
        or re-insert its exact bytes, defeating a literal secret-byte scan.
        """
        with self._lock:
            if not value or value in self._values:
                return
            for rendering in _renderings(value):
                if self._overlaps_marker(rendering):
                    raise SpecError(
                        "credential value overlaps the redaction marker and cannot "
                        "be scrubbed reliably; choose a different credential"
                    )
            self._values.add(value)
            variants: set[str] = set()
            for secret in self._values:
                variants.update(_renderings(secret))
            # Longest first: an encoded form must not be half-destroyed by a
            # shorter raw replacement leaving recognizable fragments.
            self._variants = tuple(sorted(variants, key=len, reverse=True))
            self._generation += 1

    @property
    def longest_variant_chars(self) -> int:
        """Length of the longest registered rendering (0 when none registered).

        Callers that retain only a bounded window of a larger stream cannot
        scrub bytes they never saw: a rendering straddling the window edge
        survives ``scrub()`` as a partial fragment shorter than the longest
        variant. After scrubbing the retained window, dropping
        ``longest_variant_chars - 1`` characters at the cut edge provably
        removes any such fragment (a complete rendering inside the window was
        already replaced; only a strict-prefix/suffix shorter than this bound
        can remain at the edge).
        """
        return max((len(variant) for variant in self._variants), default=0)

    def scrub(self, text: str) -> str:
        """Replace every registered secret rendering until the text is stable.

        Multiple passes close the juxtaposition gap: an insertion of the
        marker could abut surviving bytes into another registered rendering,
        which the next pass removes. Registration guarantees no rendering
        overlaps the marker itself, so passes converge.

        Held for the whole read-and-replace, the same lock ``register()``
        holds for its whole body: a registration can never land strictly
        between the moment this call reads ``_variants`` and the moment it
        finishes replacing, so one ``scrub()`` call always sees either the
        registry exactly as it stood before some concurrent registration or
        exactly as it stood after — never a snapshot read followed by a scan
        that a landing registration should have invalidated.
        """
        with self._lock:
            variants = self._variants
            if not variants:
                return text
            return _replace_all(text, variants)

    def open_stream(
        self, *, max_variant_chars: int, max_variants: int
    ) -> StreamingSecretScrubber:
        """Open one streaming scrub session over a snapshot of variants
        already registered at this point.

        Fails closed (raises ``SpecError``) rather than returning a stream
        whose ceilings the current registry already exceeds: a stream is a
        commitment to bound unresolved carry by ``max_variant_chars``, which
        cannot be honored if a variant longer than that is already
        registered, and scanning cost is bounded by ``max_variants``.
        """
        for name, value in (
            ("max_variant_chars", max_variant_chars),
            ("max_variants", max_variants),
        ):
            if type(value) is not int or value <= 0:
                raise SpecError(f"{name} must be a positive integer")
        with self._lock:
            generation = self._generation
            variants = self._variants
            if len(variants) > max_variants:
                raise SpecError(
                    f"registered secret-variant count {len(variants)} exceeds the "
                    f"streaming ceiling {max_variants}"
                )
            longest = max((len(variant) for variant in variants), default=0)
            if longest > max_variant_chars:
                raise SpecError(
                    f"longest registered secret-variant length {longest} exceeds "
                    f"the streaming ceiling {max_variant_chars}"
                )
            return StreamingSecretScrubber(self, generation, variants, longest)


def _replace_all(text: str, variants: tuple[str, ...]) -> str:
    """Replace every variant in ``variants`` within ``text`` until stable.

    Module-level so a caller holding ``SecretScrubber._lock`` (``scrub()``)
    does the whole read-variants-and-replace under one uninterrupted critical
    section; factored out, rather than inlined, so it is a seam a test can
    monkeypatch to make that critical section observably slow without
    changing its behavior.
    """
    for _ in range(_MAX_SCRUB_PASSES):
        before = text
        for variant in variants:
            if variant in text:
                text = text.replace(variant, SecretScrubber._REPLACEMENT)
        if text == before:
            break
    return text


def _scan(
    buffer: str, variants: tuple[str, ...], longest: int, *, hold_growable: bool
) -> tuple[str, int]:
    """Leftmost-longest scan of ``variants`` over ``buffer``.

    Returns the resolved output (matches replaced by the redaction marker)
    and the buffer index from which resolution is not yet certain — equal to
    ``len(buffer)`` when the whole buffer resolved. A position is ambiguous
    only when its remaining tail is shorter than ``longest``, since no
    variant can match starting there without more data; that bounds the
    ambiguity check to at most ``longest - 1`` trailing characters and keeps
    the scan linear in ``len(buffer)`` for any fixed variant set.

    ``hold_growable`` selects which side of a buffer-edge ambiguity wins,
    and choosing it per call site is what makes redaction split-invariant:

    - ``True`` (mid-stream ``feed``): a position where some variant longer
      than the remaining tail could still complete is held *even if a
      shorter variant already matches there in full* — committing the short
      match now would differ from what an unsplit buffer resolves to
      (leftmost-longest must not depend on where chunks were cut);
    - ``False`` (real EOF): no more data can arrive, so a full match is
      final; only a tail that matches nothing yet remains a proper prefix
      of some variant is reported as unresolved (the caller discards it).
    """
    first_chars = {variant[0] for variant in variants if variant}
    out: list[str] = []
    i = 0
    n = len(buffer)
    while i < n:
        remaining = n - i
        ch = buffer[i]
        in_first = ch in first_chars
        if hold_growable and in_first and remaining < longest:
            tail = buffer[i:]
            if any(
                len(variant) > remaining and variant.startswith(tail) for variant in variants
            ):
                return "".join(out), i
        matched_len = 0
        if in_first:
            for variant in variants:  # longest-first: leftmost-longest
                vlen = len(variant)
                if vlen <= remaining and buffer[i : i + vlen] == variant:
                    matched_len = vlen
                    break
        if matched_len:
            out.append(SecretScrubber._REPLACEMENT)
            i += matched_len
            continue
        if not hold_growable and in_first and remaining < longest:
            tail = buffer[i:]
            if any(
                len(variant) > remaining and variant.startswith(tail) for variant in variants
            ):
                return "".join(out), i
        out.append(ch)
        i += 1
    return "".join(out), n


class StreamingSecretScrubber:
    """One streaming scrub session over a snapshot of variants already
    registered on a ``SecretScrubber`` at ``open_stream`` time.

    ``feed()`` only ever emits text proven unable to begin a registered
    variant; an unresolved suffix that could still grow into one is held
    back as bounded carry and folded into the next call. ``finish()`` closes
    the session exactly once: at real EOF the held carry is resolved fully
    (any complete variant redacted; a final proper-prefix fragment is
    discarded, never revealed, since revealing a partial credential is as
    unsafe as revealing the whole one); at a non-EOF finish the carry is
    always discarded unread, because it cannot be told apart from a
    credential truncated mid-stream. A later registration on the source
    scrubber invalidates the snapshot: every further call fails closed by
    raising rather than silently scrubbing against stale variants.

    ``feed()``/``finish()`` hold the source scrubber's lock for their entire
    body, the same lock ``register()``/``scrub()``/``open_stream()`` hold:
    a registration can never land strictly between this session's freshness
    check and the scan it guards, so every call observes a registry that is
    either still exactly this session's generation for its whole duration,
    or already advanced before the call began (and is rejected outright).
    """

    __slots__ = ("_scrubber", "_generation", "_variants", "_longest", "_carry", "_finished")

    def __init__(
        self,
        scrubber: SecretScrubber,
        generation: int,
        variants: tuple[str, ...],
        longest: int,
    ) -> None:
        self._scrubber = scrubber
        self._generation = generation
        self._variants = variants
        self._longest = longest
        self._carry = ""
        self._finished = False

    def __repr__(self) -> str:  # never expose values, even in debug output
        return f"StreamingSecretScrubber(variants={len(self._variants)})"

    def _check_fresh(self) -> None:
        if self._scrubber._generation != self._generation:
            # Close the stream, not just clear this call's view of it: a
            # caller that catches the exception and keeps calling must not
            # be able to observe or extend the buffered fragment, and every
            # later call must fail the same closed way (``feed()``/``finish()
            # called after finish()``) rather than re-running this check.
            self._carry = ""
            self._finished = True
            raise SpecError(
                "secret registry changed after open_stream; this streaming "
                "session is stale and must be discarded"
            )

    def feed(self, text: str) -> str:
        if type(text) is not str:
            raise SpecError(f"feed() text must be a str, got {type(text).__name__}")
        # ``_finished`` is read and every state transition happens inside
        # this same lock, the one register()/scrub()/open_stream()/finish()
        # all hold for their whole body: a feed() blocked here while
        # finish() is in flight must not be able to observe a stale
        # "not finished yet" verdict and run its scan after finish() has
        # already linearized terminal closure — it must see the terminal
        # state finish() left behind and fail closed, emitting nothing.
        with self._scrubber._lock:
            if self._finished:
                raise SpecError("feed() called after finish()")
            self._check_fresh()
            buffer = self._carry + text
            output, cut = _scan(buffer, self._variants, self._longest, hold_growable=True)
            self._carry = buffer[cut:]
            return output

    def finish(self, *, source_eof: bool) -> tuple[str, bool]:
        if type(source_eof) is not bool:
            raise SpecError(
                f"finish() source_eof must be a bool, got {type(source_eof).__name__}"
            )
        # Same lock as feed(): the finished-check and the terminal
        # transition happen atomically, so two concurrent finish() calls
        # cannot both observe "not finished yet" — exactly one linearizes
        # first and wins, the other sees the terminal state and rejects.
        with self._scrubber._lock:
            if self._finished:
                raise SpecError("finish() called after finish()")
            self._check_fresh()
            self._finished = True
            carry, self._carry = self._carry, ""
            if not source_eof:
                return "", bool(carry)
            output, cut = _scan(carry, self._variants, self._longest, hold_growable=False)
            return output, cut < len(carry)


# The process-wide registry: adapters and the durable-event path default to
# this shared instance, making cross-provider scrubbing an enforced invariant
# rather than a composition-root convention. Tests may construct private
# scrubbers explicitly.
_PROCESS_SCRUBBER = SecretScrubber()


def process_scrubber() -> SecretScrubber:
    return _PROCESS_SCRUBBER
