"""Storage ports: durable run state and content-addressed artifacts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Protocol

from dagvane.domain.models import ArtifactRef, EventEnvelope


class ArtifactStore(Protocol):
    def put(self, data: bytes, *, media_type: str, role: str) -> ArtifactRef:
        """Persist ``data`` durably and return its content-addressed reference."""
        ...

    def load(self, sha256: str) -> bytes: ...


class EventJournal(Protocol):
    """Single-writer, gapless, append-only durable event log."""

    @property
    def next_seq(self) -> int: ...

    def append(self, envelope: EventEnvelope) -> bytes:
        """Durably append one envelope; returns the exact journal line bytes."""
        ...

    def close(self) -> None: ...


class RunStore(Protocol):
    def create_run(self, run_id: str) -> None: ...

    def run_exists(self, run_id: str) -> bool: ...

    def open_journal(self, run_id: str) -> EventJournal: ...

    def artifact_store(self, run_id: str) -> ArtifactStore: ...

    def write_manifest(self, run_id: str, doc: Mapping[str, object]) -> None: ...

    def write_decision(self, run_id: str, doc: Mapping[str, object]) -> None: ...

    def write_report(self, run_id: str, doc: Mapping[str, object]) -> None: ...

    def read_manifest(self, run_id: str) -> dict[str, object]: ...

    def iter_frames(self, run_id: str, *, since: int = 0) -> Iterator[bytes]: ...
