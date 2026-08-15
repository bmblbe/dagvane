"""Native Anthropic backend adapter (G1).

The SDK is imported lazily so the default install stays dependency-free; the
engine owns all retries (``max_retries=0``). Every error message is redacted
before it leaves this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from dagvane.adapters.backends.common import (
    POST_SEND_TIMEOUT_NAMES,
    PRE_SEND_ERROR_NAMES,
    kind_for_status,
    optional_str,
    redact,
    usable_token_count,
    usage_from_error_body,
    watchdog_seconds,
)
from dagvane.domain.models import (
    DISPATCH_KIND_CONNECTION,
    DISPATCH_KIND_TIMEOUT,
    DISPATCH_KIND_USAGE_MISSING,
    BackendDispatchError,
    InvocationReceipt,
    PartialUsage,
    SpecError,
    Usage,
)
from dagvane.domain.secrets import SecretScrubber, process_scrubber
from dagvane.ports.backend import ChatResult, PreparedRequest
from dagvane.ports.runtime import Monotonic, SystemMonotonic

BACKEND_KIND = "anthropic"


class AnthropicBackend:
    """One-shot Anthropic Messages adapter.

    ``client_factory`` injects a test double with the same call surface
    (``client.messages.create``); without it the real SDK client is built on
    first use with SDK retries disabled.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        api_key: str,
        timeout_seconds: int,
        base_url: str | None = None,
        monotonic: Monotonic | None = None,
        client_factory: Callable[[], Any] | None = None,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url
        self._monotonic: Monotonic = monotonic if monotonic is not None else SystemMonotonic()
        self._client_factory = client_factory
        self._client: Any | None = None
        # Cross-provider scrubbing is an enforced invariant: adapters default
        # to the process-wide registry (tests may inject a private one), and
        # this adapter's own credential is always registered.
        self._scrubber: SecretScrubber = (
            scrubber if scrubber is not None else process_scrubber()
        )
        self._scrubber.register(api_key)

    def ensure_ready(self) -> None:
        """Verify the optional SDK is importable — without constructing the
        client, which must be born inside the running event loop."""
        if self._client_factory is not None:
            return
        try:
            import anthropic  # noqa: F401
        except ModuleNotFoundError as exc:
            raise SpecError(
                "the Anthropic SDK is not installed; "
                "install the live extra: pip install 'dagvane[live]'"
            ) from exc

    async def aclose(self) -> None:
        """Release transport resources. The adapter is single-event-loop scoped."""
        client = self._client
        self._client = None
        if client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if result is not None and hasattr(result, "__await__"):
                    await result

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                try:
                    from anthropic import AsyncAnthropic
                except ModuleNotFoundError as exc:
                    raise SpecError(
                        "the Anthropic SDK is not installed; "
                        "install the live extra: pip install 'dagvane[live]'"
                    ) from exc
                kwargs: dict[str, Any] = {
                    "api_key": self._api_key,
                    # Engine-owned retries (Round 4 F1): the SDK must never retry.
                    "max_retries": 0,
                    "timeout": float(self._timeout_seconds),
                }
                if self._base_url is not None:
                    kwargs["base_url"] = self._base_url
                self._client = AsyncAnthropic(**kwargs)
        return self._client

    def _redact(self, message: str) -> str:
        return redact(message, self._scrubber)

    def _scrub_opt(self, value: str | None) -> str | None:
        return self._scrubber.scrub(value) if value is not None else None

    def _receipt(self, started_ms: int, provider_request_id: str | None) -> InvocationReceipt:
        return InvocationReceipt(
            backend_kind=BACKEND_KIND,
            connection_id=self._connection_id,
            provider_request_id=provider_request_id,
            latency_ms=max(0, self._monotonic.now_ms() - started_ms),
        )

    def _normalize(self, exc: Exception, started_ms: int) -> BackendDispatchError:
        message = self._redact(f"{type(exc).__name__}: {exc}")
        receipt = self._receipt(started_ms, None)
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            kind, billed = kind_for_status(status)
            # A status-bearing SDK exception may carry the provider's reported
            # usage in its body; never discard a known billable component.
            return BackendDispatchError(
                kind=kind,
                message=message,
                billed=billed,
                usage=usage_from_error_body(getattr(exc, "body", None)),
                receipt=receipt,
            )
        # No HTTP status. The SDK wraps transport errors; when the underlying
        # cause proves the request never left this machine, nothing was billed.
        cause_name = type(exc.__cause__).__name__ if exc.__cause__ is not None else ""
        if cause_name in PRE_SEND_ERROR_NAMES:
            return BackendDispatchError(
                kind=DISPATCH_KIND_CONNECTION, message=message, billed=False, receipt=receipt
            )
        if cause_name in POST_SEND_TIMEOUT_NAMES:
            return BackendDispatchError(
                kind=DISPATCH_KIND_TIMEOUT, message=message, billed=True, receipt=receipt
            )
        return BackendDispatchError(
            kind=DISPATCH_KIND_CONNECTION, message=message, billed=True, receipt=receipt
        )

    async def complete(self, request: PreparedRequest) -> ChatResult:
        started_ms = self._monotonic.now_ms()
        try:
            # Client construction stays inside the normalization boundary: an
            # SDK constructor rejecting a header/base-URL configuration may
            # echo credential material in its exception.
            client = self._get_client()
        except Exception as exc:
            raise BackendDispatchError(
                kind=DISPATCH_KIND_CONNECTION,
                message=self._redact(
                    f"client construction failed: {type(exc).__name__}: {exc}"
                ),
                billed=False,
                receipt=self._receipt(started_ms, None),
            ) from exc
        try:
            # The watchdog runs behind the transport's own timers (grace) so
            # precise pre-send classifications (PoolTimeout, ConnectTimeout)
            # are never masked by an equal outer deadline (Codex M2).
            async with asyncio.timeout(watchdog_seconds(self._timeout_seconds)):
                message = await client.messages.create(
                    model=request.model,
                    max_tokens=request.max_output_tokens,
                    system=request.system,
                    messages=[{"role": "user", "content": request.user_text}],
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise BackendDispatchError(
                kind=DISPATCH_KIND_TIMEOUT,
                message=f"no response within {self._timeout_seconds}s",
                billed=True,
                receipt=self._receipt(started_ms, None),
            ) from exc
        except Exception as exc:
            raise self._normalize(exc, started_ms) from exc

        # Every provider-derived field is scrubbed before it can be persisted
        # or forwarded to another provider (success paths included).
        receipt = self._receipt(
            started_ms, self._scrub_opt(optional_str(getattr(message, "id", None)))
        )

        parts: list[str] = []
        content = getattr(message, "content", None)
        if isinstance(content, (list, tuple)):
            for block in content:
                if getattr(block, "type", None) == "text":
                    text_value = getattr(block, "text", None)
                    if isinstance(text_value, str):
                        parts.append(text_value)
        text = self._scrubber.scrub("".join(parts))

        usage_obj = getattr(message, "usage", None)
        input_tokens = usable_token_count(getattr(usage_obj, "input_tokens", None))
        output_tokens = usable_token_count(getattr(usage_obj, "output_tokens", None))
        if input_tokens is None or output_tokens is None:
            # Preserve every reliable reported component: partial usage rides
            # on the error so accounting never discards a known actual.
            raise BackendDispatchError(
                kind=DISPATCH_KIND_USAGE_MISSING,
                message=(
                    "provider response carries incomplete token usage; "
                    "unknown components are accounted at the reservation ceiling"
                ),
                billed=True,
                usage=PartialUsage(input_tokens=input_tokens, output_tokens=output_tokens),
                receipt=receipt,
            )

        model_name = self._scrub_opt(optional_str(getattr(message, "model", None)))
        return ChatResult(
            model=model_name if model_name is not None else request.model,
            text=text,
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            receipt=receipt,
        )
