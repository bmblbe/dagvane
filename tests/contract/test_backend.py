"""ChatBackend contract on FakeBackend, plus the barrier order observable.

Acceptance criterion 6 (behavioral half): the fake backend's invocation log
proves both proposer dispatches happen before any reviewer dispatch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dagvane.adapters.backends.fake import FakeBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council
from dagvane.domain.models import BackendError, RunStatus, Usage, estimate_tokens
from dagvane.ports.backend import PreparedRequest
from dagvane.protocol.documents import FixtureResponse, load_fixture_file, load_task_file
from helpers import FIXTURE_HAPPY, TASK_BASIC


def _request(model: str) -> PreparedRequest:
    return PreparedRequest(model=model, max_output_tokens=64, system="sys", user_text="hello")


def test_fake_backend_returns_fixture_text_and_usage() -> None:
    backend = FakeBackend(
        {"m": FixtureResponse(text="out", usage=Usage(input_tokens=7, output_tokens=3))}
    )
    result = asyncio.run(backend.complete(_request("m")))
    assert result.model == "m"
    assert result.text == "out"
    assert result.usage == Usage(input_tokens=7, output_tokens=3)


def test_fake_backend_is_deterministic_across_repeats() -> None:
    backend = FakeBackend({"m": FixtureResponse(text="out", usage=None)})
    first = asyncio.run(backend.complete(_request("m")))
    second = asyncio.run(backend.complete(_request("m")))
    assert first == second
    assert backend.invocations[0] == backend.invocations[1]


def test_fake_backend_derives_deterministic_default_usage() -> None:
    backend = FakeBackend({"m": FixtureResponse(text="abcdefgh", usage=None)})
    result = asyncio.run(backend.complete(_request("m")))
    assert result.usage == Usage(
        input_tokens=estimate_tokens("sys" + "hello"),
        output_tokens=estimate_tokens("abcdefgh"),
    )


def test_fake_backend_rejects_unknown_model() -> None:
    backend = FakeBackend({"m": FixtureResponse(text="out", usage=None)})
    with pytest.raises(BackendError, match="no response for model"):
        asyncio.run(backend.complete(_request("other")))
    assert [inv.model for inv in backend.invocations] == ["other"]


def test_barrier_order_in_invocation_log(tmp_path: Path) -> None:
    fixture = load_fixture_file(FIXTURE_HAPPY)
    backend = FakeBackend(fixture.responses)
    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=fixture,
        store=FilesystemRunStore(tmp_path),
        backend=backend,
        sink=None,
    )
    assert result.status is RunStatus.COMPLETED
    models = [invocation.model for invocation in backend.invocations]
    proposer_last = max(models.index(m) for m in ("fake-proposer-a", "fake-proposer-b"))
    reviewer_first = min(
        models.index(m) for m in ("fake-reviewer-a", "fake-reviewer-b")
    )
    assert proposer_last < reviewer_first, models
    assert models.index("fake-judge") > max(
        models.index(m) for m in ("fake-reviewer-a", "fake-reviewer-b")
    )
