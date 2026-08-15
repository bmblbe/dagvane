"""Shared helpers for live backend adapters: redaction and error normalization.

Every message that leaves an adapter inside a ``BackendDispatchError`` must be
redacted here first — credential values never appear in events, artifacts, or
diagnostics.
"""

from __future__ import annotations

from dagvane.domain.models import (
    DISPATCH_KIND_API,
    DISPATCH_KIND_AUTH,
    DISPATCH_KIND_RATE_LIMIT,
)


def redact(message: str, secrets: tuple[str, ...]) -> str:
    """Replace every occurrence of each secret value with a placeholder."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def kind_for_status(status_code: int) -> tuple[str, bool]:
    """Map an HTTP status to (normalized dispatch kind, billed).

    Requests rejected before processing (auth, rate limit, invalid request)
    are not billed; server-side failures may have consumed tokens, so they
    bill conservatively at the ceiling.
    """
    if status_code in (401, 403):
        return DISPATCH_KIND_AUTH, False
    if status_code == 429:
        return DISPATCH_KIND_RATE_LIMIT, False
    if status_code >= 500:
        return DISPATCH_KIND_API, True
    return DISPATCH_KIND_API, False


def optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def usable_token_count(value: object) -> int | None:
    """A provider-reported token count usable for honest accounting."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
