"""Backend-boundary proofs with an independent, delayed, capturing ChatBackend.

Unlike the persisted-snapshot checks in test_e2e.py, these assertions inspect
the PreparedRequest objects the backend *actually receives*, and the barrier is
proven against a proposer that stays deliberately slow on the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council
from dagvane.domain.models import BackendError, RunStatus, Usage, estimate_tokens
from dagvane.ports.backend import ChatResult, PreparedRequest
from dagvane.protocol.documents import FixtureResponse, load_fixture_file, load_task_file
from helpers import FIXTURE_HAPPY, TASK_BASIC


class RecordingBackend:
    """A second, independent ChatBackend implementation for adversarial tests.

    Captures every PreparedRequest, can hold selected models slow (yielding the
    event loop many times so any broken barrier would be exploited), and can
    fail selected models outright.
    """

    def __init__(
        self,
        responses: Mapping[str, FixtureResponse],
        *,
        slow_models: frozenset[str] = frozenset(),
        fail_models: frozenset[str] = frozenset(),
        yields: int = 50,
    ) -> None:
        self._responses = dict(responses)
        self._slow = slow_models
        self._fail = fail_models
        self._yields = yields
        self.requests: list[PreparedRequest] = []
        self.timeline: list[tuple[str, str]] = []

    async def complete(self, request: PreparedRequest) -> ChatResult:
        self.requests.append(request)
        self.timeline.append(("request", request.model))
        if request.model in self._slow:
            for _ in range(self._yields):
                await asyncio.sleep(0)
        if request.model in self._fail:
            self.timeline.append(("failed", request.model))
            raise BackendError(f"injected failure for {request.model}")
        response = self._responses[request.model]
        usage = response.usage
        if usage is None:
            usage = Usage(
                input_tokens=estimate_tokens(request.system + request.user_text),
                output_tokens=estimate_tokens(response.text),
            )
        self.timeline.append(("response", request.model))
        return ChatResult(model=request.model, text=response.text, usage=usage)


def _run(backend: RecordingBackend, tmp_path: Path) -> RunStatus:
    fixture = load_fixture_file(FIXTURE_HAPPY)
    result = run_council(
        task=load_task_file(TASK_BASIC),
        fixture=fixture,
        store=FilesystemRunStore(tmp_path),
        backend=backend,
        sink=None,
    )
    return result.status


def _happy_backend(
    *,
    slow_models: frozenset[str] = frozenset(),
    fail_models: frozenset[str] = frozenset(),
) -> RecordingBackend:
    return RecordingBackend(
        load_fixture_file(FIXTURE_HAPPY).responses,
        slow_models=slow_models,
        fail_models=fail_models,
    )


def _request_for(backend: RecordingBackend, model: str) -> PreparedRequest:
    matches = [request for request in backend.requests if request.model == model]
    assert len(matches) == 1, f"expected exactly one request for {model}"
    return matches[0]


def test_barrier_holds_against_a_slow_proposer(tmp_path: Path) -> None:
    backend = _happy_backend(slow_models=frozenset({"fake-proposer-a"}))
    assert _run(backend, tmp_path) is RunStatus.COMPLETED
    proposer_done = max(
        backend.timeline.index(("response", "fake-proposer-a")),
        backend.timeline.index(("response", "fake-proposer-b")),
    )
    for reviewer in ("fake-reviewer-a", "fake-reviewer-b"):
        assert backend.timeline.index(("request", reviewer)) > proposer_done, (
            "a review was dispatched before both proposals completed: "
            f"{backend.timeline}"
        )
    reviews_done = max(
        backend.timeline.index(("response", "fake-reviewer-a")),
        backend.timeline.index(("response", "fake-reviewer-b")),
    )
    assert backend.timeline.index(("request", "fake-judge")) > reviews_done


def test_backend_received_proposer_requests_are_isolated(tmp_path: Path) -> None:
    responses = load_fixture_file(FIXTURE_HAPPY).responses
    backend = RecordingBackend(responses, slow_models=frozenset({"fake-proposer-a"}))
    assert _run(backend, tmp_path) is RunStatus.COMPLETED
    proposal_a = responses["fake-proposer-a"].text
    proposal_b = responses["fake-proposer-b"].text
    for model, sibling in (
        ("fake-proposer-a", proposal_b),
        ("fake-proposer-b", proposal_a),
    ):
        request = _request_for(backend, model)
        assert "Pick a storage layout" in request.user_text  # the task is there
        assert sibling not in request.user_text
        assert "candidate-" not in request.user_text
        assert "review" not in request.user_text


def test_backend_received_reviews_are_blind_and_never_self(tmp_path: Path) -> None:
    responses = load_fixture_file(FIXTURE_HAPPY).responses
    backend = RecordingBackend(responses, slow_models=frozenset({"fake-proposer-a"}))
    assert _run(backend, tmp_path) is RunStatus.COMPLETED
    proposal_a = responses["fake-proposer-a"].text
    proposal_b = responses["fake-proposer-b"].text
    review_a = _request_for(backend, "fake-reviewer-a")
    assert proposal_b in review_a.user_text  # the opposite proposal, fully rendered
    assert proposal_a not in review_a.user_text  # never its own identity's proposal
    review_b = _request_for(backend, "fake-reviewer-b")
    assert proposal_a in review_b.user_text
    assert proposal_b not in review_b.user_text
    judge = _request_for(backend, "fake-judge")
    assert proposal_a in judge.user_text and proposal_b in judge.user_text
    for request in (review_a, review_b, judge):
        assert "proposer-a" not in request.user_text  # sealed identities
        assert "proposer-b" not in request.user_text


def test_failed_proposer_starves_reviews_and_judge(tmp_path: Path) -> None:
    backend = _happy_backend(fail_models=frozenset({"fake-proposer-a"}))
    assert _run(backend, tmp_path) is RunStatus.FAILED
    # The backend saw exactly the two proposers — no degraded one-candidate
    # council ever reached a reviewer or the judge.
    assert sorted(request.model for request in backend.requests) == [
        "fake-proposer-a",
        "fake-proposer-b",
    ]
