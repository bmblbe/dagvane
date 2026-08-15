"""Generic OpenAI-compatible backend adapter (G1).

One httpx-based JSON adapter for every configured OpenAI-compatible endpoint
(OpenAI, DeepSeek, OpenRouter, Ollama, local servers) — vendor differences
stay in profile configuration, never in the application layer. httpx is
imported lazily; error messages are redacted before leaving this module.
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
    DISPATCH_KIND_PROTOCOL,
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

BACKEND_KIND = "openai_compat"

_BODY_SNIPPET_LIMIT = 300


class OpenAICompatBackend:
    """One-shot ``/chat/completions`` adapter for OpenAI-compatible endpoints.

    ``client_factory`` injects a test double exposing ``await client.post(url,
    json=...)``; without it a real ``httpx.AsyncClient`` is built on first use.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        max_tokens_field: str = "max_tokens",
        monotonic: Monotonic | None = None,
        client_factory: Callable[[], Any] | None = None,
        scrubber: SecretScrubber | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._max_tokens_field = max_tokens_field
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
        """Verify the optional dependency is importable — without constructing
        the client, which must be born inside the running event loop."""
        if self._client_factory is not None:
            return
        try:
            import httpx  # noqa: F401
        except ModuleNotFoundError as exc:
            raise SpecError(
                "httpx is not installed; "
                "install the live extra: pip install 'dagvane[live]'"
            ) from exc

    async def aclose(self) -> None:
        """Release transport resources. The adapter is single-event-loop scoped."""
        client = self._client
        self._client = None
        if client is not None:
            aclose = getattr(client, "aclose", None)
            if callable(aclose):
                result = aclose()
                if result is not None and hasattr(result, "__await__"):
                    await result

    def _get_client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                try:
                    import httpx
                except ModuleNotFoundError as exc:
                    raise SpecError(
                        "httpx is not installed; "
                        "install the live extra: pip install 'dagvane[live]'"
                    ) from exc
                self._client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=float(self._timeout_seconds),
                )
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

    def _protocol_error(
        self,
        message: str,
        started_ms: int,
        request_id: str | None = None,
        usage: PartialUsage | None = None,
    ) -> BackendDispatchError:
        return BackendDispatchError(
            kind=DISPATCH_KIND_PROTOCOL,
            message=self._redact(message),
            billed=True,
            usage=usage,
            receipt=self._receipt(started_ms, request_id),
        )

    async def complete(self, request: PreparedRequest) -> ChatResult:
        started_ms = self._monotonic.now_ms()
        try:
            # Client construction stays inside the normalization boundary: a
            # constructor rejecting a header/base-URL configuration may echo
            # credential material in its exception.
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
        url = self._base_url + "/chat/completions"
        payload: dict[str, object] = {
            "model": request.model,
            # Reasoning-era OpenAI endpoints reject "max_tokens"; the profile's
            # connection config selects the field name (default "max_tokens").
            self._max_tokens_field: request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user_text},
            ],
            "stream": False,
        }
        try:
            # The watchdog runs behind the transport's own timers (grace) so
            # precise pre-send classifications (PoolTimeout, ConnectTimeout)
            # are never masked by an equal outer deadline (Codex M2).
            async with asyncio.timeout(watchdog_seconds(self._timeout_seconds)):
                response = await client.post(url, json=payload)
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
            error_name = type(exc).__name__
            if error_name in PRE_SEND_ERROR_NAMES:
                # The request provably never reached the provider: not billed.
                kind, billed = DISPATCH_KIND_CONNECTION, False
            elif error_name in POST_SEND_TIMEOUT_NAMES:
                kind, billed = DISPATCH_KIND_TIMEOUT, True
            else:
                kind, billed = DISPATCH_KIND_CONNECTION, True
            raise BackendDispatchError(
                kind=kind,
                message=self._redact(f"{error_name}: {exc}"),
                billed=billed,
                receipt=self._receipt(started_ms, None),
            ) from exc

        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or isinstance(status, bool):
            raise self._protocol_error("response carries no HTTP status code", started_ms)
        if status != 200:
            body_text = getattr(response, "text", "")
            # Scrub the *full* body before truncation: a credential straddling
            # the snippet boundary must not survive as an identifying prefix.
            scrubbed = (
                self._scrubber.scrub(body_text) if isinstance(body_text, str) else ""
            )
            snippet = scrubbed[:_BODY_SNIPPET_LIMIT]
            kind, billed = kind_for_status(status)
            # An error body may still carry the provider's reported usage —
            # what it may bill. Parse best-effort; never discard a known
            # component merely because the response failed.
            try:
                error_body = response.json()
            except Exception:  # noqa: BLE001 — error bodies are often not JSON
                error_body = None
            raise BackendDispatchError(
                kind=kind,
                message=self._redact(f"HTTP {status}: {snippet}"),
                billed=billed,
                usage=usage_from_error_body(
                    error_body,
                    input_field="prompt_tokens",
                    output_field="completion_tokens",
                ),
                receipt=self._receipt(started_ms, None),
            )

        try:
            body = response.json()
        except Exception as exc:
            raise self._protocol_error(
                f"response body is not valid JSON: {type(exc).__name__}", started_ms
            ) from exc
        if not isinstance(body, dict):
            raise self._protocol_error("response body is not a JSON object", started_ms)

        request_id = self._scrub_opt(optional_str(body.get("id")))
        receipt = self._receipt(started_ms, request_id)

        # Usage is read *before* validating choices: a billed 200 with a
        # malformed choice must not lose the provider's reported actuals.
        usage_obj = body.get("usage")
        if not isinstance(usage_obj, dict):
            usage_obj = {}
        input_tokens = usable_token_count(usage_obj.get("prompt_tokens"))
        output_tokens = usable_token_count(usage_obj.get("completion_tokens"))
        reported = PartialUsage(input_tokens=input_tokens, output_tokens=output_tokens)

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise self._protocol_error(
                "response carries no choices", started_ms, request_id, usage=reported
            )
        message_obj = choices[0].get("message")
        if not isinstance(message_obj, dict):
            raise self._protocol_error(
                "response choice carries no message object",
                started_ms,
                request_id,
                usage=reported,
            )
        content = message_obj.get("content")
        # Provider-derived content is scrubbed before it can be persisted or
        # forwarded to another provider as council context.
        text = self._scrubber.scrub(content) if isinstance(content, str) else ""

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
                usage=reported,
                receipt=receipt,
            )

        model_name = self._scrub_opt(optional_str(body.get("model")))
        return ChatResult(
            model=model_name if model_name is not None else request.model,
            text=text,
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            receipt=receipt,
        )
