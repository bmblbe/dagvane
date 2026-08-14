"""Chat backend port: the wire-protocol boundary as code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from dagvane.domain.models import Usage


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """A fully rendered model request.

    Assembled exclusively from a node's InputManifest; carries no node or
    orchestration identity beyond the target model.
    """

    model: str
    max_output_tokens: int
    system: str
    user_text: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    model: str
    text: str
    usage: Usage


class ChatBackend(Protocol):
    """One-shot completion protocol. Backends own no task loop, tools, or state."""

    async def complete(self, request: PreparedRequest) -> ChatResult: ...
