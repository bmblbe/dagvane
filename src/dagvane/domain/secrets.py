"""Ephemeral secret registry and scrubbing boundary (G1 hardening).

One ``SecretScrubber`` instance holds every configured credential value for
the process — in memory only, never persisted — and is shared by all backend
adapters and the durable-event path. Everything provider-derived (response
content, model names, request ids, error text) passes through ``scrub()``
before it may be truncated, persisted, or forwarded to another provider.

Scrubbing replaces each registered value *and its common encoded forms*:

- the raw value;
- the ``unicode_escape`` form (transport libraries ``repr()`` header values);
- the JSON-escaped form (values reflected inside JSON bodies);
- the ``repr()`` inner form (mixed-quote values rendered by ``repr``).

Scrub-before-truncate is the caller's contract: a truncation applied first
could keep an identifying prefix of a secret that straddles the boundary.
"""

from __future__ import annotations

import json


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

    def register(self, value: str) -> None:
        """Register one credential value (idempotent). Empty values are ignored."""
        if not value or value in self._values:
            return
        self._values.add(value)
        variants: set[str] = set()
        for secret in self._values:
            variants.add(secret)
            escaped = secret.encode("unicode_escape").decode("ascii")
            variants.add(escaped)
            variants.add(json.dumps(secret)[1:-1])
            variants.add(repr(secret)[1:-1])
        # Longest first: an encoded form must not be half-destroyed by a
        # shorter raw replacement leaving recognizable fragments.
        self._variants = tuple(sorted(variants, key=len, reverse=True))

    def scrub(self, text: str) -> str:
        """Replace every registered secret (all encoded forms) in ``text``."""
        for variant in self._variants:
            if variant in text:
                text = text.replace(variant, self._REPLACEMENT)
        return text
