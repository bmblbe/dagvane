"""Canonical serializer and NDJSON frame codec tests."""

from __future__ import annotations

import pytest

from dagvane.domain.models import (
    EventEnvelope,
    NodeFailed,
    ProtocolError,
    payload_to_data,
)
from dagvane.protocol.frames import (
    canonical_json_bytes,
    envelope_to_frame,
    frame_to_envelope,
    sha256_hex,
)


def _envelope(**overrides: object) -> EventEnvelope:
    base: dict[str, object] = {
        "v": 1,
        "event_id": "event-1",
        "run_id": "r-1",
        "seq": 1,
        "ts": "2026-01-01T00:00:00.000Z",
        "node_id": "proposer-a",
        "attempt": 1,
        "operation_id": None,
        "call_id": None,
        "type": NodeFailed.TYPE,
        "data": payload_to_data(NodeFailed(reason="backend_error", message="boom")),
    }
    base.update(overrides)
    return EventEnvelope(**base)  # type: ignore[arg-type]


def test_canonical_json_is_sorted_compact_utf8() -> None:
    data = canonical_json_bytes({"b": 1, "a": {"z": None, "y": "ключ"}})
    assert data == '{"a":{"y":"ключ","z":null},"b":1}\n'.encode()


def test_canonical_json_single_trailing_newline() -> None:
    assert canonical_json_bytes([]).endswith(b"]\n")
    assert not canonical_json_bytes([]).endswith(b"\n\n")


def test_sha256_hex() -> None:
    assert sha256_hex(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_envelope_frame_round_trip() -> None:
    envelope = _envelope()
    frame = envelope_to_frame(envelope)
    assert frame.endswith(b"\n")
    decoded = frame_to_envelope(frame)
    assert decoded == EventEnvelope(
        v=envelope.v,
        event_id=envelope.event_id,
        run_id=envelope.run_id,
        seq=envelope.seq,
        ts=envelope.ts,
        node_id=envelope.node_id,
        attempt=envelope.attempt,
        operation_id=envelope.operation_id,
        call_id=envelope.call_id,
        type=envelope.type,
        data=dict(envelope.data),
    )


def test_oversized_frame_rejected() -> None:
    huge = _envelope(
        data=payload_to_data(NodeFailed(reason="x", message="m" * (1024 * 1024)))
    )
    with pytest.raises(ProtocolError, match="exceeds"):
        envelope_to_frame(huge)


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b"[1,2]\n",
        b'{"v":1}\n',
    ],
)
def test_malformed_frames_rejected(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        frame_to_envelope(line)


def test_wrong_envelope_values_rejected() -> None:
    with pytest.raises(ProtocolError, match="version"):
        frame_to_envelope(envelope_to_frame(_envelope()).replace(b'"v":1', b'"v":2'))
    with pytest.raises(ProtocolError, match="seq"):
        frame_to_envelope(envelope_to_frame(_envelope()).replace(b'"seq":1', b'"seq":0'))


def test_frame_payload_checked_against_registry() -> None:
    frame = envelope_to_frame(_envelope())
    bad = frame.replace(b'"type":"node.failed"', b'"type":"node.exploded"')
    with pytest.raises(ProtocolError, match="unknown event type"):
        frame_to_envelope(bad)
