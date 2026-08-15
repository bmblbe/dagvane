"""Live backend adapters against injected fake clients — fully offline.

Covers: usage normalization, receipts, error-kind/billing mapping, redaction,
timeout, cancellation propagation, missing-usage refusal, and the missing-SDK
usage error. No network, no vendor SDKs.
"""

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest

from dagvane.adapters.backends.anthropic import AnthropicBackend
from dagvane.adapters.backends.openai_compat import OpenAICompatBackend
from dagvane.domain.models import BackendDispatchError, SpecError, Usage
from dagvane.ports.backend import PreparedRequest
from dagvane.ports.runtime import SteppingMonotonic

API_KEY = "sk-test-secret-9f8e7d"

REQUEST = PreparedRequest(
    model="test-model", max_output_tokens=64, system="sys", user_text="hello"
)


class FakeStatusError(Exception):
    """Duck-typed provider exception carrying an HTTP status code."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------


def anthropic_message(
    *,
    text_blocks: tuple[str, ...] = ("hel", "lo"),
    input_tokens: object = 7,
    output_tokens: object = 3,
    with_usage: bool = True,
) -> SimpleNamespace:
    content = [SimpleNamespace(type="text", text=t) for t in text_blocks]
    content.insert(0, SimpleNamespace(type="tool_use"))  # non-text blocks are ignored
    usage = (
        SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        if with_usage
        else None
    )
    return SimpleNamespace(
        id="msg_123", model="test-model-2026", content=content, usage=usage
    )


class FakeAnthropicClient:
    def __init__(
        self, result: object = None, error: Exception | None = None, hang: bool = False
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result
        self._error = error
        self._hang = hang
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self._hang:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        return self._result


def anthropic_backend(client: FakeAnthropicClient, *, timeout: int = 30) -> AnthropicBackend:
    return AnthropicBackend(
        connection_id="anthro",
        api_key=API_KEY,
        timeout_seconds=timeout,
        monotonic=SteppingMonotonic(step_ms=5),
        client_factory=lambda: client,
    )


def test_anthropic_success_normalizes_text_usage_and_receipt() -> None:
    client = FakeAnthropicClient(result=anthropic_message())
    result = asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert result.text == "hello"
    assert result.model == "test-model-2026"
    assert result.usage == Usage(input_tokens=7, output_tokens=3)
    assert result.receipt is not None
    assert result.receipt.backend_kind == "anthropic"
    assert result.receipt.connection_id == "anthro"
    assert result.receipt.provider_request_id == "msg_123"
    assert result.receipt.latency_ms == 5  # SteppingMonotonic: one step between readings
    [call] = client.calls
    assert call["model"] == "test-model"
    assert call["max_tokens"] == 64
    assert call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize(
    ("status", "kind", "billed"),
    [
        (401, "auth", False),
        (403, "auth", False),
        (429, "rate_limit", False),
        (400, "api", False),
        (500, "api", True),
        (503, "api", True),
    ],
)
def test_anthropic_status_error_mapping(status: int, kind: str, billed: bool) -> None:
    client = FakeAnthropicClient(error=FakeStatusError(status, "provider says no"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == kind
    assert excinfo.value.billed is billed
    assert excinfo.value.receipt is not None


def test_anthropic_statusless_error_is_connection_and_billed() -> None:
    client = FakeAnthropicClient(error=RuntimeError("socket exploded"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "connection"
    assert excinfo.value.billed is True


def test_anthropic_error_messages_are_redacted() -> None:
    client = FakeAnthropicClient(error=FakeStatusError(500, f"denied for key {API_KEY}"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    message = str(excinfo.value)
    assert API_KEY not in message
    assert "[redacted]" in message


def test_anthropic_missing_usage_is_billed_usage_missing() -> None:
    client = FakeAnthropicClient(result=anthropic_message(with_usage=False))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "usage_missing"
    assert excinfo.value.billed is True


def test_anthropic_bool_usage_is_rejected() -> None:
    client = FakeAnthropicClient(
        result=anthropic_message(input_tokens=True, output_tokens=3)
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "usage_missing"


def test_anthropic_timeout_is_billed() -> None:
    client = FakeAnthropicClient(hang=True)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client, timeout=0).complete(REQUEST))
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.billed is True


def test_anthropic_cancellation_propagates() -> None:
    client = FakeAnthropicClient(hang=True)
    backend = anthropic_backend(client, timeout=60)

    async def scenario() -> None:
        task = asyncio.create_task(backend.complete(REQUEST))
        while not client.calls:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.skipif(
    importlib.util.find_spec("anthropic") is not None,
    reason="the anthropic SDK is installed in this environment",
)
def test_anthropic_missing_sdk_is_a_usage_error() -> None:
    backend = AnthropicBackend(
        connection_id="anthro", api_key=API_KEY, timeout_seconds=30
    )
    with pytest.raises(SpecError, match="live"):
        backend.ensure_ready()


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------

_RAISE = object()


class FakeHttpResponse:
    def __init__(
        self, status_code: int = 200, body: object = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self) -> object:
        if self._body is _RAISE:
            raise ValueError("not json")
        return self._body


def compat_body(
    *,
    content: object = "hi there",
    prompt_tokens: object = 11,
    completion_tokens: object = 4,
    with_usage: bool = True,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "chatcmpl-42",
        "model": "served-model",
        "choices": [{"message": {"role": "assistant", "content": content}}],
    }
    if with_usage:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
    return body


class FakeHttpClient:
    def __init__(
        self,
        response: FakeHttpResponse | None = None,
        error: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []
        self._response = response
        self._error = error
        self._hang = hang

    async def post(self, url: str, json: dict[str, object]) -> FakeHttpResponse:
        self.posts.append((url, json))
        if self._hang:
            await asyncio.Event().wait()
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def compat_backend(client: FakeHttpClient, *, timeout: int = 30) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        connection_id="compat",
        base_url="https://api.example.test/v1/",
        api_key=API_KEY,
        timeout_seconds=timeout,
        monotonic=SteppingMonotonic(step_ms=7),
        client_factory=lambda: client,
    )


def test_compat_success_normalizes_url_payload_usage_and_receipt() -> None:
    client = FakeHttpClient(response=FakeHttpResponse(body=compat_body()))
    result = asyncio.run(compat_backend(client).complete(REQUEST))
    assert result.text == "hi there"
    assert result.model == "served-model"
    assert result.usage == Usage(input_tokens=11, output_tokens=4)
    assert result.receipt is not None
    assert result.receipt.backend_kind == "openai_compat"
    assert result.receipt.provider_request_id == "chatcmpl-42"
    [(url, payload)] = client.posts
    assert url == "https://api.example.test/v1/chat/completions"
    assert payload["model"] == "test-model"
    assert payload["max_tokens"] == 64
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.parametrize(
    ("status", "kind", "billed"),
    [(401, "auth", False), (429, "rate_limit", False), (500, "api", True)],
)
def test_compat_http_status_mapping(status: int, kind: str, billed: bool) -> None:
    client = FakeHttpClient(
        response=FakeHttpResponse(status_code=status, body={}, text="upstream error")
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == kind
    assert excinfo.value.billed is billed


def test_compat_error_body_is_redacted() -> None:
    client = FakeHttpClient(
        response=FakeHttpResponse(status_code=500, body={}, text=f"bad key {API_KEY}")
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert API_KEY not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


def test_compat_transport_error_is_connection_and_billed() -> None:
    client = FakeHttpClient(error=OSError("connection reset"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "connection"
    assert excinfo.value.billed is True


def test_compat_invalid_json_is_billed_protocol_error() -> None:
    client = FakeHttpClient(response=FakeHttpResponse(body=_RAISE))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "protocol"
    assert excinfo.value.billed is True


def test_compat_missing_choices_is_protocol_error() -> None:
    client = FakeHttpClient(response=FakeHttpResponse(body={"usage": {}}))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "protocol"


def test_compat_missing_usage_is_billed_usage_missing() -> None:
    client = FakeHttpClient(response=FakeHttpResponse(body=compat_body(with_usage=False)))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "usage_missing"
    assert excinfo.value.billed is True


def test_compat_timeout_is_billed() -> None:
    client = FakeHttpClient(hang=True)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client, timeout=0).complete(REQUEST))
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.billed is True


def test_compat_cancellation_propagates() -> None:
    client = FakeHttpClient(hang=True)
    backend = compat_backend(client, timeout=60)

    async def scenario() -> None:
        task = asyncio.create_task(backend.complete(REQUEST))
        while not client.posts:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.skipif(
    importlib.util.find_spec("httpx") is not None,
    reason="httpx is installed in this environment",
)
def test_compat_missing_httpx_is_a_usage_error() -> None:
    backend = OpenAICompatBackend(
        connection_id="compat",
        base_url="https://api.example.test/v1",
        api_key=API_KEY,
        timeout_seconds=30,
    )
    with pytest.raises(SpecError, match="live"):
        backend.ensure_ready()
