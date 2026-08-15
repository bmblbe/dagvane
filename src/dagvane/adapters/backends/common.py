"""Shared helpers for live backend adapters: redaction and error normalization.

Every message that leaves an adapter inside a ``BackendDispatchError`` must be
redacted **and bounded** here first — credential values never appear in
events, artifacts, or diagnostics, and provider error text must never grow a
journal frame past the protocol limit.
"""

from __future__ import annotations

from dagvane.domain.models import (
    DISPATCH_KIND_API,
    DISPATCH_KIND_AUTH,
    DISPATCH_KIND_PROTOCOL,
    DISPATCH_KIND_RATE_LIMIT,
)

# Normalized error text is journaled inside model.failed/node.failed events;
# frames are capped at 1 MiB, so provider error bodies must be bounded long
# before that limit.
MAX_ERROR_MESSAGE_CHARS = 2000

# httpx exception class names that prove the request never reached the
# provider (nothing to bill) — matched by name so test doubles and the lazy
# import strategy work without importing httpx here.
PRE_SEND_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "UnsupportedProtocol",
        "InvalidURL",
        "LocalProtocolError",
        "ProxyError",
    }
)

# httpx exception class names for timeouts after the request was sent.
POST_SEND_TIMEOUT_NAMES: frozenset[str] = frozenset(
    {"ReadTimeout", "WriteTimeout", "PoolTimeout"}
)


def bound_message(message: str, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    """Truncate an error message so journaled frames can never overflow."""
    if len(message) <= limit:
        return message
    return message[:limit] + f"... [truncated {len(message) - limit} chars]"


def redact(message: str, secrets: tuple[str, ...]) -> str:
    """Replace each secret value — including its escaped repr form — then bound.

    The escaped variant matters: transport libraries repr() header values in
    their error messages, so a key containing control characters would appear
    as ``sk-\\nabc`` and dodge a plain exact-match replacement.
    """
    for secret in secrets:
        if not secret:
            continue
        message = message.replace(secret, "[redacted]")
        escaped = secret.encode("unicode_escape").decode("ascii")
        if escaped != secret:
            message = message.replace(escaped, "[redacted]")
    return bound_message(message)


def kind_for_status(status_code: int) -> tuple[str, bool]:
    """Map the HTTP status of a *failure* to (normalized dispatch kind, billed).

    Requests rejected before processing (auth, rate limit, invalid request)
    are not billed; server-side failures may have consumed tokens, so they
    bill conservatively at the ceiling. An exception carrying a 2xx/3xx
    status means a delivered response could not be used (malformed body,
    validation failure) — the provider processed the call, so it is a billed
    protocol failure.
    """
    if 200 <= status_code < 400:
        return DISPATCH_KIND_PROTOCOL, True
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
