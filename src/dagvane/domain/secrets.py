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

from dagvane.domain.models import SpecError

_MAX_SCRUB_PASSES = 10


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

    __slots__ = ("_values", "_variants")

    _REPLACEMENT = "[redacted]"

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._variants: tuple[str, ...] = ()

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
        """
        if not self._variants:
            return text
        for _ in range(_MAX_SCRUB_PASSES):
            before = text
            for variant in self._variants:
                if variant in text:
                    text = text.replace(variant, self._REPLACEMENT)
            if text == before:
                break
        return text


# The process-wide registry: adapters and the durable-event path default to
# this shared instance, making cross-provider scrubbing an enforced invariant
# rather than a composition-root convention. Tests may construct private
# scrubbers explicitly.
_PROCESS_SCRUBBER = SecretScrubber()


def process_scrubber() -> SecretScrubber:
    return _PROCESS_SCRUBBER
