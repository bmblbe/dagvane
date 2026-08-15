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
    PartialUsage,
)
from dagvane.domain.secrets import SecretScrubber

# Normalized error text is journaled inside model.failed/node.failed events;
# frames are capped at 1 MiB, so provider error bodies must be bounded long
# before that limit.
MAX_ERROR_MESSAGE_CHARS = 2000

# The adapter's asyncio watchdog runs *behind* the transport's own timeout by
# this many seconds. The transport timers are per-phase and classify precisely
# (PoolTimeout/ConnectTimeout are pre-send, never billed); an equal outer
# deadline would win the race and mask them as a generic billed timeout. The
# watchdog remains only a backstop for a wedged transport.
WATCHDOG_GRACE_SECONDS = 5


def watchdog_seconds(timeout_seconds: int) -> int:
    return timeout_seconds + WATCHDOG_GRACE_SECONDS

# httpx exception class names that prove the request never reached the
# provider (nothing to bill) — matched by name so test doubles and the lazy
# import strategy work without importing httpx here. PoolTimeout belongs here:
# httpcore raises it while waiting to *acquire* a connection, before the
# request is physically sent (verified against httpcore 1.0.9).
PRE_SEND_ERROR_NAMES: frozenset[str] = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "UnsupportedProtocol",
        "InvalidURL",
        "LocalProtocolError",
        "ProxyError",
        "PoolTimeout",
    }
)

# httpx exception class names for timeouts after the request was sent.
POST_SEND_TIMEOUT_NAMES: frozenset[str] = frozenset({"ReadTimeout", "WriteTimeout"})


def bound_message(message: str, limit: int = MAX_ERROR_MESSAGE_CHARS) -> str:
    """Truncate an error message so journaled frames can never overflow."""
    if len(message) <= limit:
        return message
    return message[:limit] + f"... [truncated {len(message) - limit} chars]"


def redact(message: str, scrubber: SecretScrubber) -> str:
    """Scrub every registered secret (all encoded forms), then bound.

    Scrub-before-truncate is mandatory: truncating first could keep an
    identifying prefix of a secret straddling the boundary.
    """
    return bound_message(scrubber.scrub(message))


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


def usage_from_error_body(
    body: object,
    *,
    input_field: str = "input_tokens",
    output_field: str = "output_tokens",
) -> PartialUsage | None:
    """Best-effort provider-reported usage carried by an error body.

    A provider that reports usage on a failure response is reporting what it
    may bill; discarding it because the response failed would understate
    spend (Codex B4). Returns None when no component is present.
    """
    if not isinstance(body, dict):
        return None
    usage_obj = body.get("usage")
    if not isinstance(usage_obj, dict):
        return None
    reported = PartialUsage(
        input_tokens=usable_token_count(usage_obj.get(input_field)),
        output_tokens=usable_token_count(usage_obj.get(output_field)),
    )
    return None if reported.is_empty() else reported
