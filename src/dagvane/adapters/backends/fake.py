"""Deterministic fixture-driven fake backend. Tests/CI only — never a product route."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from dagvane.domain.models import BackendError, Usage, estimate_tokens
from dagvane.ports.backend import ChatResult, PreparedRequest
from dagvane.protocol.documents import FixtureResponse
from dagvane.protocol.frames import sha256_hex


@dataclass(frozen=True, slots=True)
class FakeInvocation:
    model: str
    request_sha256: str


class FakeBackend:
    """Responds from a fixture keyed by model id; knows nothing about nodes or plans."""

    def __init__(self, responses: Mapping[str, FixtureResponse]) -> None:
        self._responses = dict(responses)
        self.invocations: list[FakeInvocation] = []

    async def complete(self, request: PreparedRequest) -> ChatResult:
        request_sha = sha256_hex(
            (request.model + "\x00" + request.system + "\x00" + request.user_text).encode("utf-8")
        )
        self.invocations.append(FakeInvocation(model=request.model, request_sha256=request_sha))
        response = self._responses.get(request.model)
        if response is None:
            raise BackendError(f"fixture has no response for model {request.model!r}")
        usage = response.usage
        if usage is None:
            usage = Usage(
                input_tokens=estimate_tokens(request.system + request.user_text),
                output_tokens=estimate_tokens(response.text),
            )
        return ChatResult(model=request.model, text=response.text, usage=usage)
