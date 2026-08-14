"""End-to-end council run: durable layout, gapless events, isolation, blindness.

Covers acceptance criteria 3 (persisted artifacts), 4 (proposer isolation,
checked on the persisted request snapshots), 5/6 (structural blindness visible
in the run record), and 7 (gapless unique seq).
"""

from __future__ import annotations

import json

from dagvane.domain.models import EventEnvelope
from dagvane.protocol.frames import frame_to_envelope, sha256_hex
from helpers import FIXTURE_HAPPY, CompletedRun

PROPOSAL_A = "Proposal Alpha"
PROPOSAL_B = "Proposal Beta"


def _envelopes(run: CompletedRun) -> list[EventEnvelope]:
    return [frame_to_envelope(line) for line in run.journal_lines()]


def _request_user_text(run: CompletedRun, node_id: str) -> str:
    shas = [
        str(env.data["sha256"])
        for env in _envelopes(run)
        if env.type == "artifact.written"
        and env.node_id == node_id
        and env.data["role"] == "request"
    ]
    assert len(shas) == 1, f"expected one request snapshot for {node_id}, got {shas}"
    doc = json.loads((run.run_dir / "artifacts" / shas[0]).read_bytes())
    user_text = doc["user_text"]
    assert isinstance(user_text, str)
    return user_text


def test_run_layout_is_complete(happy_run: CompletedRun) -> None:
    assert (happy_run.run_dir / "manifest.json").is_file()
    assert (happy_run.run_dir / "events.jsonl").is_file()
    assert (happy_run.run_dir / "decision.json").is_file()
    assert (happy_run.run_dir / "report.json").is_file()
    artifacts = list((happy_run.run_dir / "artifacts").iterdir())
    assert len(artifacts) == 10  # 5 request snapshots + 5 model outputs
    for artifact in artifacts:
        assert artifact.name == sha256_hex(artifact.read_bytes())


def test_events_are_gapless_unique_and_bracketed(happy_run: CompletedRun) -> None:
    envelopes = _envelopes(happy_run)
    seqs = [env.seq for env in envelopes]
    assert seqs == list(range(1, len(seqs) + 1))
    assert envelopes[0].type == "run.created"
    assert envelopes[-1].type == "run.finished"
    assert all(env.run_id == happy_run.run_id for env in envelopes)
    assert len({env.event_id for env in envelopes}) == len(envelopes)


def test_report_records_a_fully_completed_judged_run(happy_run: CompletedRun) -> None:
    report = json.loads((happy_run.run_dir / "report.json").read_bytes())
    assert report["status"] == "completed"
    assert report["reason"] is None
    nodes = report["nodes"]
    assert set(nodes) == {"proposer-a", "proposer-b", "review-by-a", "review-by-b", "judge"}
    assert all(node["status"] == "completed" for node in nodes.values())
    assert report["decision"]["winner"] == "candidate-1"
    assert report["budget"]["committed"]["calls"] == 5
    decision = json.loads((happy_run.run_dir / "decision.json").read_bytes())
    assert decision["winner"] == "candidate-1"


def test_proposer_inputs_contain_no_sibling_output(happy_run: CompletedRun) -> None:
    text_a = _request_user_text(happy_run, "proposer-a")
    text_b = _request_user_text(happy_run, "proposer-b")
    for text in (text_a, text_b):
        assert "Pick a storage layout" in text  # the task is there
        assert "candidate-" not in text  # and nothing else is
        assert "review" not in text
    assert PROPOSAL_B not in text_a
    assert PROPOSAL_A not in text_b


def test_reviews_are_blind_and_never_self(happy_run: CompletedRun) -> None:
    review_a = _request_user_text(happy_run, "review-by-a")  # identity A reviews candidate-2
    assert "candidate-2" in review_a
    assert PROPOSAL_B in review_a
    assert PROPOSAL_A not in review_a  # self-exclusion: own identity's proposal absent
    review_b = _request_user_text(happy_run, "review-by-b")
    assert "candidate-1" in review_b
    assert PROPOSAL_A in review_b
    assert PROPOSAL_B not in review_b
    for text in (review_a, review_b):
        assert "proposer-a" not in text  # sealed: producer identities never rendered
        assert "proposer-b" not in text


def test_judge_sees_candidates_and_reviews_by_label_only(happy_run: CompletedRun) -> None:
    judge = _request_user_text(happy_run, "judge")
    for label in ("candidate-1", "candidate-2", "review-of-candidate-1", "review-of-candidate-2"):
        assert label in judge
    assert PROPOSAL_A in judge and PROPOSAL_B in judge
    assert "proposer-a" not in judge and "proposer-b" not in judge


def test_manifest_seals_the_mapping_and_embeds_no_paths(happy_run: CompletedRun) -> None:
    manifest_bytes = (happy_run.run_dir / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["plan"]["anonymization"] == {
        "candidate-1": "proposer-a",
        "candidate-2": "proposer-b",
    }
    assert manifest["determinism"] == {
        "run_id_pinned": True,
        "clock": {"start": "2026-01-01T00:00:00.000Z", "step_ms": 10},
        "ids_seed": "g0",
    }
    assert manifest["fixture_sha256"] == sha256_hex(FIXTURE_HAPPY.read_bytes())
    for persisted in ("manifest.json", "report.json", "events.jsonl", "decision.json"):
        data = (happy_run.run_dir / persisted).read_bytes()
        assert str(happy_run.cwd).encode() not in data
        assert str(FIXTURE_HAPPY).encode() not in data
