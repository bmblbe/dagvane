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
    kind_for_status,
    optional_str,
    redact,
    usable_token_count,
)
from dagvane.domain.models import (
    DISPATCH_KIND_CONNECTION,
    DISPATCH_KIND_PROTOCOL,
    DISPATCH_KIND_TIMEOUT,
    DISPATCH_KIND_USAGE_MISSING,
    BackendDispatchError,
    InvocationReceipt,
    SpecError,
    Usage,
)
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
        monotonic: Monotonic | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._connection_id = connection_id
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._monotonic: Monotonic = monotonic if monotonic is not None else SystemMonotonic()
        self._client_factory = client_factory
        self._client: Any | None = None

    def ensure_ready(self) -> None:
        """Build the client now: a missing dependency surfaces before any run state."""
        self._get_client()

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
        return redact(message, (self._api_key,))

    def _receipt(self, started_ms: int, provider_request_id: str | None) -> InvocationReceipt:
        return InvocationReceipt(
            backend_kind=BACKEND_KIND,
            connection_id=self._connection_id,
            provider_request_id=provider_request_id,
            latency_ms=max(0, self._monotonic.now_ms() - started_ms),
        )

    def _protocol_error(
        self, message: str, started_ms: int, request_id: str | None = None
    ) -> BackendDispatchError:
        return BackendDispatchError(
            kind=DISPATCH_KIND_PROTOCOL,
            message=self._redact(message),
            billed=True,
            receipt=self._receipt(started_ms, request_id),
        )

    async def complete(self, request: PreparedRequest) -> ChatResult:
        client = self._get_client()
        url = self._base_url + "/chat/completions"
        payload: dict[str, object] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user_text},
            ],
            "stream": False,
        }
        started_ms = self._monotonic.now_ms()
        try:
            async with asyncio.timeout(self._timeout_seconds):
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
            raise BackendDispatchError(
                kind=DISPATCH_KIND_CONNECTION,
                message=self._redact(f"{type(exc).__name__}: {exc}"),
                billed=True,
                receipt=self._receipt(started_ms, None),
            ) from exc

        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or isinstance(status, bool):
            raise self._protocol_error("response carries no HTTP status code", started_ms)
        if status != 200:
            body_text = getattr(response, "text", "")
            snippet = body_text[:_BODY_SNIPPET_LIMIT] if isinstance(body_text, str) else ""
            kind, billed = kind_for_status(status)
            raise BackendDispatchError(
                kind=kind,
                message=self._redact(f"HTTP {status}: {snippet}"),
                billed=billed,
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

        request_id = optional_str(body.get("id"))
        receipt = self._receipt(started_ms, request_id)

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise self._protocol_error("response carries no choices", started_ms, request_id)
        message_obj = choices[0].get("message")
        if not isinstance(message_obj, dict):
            raise self._protocol_error(
                "response choice carries no message object", started_ms, request_id
            )
        content = message_obj.get("content")
        text = content if isinstance(content, str) else ""

        usage_obj = body.get("usage")
        if not isinstance(usage_obj, dict):
            usage_obj = {}
        input_tokens = usable_token_count(usage_obj.get("prompt_tokens"))
        output_tokens = usable_token_count(usage_obj.get("completion_tokens"))
        if input_tokens is None or output_tokens is None:
            raise BackendDispatchError(
                kind=DISPATCH_KIND_USAGE_MISSING,
                message=(
                    "provider response carries no usable token usage; "
                    "a hard-budget run cannot account this call"
                ),
                billed=True,
                receipt=receipt,
            )

        model_name = optional_str(body.get("model"))
        return ChatResult(
            model=model_name if model_name is not None else request.model,
            text=text,
            usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
            receipt=receipt,
        )
