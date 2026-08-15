"""Local (Ollama) model integration: capability probe and bounded use (MVP).

The local model is LOCAL_FAST: file classification, summarization, failure
grouping, context compression. It is never routed substantial implementation,
architecture, security review, or release acceptance. The probe is a single
short deterministic task with a tight timeout; on failure the session marks
LOCAL unavailable and the router falls back to the CHEAP tier — no retries.
"""

from __future__ import annotations

import asyncio

from dagvane.adapters.backends.openai_compat import OpenAICompatBackend
from dagvane.application.resources import ResourceCatalog, ResourceSpec
from dagvane.domain.models import DagvaneError
from dagvane.ports.backend import PreparedRequest

PROBE_TIMEOUT_SECONDS = 15
_PROBE_EXPECTED = "READY"


def _build_backend(resource: ResourceSpec, timeout_seconds: int) -> OpenAICompatBackend:
    base_url = resource.base_url or "http://127.0.0.1:11434/v1"
    # Loopback cleartext is admitted without opt-in; the key is a placeholder
    # (Ollama ignores it) and local token billing is zero.
    return OpenAICompatBackend(
        connection_id=resource.resource_id,
        base_url=base_url,
        api_key="ollama-local",
        timeout_seconds=timeout_seconds,
    )


async def _complete(backend: OpenAICompatBackend, request: PreparedRequest) -> str:
    try:
        result = await backend.complete(request)
        return result.text
    finally:
        await backend.aclose()


def probe_local_model(catalog: ResourceCatalog, resource_id: str = "ollama-local") -> bool:
    """One bounded capability probe; marks LOCAL availability on the catalog."""
    try:
        resource = catalog.get(resource_id)
    except DagvaneError:
        catalog.mark_local_available(False)
        return False
    request = PreparedRequest(
        model=resource.model or "qwen2.5-coder:3b",
        max_output_tokens=16,
        system="You are a classification assistant. Follow instructions exactly.",
        user_text='Reply with exactly the single word: READY',
    )
    try:
        text = asyncio.run(
            _complete(_build_backend(resource, PROBE_TIMEOUT_SECONDS), request)
        )
    except (DagvaneError, OSError):
        catalog.mark_local_available(False)
        return False
    available = _PROBE_EXPECTED in text.upper()
    catalog.mark_local_available(available)
    return available


def summarize_locally(
    catalog: ResourceCatalog,
    text: str,
    *,
    instruction: str = "Summarize the following tool output in at most 10 bullet "
    "points, keeping exact error messages and file paths.",
    resource_id: str = "ollama-local",
    timeout_seconds: int = 60,
    max_output_tokens: int = 512,
) -> str | None:
    """Bounded local summarization; None when LOCAL is unavailable or fails."""
    if not catalog.local_available:
        return None
    try:
        resource = catalog.get(resource_id)
        request = PreparedRequest(
            model=resource.model or "qwen2.5-coder:3b",
            max_output_tokens=max_output_tokens,
            system="You compress tool output faithfully. Never invent content.",
            user_text=f"{instruction}\n\n```\n{text[-12000:]}\n```",
        )
        return asyncio.run(
            _complete(_build_backend(resource, timeout_seconds), request)
        )
    except (DagvaneError, OSError):
        return None
