"""Canonical serialization and NDJSON frame codec.

Every byte the engine persists or streams flows through this module: journal
lines, stdout NDJSON frames, JSON documents, and content hashes all use the
same canonical serializer, which is what makes journal and stdout byte-identical
and repeated deterministic runs byte-reproducible.

Canonical form: JSON with sorted keys, no whitespace, UTF-8, ``ensure_ascii``
off, one trailing newline. Never serialize set-derived ordering — ``sorted()``
first — and never embed filesystem paths in persisted documents.
"""

from __future__ import annotations

import hashlib
import json

from dagvane.domain.models import (
    ENVELOPE_VERSION,
    EventEnvelope,
    ProtocolError,
    decode_payload,
)

MAX_FRAME_BYTES = 1024 * 1024


def canonical_json_bytes(obj: object) -> bytes:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_ENVELOPE_KEYS = frozenset(
    {
        "v",
        "event_id",
        "run_id",
        "seq",
        "ts",
        "node_id",
        "attempt",
        "operation_id",
        "call_id",
        "type",
        "data",
    }
)


def envelope_to_frame(envelope: EventEnvelope) -> bytes:
    frame = canonical_json_bytes(
        {
            "v": envelope.v,
            "event_id": envelope.event_id,
            "run_id": envelope.run_id,
            "seq": envelope.seq,
            "ts": envelope.ts,
            "node_id": envelope.node_id,
            "attempt": envelope.attempt,
            "operation_id": envelope.operation_id,
            "call_id": envelope.call_id,
            "type": envelope.type,
            "data": dict(envelope.data),
        }
    )
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame exceeds {MAX_FRAME_BYTES} bytes ({len(frame)})")
    return frame


def _req_str(obj: dict[str, object], key: str) -> str:
    value = obj[key]
    if not isinstance(value, str):
        raise ProtocolError(f"frame field {key!r} must be a string")
    return value


def _opt_str(obj: dict[str, object], key: str) -> str | None:
    value = obj[key]
    if value is not None and not isinstance(value, str):
        raise ProtocolError(f"frame field {key!r} must be a string or null")
    return value


def _req_int(obj: dict[str, object], key: str) -> int:
    value = obj[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"frame field {key!r} must be an integer")
    return value


def _opt_int(obj: dict[str, object], key: str) -> int | None:
    value = obj[key]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"frame field {key!r} must be an integer or null")
    return value


def frame_to_envelope(line: bytes) -> EventEnvelope:
    """Strictly decode one NDJSON frame, including its payload registry check."""
    if len(line) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame exceeds {MAX_FRAME_BYTES} bytes ({len(line)})")
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    if set(obj.keys()) != _ENVELOPE_KEYS:
        raise ProtocolError(f"frame keys {sorted(obj.keys())!r} do not match the envelope")
    version = _req_int(obj, "v")
    if version != ENVELOPE_VERSION:
        raise ProtocolError(f"unsupported envelope version {version}")
    seq = _req_int(obj, "seq")
    if seq < 1:
        raise ProtocolError(f"frame seq must be >= 1, got {seq}")
    data = obj["data"]
    if not isinstance(data, dict) or not all(isinstance(k, str) for k in data):
        raise ProtocolError("frame data must be a JSON object")
    envelope = EventEnvelope(
        v=version,
        event_id=_req_str(obj, "event_id"),
        run_id=_req_str(obj, "run_id"),
        seq=seq,
        ts=_req_str(obj, "ts"),
        node_id=_opt_str(obj, "node_id"),
        attempt=_opt_int(obj, "attempt"),
        operation_id=_opt_str(obj, "operation_id"),
        call_id=_opt_str(obj, "call_id"),
        type=_req_str(obj, "type"),
        data=data,
    )
    decode_payload(envelope.type, envelope.data)
    return envelope
