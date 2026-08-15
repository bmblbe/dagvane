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


# ---------------------------------------------------------------------------
# Adversarial-review regression tests (G1 review round 1)
# ---------------------------------------------------------------------------


def test_anthropic_2xx_bearing_exception_is_billed_protocol_failure() -> None:
    """An SDK exception carrying a 2xx status means a delivered-but-unusable
    response: the provider processed the call, so it bills at the ceiling."""
    client = FakeAnthropicClient(error=FakeStatusError(200, "malformed body"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "protocol"
    assert excinfo.value.billed is True


# A legal printable-ASCII credential the CLI admits: quotes, backslashes,
# and characters whose JSON/repr renderings differ from the raw bytes.
TRICKY_KEY = 'sk-tr"ick\\y\'key-11'


@pytest.mark.parametrize(
    "encode",
    [
        lambda s: s,  # raw reflection
        lambda s: s.encode("unicode_escape").decode("ascii"),  # transport repr()
        lambda s: __import__("json").dumps(s)[1:-1],  # JSON-escaped reflection
        lambda s: repr(s)[1:-1],  # mixed-quote repr rendering
    ],
    ids=["raw", "unicode-escape", "json-escape", "repr"],
)
def test_redaction_covers_admitted_credential_encodings(encode: Any) -> None:
    """Every encoded rendering of a legal printable credential must be
    scrubbed from normalized error text (Codex B2)."""
    reflected = encode(TRICKY_KEY)
    client = FakeAnthropicClient(error=FakeStatusError(500, f"illegal header {reflected}"))
    backend = AnthropicBackend(
        connection_id="anthro",
        api_key=TRICKY_KEY,
        timeout_seconds=30,
        monotonic=SteppingMonotonic(),
        client_factory=lambda: client,
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(backend.complete(REQUEST))
    message = str(excinfo.value)
    assert TRICKY_KEY not in message
    assert reflected not in message
    assert "[redacted]" in message


def test_compat_snippet_truncation_cannot_expose_a_credential_prefix() -> None:
    """Scrub-before-truncate: a credential straddling the 300-char body
    snippet boundary must not survive as an identifying prefix (Codex B2)."""
    body = "x" * 290 + TRICKY_KEY + "y" * 100
    client = FakeHttpClient(response=FakeHttpResponse(status_code=503, text=body))
    backend = OpenAICompatBackend(
        connection_id="compat",
        base_url="https://api.example.test/v1",
        api_key=TRICKY_KEY,
        timeout_seconds=30,
        monotonic=SteppingMonotonic(),
        client_factory=lambda: client,
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(backend.complete(REQUEST))
    message = str(excinfo.value)
    assert TRICKY_KEY not in message
    # No identifying prefix either: the longest visible run of key bytes must
    # be far shorter than the credential.
    assert TRICKY_KEY[:8] not in message


def test_adapter_error_messages_are_bounded() -> None:
    """Journal frames are capped at 1 MiB; provider error text must be bounded
    long before that."""
    client = FakeAnthropicClient(error=FakeStatusError(500, "x" * 2_000_000))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert len(str(excinfo.value)) < 3000
    assert "truncated" in str(excinfo.value)


def test_compat_pre_send_connection_failure_is_not_billed() -> None:
    """A ConnectError proves the request never left this machine."""

    class ConnectError(Exception):
        pass

    client = FakeHttpClient(error=ConnectError("dns failure"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "connection"
    assert excinfo.value.billed is False


def test_compat_read_timeout_is_billed_timeout() -> None:
    class ReadTimeout(Exception):
        pass

    client = FakeHttpClient(error=ReadTimeout("read timed out"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "timeout"
    assert excinfo.value.billed is True


def test_anthropic_pre_send_cause_is_not_billed() -> None:
    class ConnectError(Exception):
        pass

    sdk_error = RuntimeError("connection error")
    sdk_error.__cause__ = ConnectError("refused")
    client = FakeAnthropicClient(error=sdk_error)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "connection"
    assert excinfo.value.billed is False


# ---------------------------------------------------------------------------
# Codex G1 acceptance-review regressions (round 2 hardening)
# ---------------------------------------------------------------------------


def test_compat_pool_timeout_is_pre_send_and_not_billed() -> None:
    """httpcore raises PoolTimeout while *waiting to acquire* a connection —
    before the request is physically sent. Billing it at ceiling would consume
    budget for a request the provider never received (Codex M2)."""

    class PoolTimeout(Exception):
        pass

    client = FakeHttpClient(error=PoolTimeout("pool exhausted"))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.billed is False


def test_anthropic_pool_timeout_cause_is_pre_send_and_not_billed() -> None:
    class PoolTimeout(Exception):
        pass

    sdk_error = RuntimeError("transport error")
    sdk_error.__cause__ = PoolTimeout("pool exhausted")
    client = FakeAnthropicClient(error=sdk_error)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.billed is False


@pytest.mark.parametrize("adapter", ["anthropic", "compat"])
def test_client_construction_failure_is_normalized_and_redacted(adapter: str) -> None:
    """A constructor exception may echo credential material; it must surface
    as a normalized, redacted, not-billed dispatch error (Codex M5)."""

    def exploding_factory() -> Any:
        raise ValueError(f"illegal header value: Bearer {TRICKY_KEY}")

    backend: Any
    if adapter == "anthropic":
        backend = AnthropicBackend(
            connection_id="anthro",
            api_key=TRICKY_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=exploding_factory,
        )
    else:
        backend = OpenAICompatBackend(
            connection_id="compat",
            base_url="https://api.example.test/v1",
            api_key=TRICKY_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=exploding_factory,
        )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(backend.complete(REQUEST))
    assert excinfo.value.kind == "connection"
    assert excinfo.value.billed is False
    message = str(excinfo.value)
    assert TRICKY_KEY not in message
    assert "[redacted]" in message


def test_anthropic_partial_usage_is_preserved_on_missing_component() -> None:
    """A known provider-reported component must never be discarded because the
    other component is missing (Codex B4)."""
    message = anthropic_message(input_tokens=100_000, output_tokens=None)
    client = FakeAnthropicClient(result=message)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "usage_missing"
    assert excinfo.value.billed is True
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100_000
    assert excinfo.value.usage.output_tokens is None


def test_compat_malformed_choices_keeps_complete_reported_usage() -> None:
    """A billed 200 whose choices are unusable must still carry the provider's
    complete reported usage (Codex B4)."""
    body = compat_body(prompt_tokens=100_000, completion_tokens=17)
    body["choices"] = "not-a-list"
    client = FakeHttpClient(response=FakeHttpResponse(body=body))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "protocol"
    assert excinfo.value.billed is True
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100_000
    assert excinfo.value.usage.output_tokens == 17


def test_compat_partial_usage_is_preserved_on_missing_component() -> None:
    body = compat_body(prompt_tokens=100_000, completion_tokens=None)
    client = FakeHttpClient(response=FakeHttpResponse(body=body))
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "usage_missing"
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100_000
    assert excinfo.value.usage.output_tokens is None


def test_compat_error_body_usage_is_preserved() -> None:
    """A failed HTTP response that reports usage is reporting what the
    provider may bill; the known components must ride on the error (Codex B4
    round 2)."""
    body = {"error": "overloaded", "usage": {"prompt_tokens": 100_000, "completion_tokens": 17}}
    client = FakeHttpClient(
        response=FakeHttpResponse(status_code=503, body=body, text="overloaded")
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "api"
    assert excinfo.value.billed is True
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100_000
    assert excinfo.value.usage.output_tokens == 17


def test_compat_non_json_error_body_still_normalizes() -> None:
    client = FakeHttpClient(
        response=FakeHttpResponse(status_code=503, body=_RAISE, text="plain text")
    )
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat_backend(client).complete(REQUEST))
    assert excinfo.value.usage is None


def test_anthropic_status_exception_body_usage_is_preserved() -> None:
    """A status-bearing SDK exception whose body reports usage must not lose
    the provider's actuals (Codex B4 round 2)."""
    error = FakeStatusError(200, "malformed but billed")
    error.body = {"usage": {"input_tokens": 100_000, "output_tokens": 17}}  # type: ignore[attr-defined]
    client = FakeAnthropicClient(error=error)
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(anthropic_backend(client).complete(REQUEST))
    assert excinfo.value.kind == "protocol"
    assert excinfo.value.billed is True
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.input_tokens == 100_000
    assert excinfo.value.usage.output_tokens == 17


def test_watchdog_runs_behind_the_transport_timers() -> None:
    """The adapter watchdog must never race the transport's own (precise,
    pre-send-classifying) timers at equal deadlines (Codex M2 round 2)."""
    from dagvane.adapters.backends.common import WATCHDOG_GRACE_SECONDS, watchdog_seconds

    assert WATCHDOG_GRACE_SECONDS > 0
    assert watchdog_seconds(30) == 30 + WATCHDOG_GRACE_SECONDS


def test_success_fields_are_scrubbed_before_leaving_the_adapter() -> None:
    """Successful response content, model name, and provider request id are
    provider-derived and must be scrubbed (Codex B3)."""
    reflected = TRICKY_KEY
    message = SimpleNamespace(
        id=f"msg-{reflected}",
        model=f"model-{reflected}",
        content=[SimpleNamespace(type="text", text=f"echo: {reflected}")],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )
    client = FakeAnthropicClient(result=message)
    backend = AnthropicBackend(
        connection_id="anthro",
        api_key=TRICKY_KEY,
        timeout_seconds=30,
        monotonic=SteppingMonotonic(),
        client_factory=lambda: client,
    )
    result = asyncio.run(backend.complete(REQUEST))
    assert TRICKY_KEY not in result.text
    assert TRICKY_KEY not in result.model
    assert result.receipt is not None
    assert result.receipt.provider_request_id is not None
    assert TRICKY_KEY not in result.receipt.provider_request_id


def test_compat_max_tokens_field_is_configurable() -> None:
    client = FakeHttpClient(response=FakeHttpResponse(body=compat_body()))
    backend = OpenAICompatBackend(
        connection_id="compat",
        base_url="https://api.example.test/v1",
        api_key=API_KEY,
        timeout_seconds=30,
        max_tokens_field="max_completion_tokens",
        monotonic=SteppingMonotonic(),
        client_factory=lambda: client,
    )
    asyncio.run(backend.complete(REQUEST))
    [(_, payload)] = client.posts
    assert payload["max_completion_tokens"] == 64
    assert "max_tokens" not in payload


def test_ensure_ready_with_injected_client_needs_no_dependency() -> None:
    """ensure_ready only checks importability; a factory bypasses it entirely."""
    client = FakeAnthropicClient(result=anthropic_message())
    anthropic_backend(client).ensure_ready()  # must not raise
    compat_backend(FakeHttpClient(response=FakeHttpResponse(body=compat_body()))).ensure_ready()


def test_aclose_releases_the_constructed_client() -> None:
    closed = []

    class ClosingClient(FakeHttpClient):
        async def aclose(self) -> None:
            closed.append(True)

    client = ClosingClient(response=FakeHttpResponse(body=compat_body()))
    backend = compat_backend(client)
    asyncio.run(backend.complete(REQUEST))
    asyncio.run(backend.aclose())
    assert closed == [True]
