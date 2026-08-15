"""Mixed-backend live councils via run_council_live — real adapters, fake clients.

Proves backend routing, receipts, billed-failure accounting, secret hygiene,
and honest budget enforcement from provider-reported usage, all offline.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dagvane.adapters.backends.anthropic import AnthropicBackend
from dagvane.adapters.backends.openai_compat import OpenAICompatBackend
from dagvane.adapters.storage.filesystem import FilesystemRunStore
from dagvane.application.council import run_council_live
from dagvane.domain.models import PlanValidationError, RunStatus
from dagvane.ports.backend import ChatBackend
from dagvane.ports.runtime import FixedClock, SequentialIds, SteppingMonotonic
from dagvane.protocol.documents import load_task_file
from dagvane.protocol.profiles import parse_profile
from helpers import TASK_BASIC

ANTHRO_KEY = "sk-ant-test-secret-000111"
COMPAT_KEY = "sk-oai-test-secret-222333"

DECISION_TEXT = json.dumps(
    {"decision_version": 1, "winner": "candidate-1", "rationale": "clearer plan"}
)

PROFILE_TOML = """\
profile_version = 1

[connections.anthro]
kind = "anthropic"
credential_env = "TEST_ANTHRO_KEY"

[connections.compat]
kind = "openai_compat"
base_url = "https://api.example.test/v1"
credential_env = "TEST_COMPAT_KEY"

[routes.anthro-writer]
connection = "anthro"
model = "anthro-model"
max_output_tokens = 512
input_microusd_per_mtok = 3000000
output_microusd_per_mtok = 15000000

[routes.compat-writer]
connection = "compat"
model = "compat-model"
max_output_tokens = 512
input_microusd_per_mtok = 1000000
output_microusd_per_mtok = 2000000

[routes.anthro-judge]
connection = "anthro"
model = "anthro-judge"
max_output_tokens = 512
input_microusd_per_mtok = 3000000
output_microusd_per_mtok = 15000000

[council]
proposer_a = "anthro-writer"
proposer_b = "compat-writer"
reviewer_a = "compat-writer"
reviewer_b = "anthro-writer"
judge = "anthro-judge"
"""


class ScriptedAnthropicClient:
    """Returns per-model scripted texts with fixed usage."""

    def __init__(self, texts: dict[str, str], usage: tuple[int, int] = (20, 10)) -> None:
        self._texts = texts
        self._usage = usage
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any) -> object:
        model = kwargs["model"]
        text = self._texts[model]
        return SimpleNamespace(
            id=f"msg-{model}",
            model=model,
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(
                input_tokens=self._usage[0], output_tokens=self._usage[1]
            ),
        )


class ScriptedHttpClient:
    """Returns per-model scripted OpenAI-compatible bodies, or an HTTP error."""

    def __init__(
        self,
        texts: dict[str, str],
        usage: tuple[int, int] = (15, 5),
        fail_with_status: int | None = None,
    ) -> None:
        self._texts = texts
        self._usage = usage
        self._fail_with_status = fail_with_status

    async def post(self, url: str, json: dict[str, object]) -> object:
        model = str(json["model"])
        if self._fail_with_status is not None:
            return SimpleNamespace(
                status_code=self._fail_with_status,
                text="upstream unavailable",
                json=lambda: {},
            )
        body = {
            "id": f"cmpl-{model}",
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": self._texts[model]}}],
            "usage": {
                "prompt_tokens": self._usage[0],
                "completion_tokens": self._usage[1],
            },
        }
        return SimpleNamespace(status_code=200, text="", json=lambda: body)


def build_backends(
    anthro_client: object, compat_client: object
) -> dict[str, ChatBackend]:
    return {
        "anthro": AnthropicBackend(
            connection_id="anthro",
            api_key=ANTHRO_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=lambda: anthro_client,
        ),
        "compat": OpenAICompatBackend(
            connection_id="compat",
            base_url="https://api.example.test/v1",
            api_key=COMPAT_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=lambda: compat_client,
        ),
    }


def run_live(
    tmp_path: Path,
    backends: dict[str, ChatBackend],
    task_file: Path = TASK_BASIC,
) -> tuple[Any, Path]:
    profile = parse_profile(PROFILE_TOML.encode("utf-8"), source="test")
    store = FilesystemRunStore(tmp_path)
    result = run_council_live(
        task=load_task_file(task_file),
        profile=profile,
        backends=backends,
        store=store,
        clock=FixedClock("2026-08-15T12:00:00.000Z", 250),
        ids=SequentialIds("live"),
    )
    return result, tmp_path / ".dagvane" / "runs" / result.run_id


def journal_events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "events.jsonl").read_bytes().splitlines()
    return [json.loads(line) for line in lines]


def test_mixed_backend_council_completes_with_receipts(tmp_path: Path) -> None:
    anthro = ScriptedAnthropicClient(
        {"anthro-model": "proposal/review text", "anthro-judge": DECISION_TEXT}
    )
    compat = ScriptedHttpClient({"compat-model": "compat proposal/review text"})
    result, run_dir = run_live(tmp_path, build_backends(anthro, compat))

    assert result.status is RunStatus.COMPLETED
    events = journal_events(run_dir)
    receipts = [
        e for e in events if e["type"] == "artifact.written" and e["data"]["role"] == "receipt"
    ]
    assert len(receipts) == 5  # one per dispatched node

    # Receipt contents bind route, connection, and request/response hashes.
    by_sha = {e["data"]["sha256"]: e for e in receipts}
    artifacts_dir = run_dir / "artifacts"
    kinds = set()
    for sha in by_sha:
        doc = json.loads((artifacts_dir / sha).read_bytes())
        kinds.add(doc["backend_kind"])
        assert doc["receipt_version"] == 1
        assert doc["connection_id"] in ("anthro", "compat")
        assert doc["request_sha256"]
        assert doc["response_sha256"]
        assert doc["usage_source"] == "provider"
        assert doc["error_kind"] is None
        assert isinstance(doc["route_fingerprint"], str)
    assert kinds == {"anthropic", "openai_compat"}

    # Committed totals equal the sum of provider-reported usage.
    committed = result.report_doc["budget"]["committed"]
    assert committed["calls"] == 5
    # anthro nodes: proposer-a, reviewer-b, judge; compat nodes: proposer-b, reviewer-a
    assert committed["input_tokens"] == 3 * 20 + 2 * 15
    assert committed["output_tokens"] == 3 * 10 + 2 * 5
    assert (run_dir / "decision.json").exists()


def test_no_credential_value_reaches_durable_state(tmp_path: Path) -> None:
    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}
    )
    compat = ScriptedHttpClient({"compat-model": "text"})
    _, run_dir = run_live(tmp_path, build_backends(anthro, compat))
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            assert ANTHRO_KEY.encode() not in data, path
            assert COMPAT_KEY.encode() not in data, path


def test_billed_backend_failure_is_committed_at_ceiling(tmp_path: Path) -> None:
    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}
    )
    compat = ScriptedHttpClient({}, fail_with_status=503)
    result, run_dir = run_live(tmp_path, build_backends(anthro, compat))

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    failed = [e for e in events if e["type"] == "model.failed"]
    # proposer-b fails; reviewer-a (same compat route) becomes dependency_failed
    # before dispatch, so exactly one billed failure exists.
    assert len(failed) == 1
    data = failed[0]["data"]
    assert data["reason"] == "api"
    assert data["usage_source"] == "ceiling"
    assert data["billed_output_tokens"] == 512  # the route's max_output_tokens
    assert data["billed_cost_microusd"] > 0

    # The billed ceiling is part of the run's committed totals.
    committed = result.report_doc["budget"]["committed"]
    assert committed["cost_microusd"] >= data["billed_cost_microusd"]

    # A failure receipt was persisted for the billed dispatch.
    receipts = [
        e for e in events if e["type"] == "artifact.written" and e["data"]["role"] == "receipt"
    ]
    failure_receipts = []
    for event in receipts:
        doc = json.loads((run_dir / "artifacts" / event["data"]["sha256"]).read_bytes())
        if doc["error_kind"] is not None:
            failure_receipts.append(doc)
    assert len(failure_receipts) == 1
    assert failure_receipts[0]["error_kind"] == "api"
    assert failure_receipts[0]["response_sha256"] is None


def test_budget_overrun_from_provider_usage_fails_honestly(tmp_path: Path) -> None:
    task_file = TASK_BASIC.parent / "task_low_budget.json"  # max_calls: 2
    # Huge provider-reported input usage must overrun the token cap at commit.
    task = json.loads(TASK_BASIC.read_text())
    task["budget"] = {"max_calls": 60, "max_total_tokens": 5000, "max_cost_microusd": 8000000}
    custom_task = tmp_path / "task_tight_tokens.json"
    custom_task.write_text(json.dumps(task))

    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}, usage=(100_000, 10)
    )
    compat = ScriptedHttpClient({"compat-model": "text"})
    result, run_dir = run_live(tmp_path, build_backends(anthro, compat), custom_task)

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    completed = [e for e in events if e["type"] == "model.completed"]
    # The overrunning usage was journaled honestly before the node failed.
    assert any(e["data"]["input_tokens"] == 100_000 for e in completed)
    failed_nodes = [e for e in events if e["type"] == "node.failed"]
    assert any(e["data"]["reason"] == "budget_exceeded" for e in failed_nodes)
    assert task_file.exists()  # silence unused-variable style checks


def test_route_backend_mismatch_fails_before_any_run_state(tmp_path: Path) -> None:
    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}
    )
    backends = build_backends(anthro, ScriptedHttpClient({"compat-model": "t"}))
    del backends["compat"]
    profile = parse_profile(PROFILE_TOML.encode("utf-8"), source="test")
    store = FilesystemRunStore(tmp_path)
    with pytest.raises(PlanValidationError, match="unknown backend connections"):
        run_council_live(
            task=load_task_file(TASK_BASIC),
            profile=profile,
            backends=backends,
            store=store,
        )
    assert not (tmp_path / ".dagvane").exists()


def test_live_runs_replay_through_the_events_command(tmp_path: Path) -> None:
    """The live journal replays byte-for-byte through the same fold as G0."""
    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}
    )
    compat = ScriptedHttpClient({"compat-model": "text"})
    result, run_dir = run_live(tmp_path, build_backends(anthro, compat))

    from dagvane.application.replay import fold_frames, rebuild_report

    frames = (run_dir / "events.jsonl").read_bytes().splitlines(keepends=True)
    view = fold_frames(iter(frames))
    assert rebuild_report(view) == json.loads((run_dir / "report.json").read_bytes())
    assert view.status is RunStatus.COMPLETED


def test_cancelled_dispatch_commits_the_reservation_ceiling() -> None:
    """Cancellation of a potentially-sent dispatch must not silently release
    the reservation: it is committed at the ceiling and durably closed with
    ``model.failed(reason="cancelled", usage_source="ceiling")``."""
    from dagvane.application.council import (
        BudgetLedger,
        CouncilTemplate,
        OneShotModelWorker,
        ResolvedInput,
    )
    from dagvane.domain.models import ArtifactRef, Attempt, Budget, BudgetRejectedError

    class MemoryArtifacts:
        def put(self, data: bytes, *, media_type: str, role: str) -> ArtifactRef:
            return ArtifactRef(
                sha256="a" * 64, size=len(data), media_type=media_type, role=role
            )

        def load(self, sha256: str) -> bytes:
            raise AssertionError("not exercised by this test")

    class HangingBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, request: Any) -> Any:
            self.started.set()
            await asyncio.Event().wait()

    class Ids:
        def __init__(self) -> None:
            self.n = 0

        def new_id(self, kind: str) -> str:
            self.n += 1
            return f"{kind}-{self.n}"

    ledger = BudgetLedger(
        Budget(max_calls=1, max_total_tokens=1_000_000, max_cost_microusd=10_000_000)
    )
    hanging = HangingBackend()
    emitted: list[Any] = []
    worker = OneShotModelWorker(
        backends={"fake": hanging},
        ledger=ledger,
        artifacts=MemoryArtifacts(),
        ids=Ids(),
        emit=lambda payload, **kwargs: emitted.append(payload),
    )
    role_routes = CouncilTemplate.fake_role_routes()
    route = role_routes["proposer_a"]
    plan, _, _ = CouncilTemplate.build(load_task_file(TASK_BASIC).spec)
    node = plan.nodes[0]

    async def scenario() -> None:
        task = asyncio.create_task(
            worker.execute(
                node=node,
                route=route,
                attempt=Attempt(node_id=node.node_id, index=1),
                entries=(ResolvedInput(kind="task", label="task", content="do it"),),
            )
        )
        await hanging.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    # max_calls=1: the dispatch may have been billed, so the slot stays
    # committed — a silent release would underbill.
    with pytest.raises(BudgetRejectedError):
        ledger.reserve(tokens=10, cost_microusd=10)
    failed = [p for p in emitted if getattr(p, "TYPE", "") == "model.failed"]
    assert len(failed) == 1
    assert failed[0].reason == "cancelled"
    assert failed[0].usage_source == "ceiling"
    totals = ledger.totals()
    assert totals.calls == 1
    assert totals.input_tokens + totals.output_tokens == (
        failed[0].billed_input_tokens + failed[0].billed_output_tokens
    )
    assert totals.cost_microusd == failed[0].billed_cost_microusd


def test_overlimit_live_output_is_committed_honestly(tmp_path: Path) -> None:
    """A 200 response whose reported output exceeds the route cap is a billed
    protocol failure: actuals committed, receipt + model.failed journaled."""

    class OverLimitHttpClient:
        async def post(self, url: str, json: dict[str, object]) -> object:
            model = str(json["model"])
            body = {
                "id": f"cmpl-{model}",
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": "way too much"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 9000},
            }
            return SimpleNamespace(status_code=200, text="", json=lambda: body)

    anthro = ScriptedAnthropicClient(
        {"anthro-model": "text", "anthro-judge": DECISION_TEXT}
    )
    result, run_dir = run_live(tmp_path, build_backends(anthro, OverLimitHttpClient()))

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    failed = [e for e in events if e["type"] == "model.failed"]
    assert len(failed) == 1
    data = failed[0]["data"]
    assert data["reason"] == "protocol"
    assert data["usage_source"] == "provider"
    assert data["billed_input_tokens"] == 30
    assert data["billed_output_tokens"] == 9000

    committed = result.report_doc["budget"]["committed"]
    assert committed["output_tokens"] >= 9000  # actuals, not zero
    assert committed["cost_microusd"] >= data["billed_cost_microusd"]

    receipts = [
        e for e in events if e["type"] == "artifact.written" and e["data"]["role"] == "receipt"
    ]
    protocol_receipts = []
    for event in receipts:
        doc = json.loads((run_dir / "artifacts" / event["data"]["sha256"]).read_bytes())
        if doc["error_kind"] == "protocol":
            protocol_receipts.append(doc)
    assert len(protocol_receipts) == 1
    assert protocol_receipts[0]["response_sha256"] is not None
    node_failed = [e for e in events if e["type"] == "node.failed"]
    assert any(e["data"]["reason"] == "backend_error" for e in node_failed)


def test_spontaneous_cancelled_error_fails_the_node_durably(tmp_path: Path) -> None:
    """A backend leaking CancelledError without a cancel request must not
    leave a non-terminal node inside a terminal journal."""

    class LeakyCancelClient:
        def __init__(self, texts: dict[str, str]) -> None:
            self._texts = texts
            self.messages = SimpleNamespace(create=self._create)

        async def _create(self, **kwargs: Any) -> object:
            model = kwargs["model"]
            if model == "anthro-judge":
                raise asyncio.CancelledError()
            text = self._texts[model]
            return SimpleNamespace(
                id=f"msg-{model}",
                model=model,
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=20, output_tokens=10),
            )

    anthro = LeakyCancelClient({"anthro-model": "text"})
    compat = ScriptedHttpClient({"compat-model": "text"})
    result, run_dir = run_live(tmp_path, build_backends(anthro, compat))

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    node_failed = {
        e["node_id"]: e["data"]["reason"] for e in events if e["type"] == "node.failed"
    }
    assert node_failed.get("judge") == "cancelled"
    # The possibly-sent judge dispatch is closed durably at the ceiling — the
    # dispatched call must not vanish from spend.
    model_failed = [
        e for e in events if e["type"] == "model.failed" and e["node_id"] == "judge"
    ]
    assert len(model_failed) == 1
    assert model_failed[0]["data"]["reason"] == "cancelled"
    assert model_failed[0]["data"]["usage_source"] == "ceiling"
    assert events[-1]["type"] == "run.finished"
    assert events[-1]["data"]["status"] == "failed"
    committed = result.report_doc["budget"]["committed"]
    assert committed["calls"] == 5  # four completed + the cancelled judge dispatch

    # The journal replays cleanly — no non-terminal node under a terminal run.
    from dagvane.application.replay import fold_frames

    frames = (run_dir / "events.jsonl").read_bytes().splitlines(keepends=True)
    view = fold_frames(iter(frames))
    assert view.status is RunStatus.FAILED
