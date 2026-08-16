"""Canonical filesystem-backed identifier contract.

A single reusable validator for any identifier that is used to derive a
filesystem path segment (run ids, and future identifiers with the same
constraint). ASCII alphanumerics plus ``._-``, first character restricted to
alphanumeric so a leading ``.`` or ``-`` can never be produced, length capped
at 64. No strip, normalization, coercion, or case folding: the accepted value
is returned unchanged, or the input is rejected.
"""

from __future__ import annotations

import re

from dagvane.domain.models import SpecError

FILESYSTEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

CONVERSATION_ID_RESERVED = "current"


def validate_filesystem_id(value: object, *, ctx: str) -> str:
    """Validate ``value`` as a canonical filesystem-backed identifier.

    Returns the unchanged string on success. Raises ``SpecError`` with
    ``ctx`` for context otherwise.
    """
    if not isinstance(value, str):
        raise SpecError(f"{ctx}: must be a string, got {type(value).__name__}")
    # fullmatch, not match: "$" alone also matches just before a trailing
    # newline, which would let "id\n" slip through a match() check.
    if not FILESYSTEM_ID_RE.fullmatch(value):
        raise SpecError(f"{ctx}: {value!r} must match {FILESYSTEM_ID_RE.pattern}")
    return value


def validate_conversation_id(value: object, *, ctx: str) -> str:
    """Validate ``value`` as a canonical Conversation identifier.

    Delegates to ``validate_filesystem_id`` and additionally rejects the
    exact reserved value ``"current"``: ``<conversations>/current`` is the
    current-conversation pointer file, not a Conversation directory, so no
    caller or generated value may use it as a Conversation identity.
    """
    validated = validate_filesystem_id(value, ctx=ctx)
    if validated == CONVERSATION_ID_RESERVED:
        raise SpecError(
            f"{ctx}: {validated!r} is reserved for the current-conversation pointer"
        )
    return validated
