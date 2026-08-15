"""Codex G1 acceptance-review regressions — full-run probes, fully offline.

Covers: durable secret hygiene across failure *and* success paths including
cross-provider context assembly (B2/B3), truthful partial-usage accounting
(B4), reservation lifecycle on pre-dispatch failures (M4), and adapter client
lifecycle (M1). The credentials used are legal printable-ASCII values the CLI
admits — quotes and backslashes included — and are never echoed in assertion
messages.
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
from dagvane.domain.models import RunStatus
from dagvane.domain.secrets import SecretScrubber
from dagvane.ports.backend import ChatBackend, PreparedRequest
from dagvane.ports.runtime import FixedClock, SequentialIds, SteppingMonotonic
from dagvane.protocol.documents import load_task_file
from dagvane.protocol.profiles import parse_profile
from helpers import TASK_BASIC

# Legal printable-ASCII credentials with quotes and backslashes: the domain
# the CLI actually admits (Codex B2).
ANTHRO_KEY = 'sk-ant-tr"ick\\y-key-0001'
COMPAT_KEY = "sk-oai-tr'icky\\-key-0002"

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


def credential_needles(secret: str) -> list[bytes]:
    """Every rendering of the secret that must be absent from durable bytes."""
    forms = {
        secret,
        secret.encode("unicode_escape").decode("ascii"),
        json.dumps(secret)[1:-1],
        repr(secret)[1:-1],
    }
    return [form.encode("utf-8") for form in forms]


def assert_run_dir_free_of(run_dir: Path, secrets: tuple[str, ...]) -> None:
    """Scan every durable byte under the run directory for credential forms."""
    scanned = 0
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        blob = path.read_bytes()
        for secret in secrets:
            for needle in credential_needles(secret):
                assert needle not in blob  # never echo the needle in the message
    assert scanned >= 3, "scan looks broken: too few durable files found"


class RecordingAnthropicClient:
    """Scripted Anthropic double that records close() calls."""

    def __init__(self, texts: dict[str, str], usage: Any = None) -> None:
        self._texts = texts
        self._usage = usage
        self.closed = False
        self.messages = SimpleNamespace(create=self._create)

    async def close(self) -> None:
        self.closed = True

    async def _create(self, **kwargs: Any) -> object:
        model = kwargs["model"]
        usage = (
            self._usage
            if self._usage is not None
            else SimpleNamespace(input_tokens=20, output_tokens=10)
        )
        return SimpleNamespace(
            id=f"msg-{model}",
            model=model,
            content=[SimpleNamespace(type="text", text=self._texts[model])],
            usage=usage,
        )


class RecordingHttpClient:
    """Scripted OpenAI-compatible double that records aclose() calls."""

    def __init__(
        self,
        bodies: dict[str, dict[str, object]] | None = None,
        fail_status: int | None = None,
        fail_text: str = "",
    ) -> None:
        self._bodies = bodies if bodies is not None else {}
        self._fail_status = fail_status
        self._fail_text = fail_text
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def post(self, url: str, json: dict[str, object]) -> object:
        if self._fail_status is not None:
            return SimpleNamespace(
                status_code=self._fail_status, text=self._fail_text, json=lambda: {}
            )
        body = self._bodies[str(json["model"])]
        return SimpleNamespace(status_code=200, text="", json=lambda: body)


def build_backends(
    anthro_client: object, compat_client: object
) -> tuple[dict[str, ChatBackend], SecretScrubber]:
    """Mirror the CLI composition: one shared scrubber across every adapter."""
    scrubber = SecretScrubber()
    backends: dict[str, ChatBackend] = {
        "anthro": AnthropicBackend(
            connection_id="anthro",
            api_key=ANTHRO_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=lambda: anthro_client,
            scrubber=scrubber,
        ),
        "compat": OpenAICompatBackend(
            connection_id="compat",
            base_url="https://api.example.test/v1",
            api_key=COMPAT_KEY,
            timeout_seconds=30,
            monotonic=SteppingMonotonic(),
            client_factory=lambda: compat_client,
            scrubber=scrubber,
        ),
    }
    return backends, scrubber


def run_live(
    tmp_path: Path, backends: dict[str, ChatBackend], scrubber: SecretScrubber
) -> tuple[Any, Path]:
    profile = parse_profile(PROFILE_TOML.encode("utf-8"), source="test")
    store = FilesystemRunStore(tmp_path)
    result = run_council_live(
        task=load_task_file(TASK_BASIC),
        profile=profile,
        backends=backends,
        store=store,
        clock=FixedClock("2026-08-15T12:00:00.000Z", 250),
        ids=SequentialIds("hard"),
        scrubber=scrubber,
    )
    return result, tmp_path / ".dagvane" / "runs" / result.run_id


def journal_events(run_dir: Path) -> list[dict[str, Any]]:
    lines = (run_dir / "events.jsonl").read_bytes().splitlines()
    return [json.loads(line) for line in lines]


def compat_success_body(model: str, content: str) -> dict[str, object]:
    return {
        "id": f"cmpl-{model}",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------------
# B2 — failure bodies: escaped and boundary-straddling reflections
# ---------------------------------------------------------------------------


def test_failure_body_reflections_never_reach_durable_state(tmp_path: Path) -> None:
    """A 503 body reflecting the credential raw, JSON-escaped, and straddling
    the snippet boundary must leave no recoverable credential bytes anywhere
    under the run directory."""
    reflected = (
        "x" * 290
        + COMPAT_KEY  # straddles the 300-char snippet boundary
        + " json:"
        + json.dumps(COMPAT_KEY)
        + " anthro:"
        + ANTHRO_KEY
    )
    anthro = RecordingAnthropicClient(
        {"anthro-model": "proposal text", "anthro-judge": DECISION_TEXT}
    )
    compat = RecordingHttpClient(fail_status=503, fail_text=reflected)
    backends, scrubber = build_backends(anthro, compat)
    result, run_dir = run_live(tmp_path, backends, scrubber)

    assert result.status is RunStatus.FAILED
    assert_run_dir_free_of(run_dir, (ANTHRO_KEY, COMPAT_KEY))
    report_bytes = json.dumps(result.report_doc).encode("utf-8")
    for secret in (ANTHRO_KEY, COMPAT_KEY):
        for needle in credential_needles(secret):
            assert needle not in report_bytes


# ---------------------------------------------------------------------------
# B3 — success responses: persistence and cross-provider forwarding
# ---------------------------------------------------------------------------


def test_success_reflections_are_scrubbed_everywhere_including_cross_provider(
    tmp_path: Path,
) -> None:
    """A successful compatible response reflecting both configured credentials
    (content, model name, request id) must be scrubbed before persistence and
    before its content is assembled into the other provider's judge request."""
    reflection = f"leak {COMPAT_KEY} plus {json.dumps(ANTHRO_KEY)} end"
    compat_model = f"compat-model-{COMPAT_KEY}"
    reflecting_body: dict[str, object] = {
        "id": f"cmpl-{COMPAT_KEY}",
        "model": compat_model,
        "choices": [
            {"message": {"role": "assistant", "content": f"proposal: {reflection}"}}
        ],
        "usage": {"prompt_tokens": 15, "completion_tokens": 5},
    }
    bodies = {"compat-model": reflecting_body}
    anthro = RecordingAnthropicClient(
        {"anthro-model": "clean proposal", "anthro-judge": DECISION_TEXT}
    )
    compat = RecordingHttpClient(bodies=bodies)
    backends, scrubber = build_backends(anthro, compat)
    result, run_dir = run_live(tmp_path, backends, scrubber)

    assert result.status is RunStatus.COMPLETED
    assert_run_dir_free_of(run_dir, (ANTHRO_KEY, COMPAT_KEY))

    # The judge's request artifact is later cross-provider context assembly:
    # it contains the compat proposal and must carry no compat credential.
    events = journal_events(run_dir)
    judge_requests = [
        e
        for e in events
        if e["type"] == "artifact.written"
        and e["node_id"] == "judge"
        and e["data"]["role"] == "request"
    ]
    assert judge_requests, "judge request artifact missing"
    for event in judge_requests:
        blob = (run_dir / "artifacts" / event["data"]["sha256"]).read_bytes()
        assert b"[redacted]" in blob  # the reflected content did arrive
        for secret in (ANTHRO_KEY, COMPAT_KEY):
            for needle in credential_needles(secret):
                assert needle not in blob


# ---------------------------------------------------------------------------
# B4 — partial provider usage is committed truthfully (mixed), never replaced
# by a smaller local estimate
# ---------------------------------------------------------------------------


def test_partial_provider_usage_commits_known_actual_and_ceiling(
    tmp_path: Path,
) -> None:
    anthro = RecordingAnthropicClient(
        {"anthro-model": "unused", "anthro-judge": DECISION_TEXT},
        usage=SimpleNamespace(input_tokens=100_000, output_tokens=None),
    )
    compat = RecordingHttpClient(
        bodies={"compat-model": compat_success_body("compat-model", "proposal")}
    )
    backends, scrubber = build_backends(anthro, compat)
    result, run_dir = run_live(tmp_path, backends, scrubber)

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    failed = [e for e in events if e["type"] == "model.failed"]
    assert failed, "expected billed model.failed events"
    for event in failed:
        data = event["data"]
        assert data["reason"] == "usage_missing"
        assert data["usage_source"] == "mixed"
        # The provider-known actual is preserved exactly; the unknown output
        # component is billed at the route ceiling (512), not estimated.
        assert data["billed_input_tokens"] == 100_000
        assert data["billed_output_tokens"] == 512
    committed = result.report_doc["budget"]["committed"]
    assert committed["input_tokens"] >= 100_000


# ---------------------------------------------------------------------------
# M4 — no ghost reservation on pre-dispatch failures
# ---------------------------------------------------------------------------


def test_pre_dispatch_emission_failure_releases_the_reservation() -> None:
    """A failure between successful reservation and backend invocation (id
    allocation, dispatch serialization/emission) must release the in-flight
    reservation instead of permanently occupying the budget."""
    from dagvane.application.council import (
        BudgetLedger,
        CouncilTemplate,
        OneShotModelWorker,
        ResolvedInput,
    )
    from dagvane.domain.models import ArtifactRef, Attempt, Budget

    class MemoryArtifacts:
        def put(self, data: bytes, *, media_type: str, role: str) -> ArtifactRef:
            return ArtifactRef(
                sha256="a" * 64, size=len(data), media_type=media_type, role=role
            )

        def load(self, sha256: str) -> bytes:
            raise AssertionError("not exercised by this test")

    class Ids:
        def __init__(self) -> None:
            self.n = 0

        def new_id(self, kind: str) -> str:
            self.n += 1
            return f"{kind}-{self.n}"

    class ExplodingEmit:
        def __call__(self, payload: Any, **kwargs: Any) -> None:
            if getattr(payload, "TYPE", "") == "model.dispatched":
                raise RuntimeError("oversized frame")

    class UnreachableBackend:
        async def complete(self, request: Any) -> Any:
            raise AssertionError("the backend must never be reached")

    ledger = BudgetLedger(
        Budget(max_calls=1, max_total_tokens=1_000_000, max_cost_microusd=10_000_000)
    )
    worker = OneShotModelWorker(
        backends={"fake": UnreachableBackend()},
        ledger=ledger,
        artifacts=MemoryArtifacts(),
        ids=Ids(),
        emit=ExplodingEmit(),
    )
    role_routes = CouncilTemplate.fake_role_routes()
    route = role_routes["proposer_a"]
    plan, _, _ = CouncilTemplate.build(load_task_file(TASK_BASIC).spec)
    node = plan.nodes[0]

    with pytest.raises(RuntimeError, match="oversized frame"):
        asyncio.run(
            worker.execute(
                node=node,
                route=route,
                attempt=Attempt(node_id=node.node_id, index=1),
                entries=(ResolvedInput(kind="task", label="task", content="do it"),),
            )
        )
    # max_calls=1: admission succeeds again only if the reservation was freed.
    reservation = ledger.reserve(tokens=10, cost_microusd=10)
    assert reservation.calls == 1
    assert ledger.totals().calls == 0  # nothing was committed


# ---------------------------------------------------------------------------
# B3 (round 2) — the shared registry is a process-wide invariant, not a
# composition-root convention
# ---------------------------------------------------------------------------


def test_independently_constructed_adapters_share_the_process_registry() -> None:
    """Adapters built without an explicit scrubber default to one process-wide
    registry: provider A's response is scrubbed of provider B's credential
    even when nobody wired a shared instance."""
    key_a = "sk-proc-reg-anthro-77410"
    key_b = "sk-proc-reg-compat-77411"
    AnthropicBackend(
        connection_id="anthro-x",
        api_key=key_a,
        timeout_seconds=30,
        monotonic=SteppingMonotonic(),
        client_factory=lambda: None,
    )

    class ReflectingClient:
        async def post(self, url: str, json: dict[str, object]) -> object:
            return SimpleNamespace(
                status_code=503,
                text=f"other provider credential: {key_a}",
                json=lambda: {},
            )

    compat = OpenAICompatBackend(
        connection_id="compat-x",
        base_url="https://api.example.test/v1",
        api_key=key_b,
        timeout_seconds=30,
        monotonic=SteppingMonotonic(),
        client_factory=lambda: ReflectingClient(),
    )
    from dagvane.domain.models import BackendDispatchError

    request = PreparedRequest(model="m", max_output_tokens=32, system="s", user_text="x")
    with pytest.raises(BackendDispatchError) as excinfo:
        asyncio.run(compat.complete(request))
    message = str(excinfo.value)
    assert key_a not in message
    assert key_b not in message


# ---------------------------------------------------------------------------
# B4 (round 2) — usage reported on failure responses is committed
# ---------------------------------------------------------------------------


def test_error_response_usage_reaches_the_journal_as_provider_actuals(
    tmp_path: Path,
) -> None:
    reflecting = RecordingHttpClient(
        fail_status=503, fail_text="overloaded but processed"
    )

    async def post_with_usage(url: str, json: dict[str, object]) -> object:
        return SimpleNamespace(
            status_code=503,
            text="overloaded but processed",
            json=lambda: {
                "error": "overloaded",
                "usage": {"prompt_tokens": 100_000, "completion_tokens": 17},
            },
        )

    reflecting.post = post_with_usage  # type: ignore[method-assign]
    anthro = RecordingAnthropicClient(
        {"anthro-model": "proposal", "anthro-judge": DECISION_TEXT}
    )
    backends, scrubber = build_backends(anthro, reflecting)
    result, run_dir = run_live(tmp_path, backends, scrubber)

    assert result.status is RunStatus.FAILED
    events = journal_events(run_dir)
    failed = [e for e in events if e["type"] == "model.failed"]
    assert failed, "expected a billed model.failed for the 503"
    for event in failed:
        data = event["data"]
        assert data["usage_source"] == "provider"
        assert data["billed_input_tokens"] == 100_000
        assert data["billed_output_tokens"] == 17


# ---------------------------------------------------------------------------
# External teardown (round 2) — dependency-waiting nodes close durably
# ---------------------------------------------------------------------------


def test_external_teardown_durably_cancels_dependency_waiting_nodes(
    tmp_path: Path,
) -> None:
    """Cancelling the run externally must leave every node durably terminal —
    including nodes that were still waiting on their dependencies, not just
    the ones inside a dispatch."""
    from dagvane.application.council import CouncilTemplate, RunExecutor
    from dagvane.application.replay import fold_frames
    from dagvane.domain.models import NodeStatus, Run, RunCreated

    task = load_task_file(TASK_BASIC)
    plan, routes, budget = CouncilTemplate.build(task.spec)

    class HangingBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, request: Any) -> Any:
            self.started.set()
            await asyncio.Event().wait()

    backend = HangingBackend()
    store = FilesystemRunStore(tmp_path)
    run_id = "r-teardown-0001"
    store.create_run(run_id)
    run = Run(
        run_id=run_id,
        task=task.spec,
        plan=plan,
        routes=routes,
        budget=budget,
        created_ts="2026-08-15T12:00:00.000Z",
    )
    executor = RunExecutor(
        run=run,
        store=store,
        backends={"fake": backend},
        clock=FixedClock("2026-08-15T12:00:00.000Z", 250),
        ids=SequentialIds("teardown"),
        run_created=RunCreated(
            engine_version="test",
            task_sha256=task.sha256,
            plan_sha256="p" * 64,
            fixture_sha256="f" * 64,
            node_count=len(plan.nodes),
            max_calls=budget.max_calls,
            max_total_tokens=budget.max_total_tokens,
            max_cost_microusd=budget.max_cost_microusd,
        ),
        sink=None,
    )

    async def scenario() -> None:
        exec_task = asyncio.create_task(executor.execute())
        await backend.started.wait()
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task

    asyncio.run(scenario())
    frames = (
        (tmp_path / ".dagvane" / "runs" / run_id / "events.jsonl")
        .read_bytes()
        .splitlines(keepends=True)
    )
    view = fold_frames(iter(frames), require_terminal=False)
    assert set(view.nodes.keys()) == {n.node_id for n in plan.nodes}
    for node in view.nodes.values():
        assert node.status is NodeStatus.FAILED
        assert node.reason == "cancelled"
    # Dependency-waiting nodes (reviewers, judge) have no dispatch; the
    # dispatching proposers were committed at the ceiling before closing.
    assert view.nodes["judge"].calls == 0
    assert view.nodes["proposer-a"].calls == 1
    assert view.nodes["proposer-a"].cost_microusd > 0


# ---------------------------------------------------------------------------
# M1 — adapter clients are closed by the production run path
# ---------------------------------------------------------------------------


def test_adapters_are_closed_after_a_completed_live_run(tmp_path: Path) -> None:
    anthro = RecordingAnthropicClient(
        {"anthro-model": "proposal", "anthro-judge": DECISION_TEXT}
    )
    compat = RecordingHttpClient(
        bodies={"compat-model": compat_success_body("compat-model", "proposal")}
    )
    backends, scrubber = build_backends(anthro, compat)
    result, _ = run_live(tmp_path, backends, scrubber)
    assert result.status is RunStatus.COMPLETED
    assert anthro.closed
    assert compat.closed


def test_adapters_are_closed_after_a_failed_live_run(tmp_path: Path) -> None:
    anthro = RecordingAnthropicClient(
        {"anthro-model": "proposal", "anthro-judge": DECISION_TEXT}
    )
    compat = RecordingHttpClient(fail_status=503, fail_text="upstream unavailable")
    backends, scrubber = build_backends(anthro, compat)
    result, _ = run_live(tmp_path, backends, scrubber)
    assert result.status is RunStatus.FAILED
    assert anthro.closed
    assert compat.closed
