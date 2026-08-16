"""Vendor-neutral managed-process lifecycle contract, API V1.

This module defines values and protocols only.  It does not spawn processes,
touch the filesystem, persist ownership records, or choose Goal state.  A
future adapter owns those effects and must use the frozen launch timeout; it
cannot replace the timeout at ``wait()`` time.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from io import IOBase, TextIOBase
from pathlib import Path
from types import MappingProxyType
from typing import IO, Protocol

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import DagvaneError, SpecError

PROCESS_PORT_API_VERSION = 1

_MAX_PURPOSE_CHARS = 64


class ProcessOwnershipError(SpecError):
    """Managed-process ownership is malformed, mismatched, or unverifiable."""


class ProcessCleanupIncomplete(DagvaneError):
    """The adapter could not prove that the owned process tree is quiescent."""


class ProcessPlatformUnsupported(SpecError):
    """The platform cannot provide the required managed-process guarantees."""


def _validated_identity(value: object, *, field_name: str) -> str:
    try:
        return validate_filesystem_id(value, ctx=field_name)
    except SpecError:
        # Ownership errors must identify the invalid field without echoing a
        # potentially hostile or sensitive durable value.
        raise ProcessOwnershipError(f"managed process owner {field_name} is invalid") from None


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validated_purpose(value: object) -> str:
    if not isinstance(value, str):
        raise ProcessOwnershipError("managed process owner purpose must be a string")
    if not value or value != value.strip() or len(value) > _MAX_PURPOSE_CHARS:
        raise ProcessOwnershipError("managed process owner purpose is invalid")
    if "\x00" in value or _contains_control(value):
        raise ProcessOwnershipError("managed process owner purpose is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ProcessScope:
    """The durable Goal run allowed to own at most one managed process."""

    goal_id: str
    run_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal_id", _validated_identity(self.goal_id, field_name="goal_id"))
        object.__setattr__(self, "run_id", _validated_identity(self.run_id, field_name="run_id"))


@dataclass(frozen=True, slots=True)
class ProcessOwner:
    """Exact logical operation to which one process lifecycle belongs.

    ``operation_id`` is the idempotency key within ``scope``.  ``purpose`` is
    descriptive provenance and is deliberately excluded from representations.
    """

    scope: ProcessScope
    operation_id: str
    purpose: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ProcessScope):
            raise ProcessOwnershipError("managed process owner scope must be a ProcessScope")
        object.__setattr__(
            self,
            "operation_id",
            _validated_identity(self.operation_id, field_name="operation_id"),
        )
        object.__setattr__(self, "purpose", _validated_purpose(self.purpose))


class ProcessPhase(StrEnum):
    """Closed durable lifecycle phases; Goal terminal states are separate.

    The normal path is ``PREPARED -> ACTIVE -> STOPPING -> QUIESCENT``.  A
    startup-failure or recovery path is ``PREPARED -> STOPPING -> QUIESCENT``.
    No transition moves backwards or leaves ``QUIESCENT``.
    """

    PREPARED = "prepared"
    ACTIVE = "active"
    STOPPING = "stopping"
    QUIESCENT = "quiescent"


class StopCause(StrEnum):
    """Process-level cleanup causes, deliberately excluding Goal outcomes."""

    TIMEOUT = "timeout"
    CALLER_ERROR = "caller_error"
    EXTERNAL_REQUEST = "external_request"
    RECOVERY = "recovery"
    NORMAL_COMPLETION = "normal_completion"


@dataclass(frozen=True, slots=True)
class ProcessLaunch:
    """One immutable launch policy handed to a managed-process adapter.

    ``timeout_seconds`` is part of the launch's frozen provenance and is
    validated before an adapter may perform any effect.  ``env`` is copied
    into an immutable proxy, while both ``env`` and ``stdin`` are excluded
    from representations so credentials and stream contents are not exposed.
    """

    owner: ProcessOwner
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    timeout_seconds: float
    env: Mapping[str, str] = field(repr=False)
    stdin: IO[bytes] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ProcessOwner):
            raise ProcessOwnershipError("managed process launch owner must be a ProcessOwner")

        if not isinstance(self.argv, (list, tuple)):
            raise SpecError("managed process argv must be a non-empty string sequence")
        frozen_argv = tuple(self.argv)
        if not frozen_argv:
            raise SpecError("managed process argv must not be empty")
        for argument in frozen_argv:
            if not isinstance(argument, str):
                raise SpecError("managed process argv entries must be strings")
            if not argument or "\x00" in argument or _contains_control(argument):
                raise SpecError("managed process argv contains an invalid entry")
        object.__setattr__(self, "argv", frozen_argv)

        if not isinstance(self.cwd, Path):
            raise SpecError("managed process cwd must be a Path")

        timeout = self.timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise SpecError("managed process timeout_seconds must be a finite number > 0")
        try:
            frozen_timeout = float(timeout)
        except (OverflowError, ValueError):
            raise SpecError("managed process timeout_seconds must be a finite number > 0") from None
        if not math.isfinite(frozen_timeout) or frozen_timeout <= 0:
            raise SpecError("managed process timeout_seconds must be a finite number > 0")
        object.__setattr__(self, "timeout_seconds", frozen_timeout)

        if not isinstance(self.env, Mapping):
            raise SpecError("managed process env must be a string mapping")
        frozen_env = dict(self.env)
        for name, value in frozen_env.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise SpecError("managed process env must contain string names and values")
            if (
                not name
                or "=" in name
                or "\x00" in name
                or _contains_control(name)
                or "\x00" in value
            ):
                raise SpecError("managed process env contains an invalid entry")
        object.__setattr__(self, "env", MappingProxyType(frozen_env))

        if self.stdin is not None and (
            not isinstance(self.stdin, IOBase) or isinstance(self.stdin, TextIOBase)
        ):
            raise SpecError("managed process stdin must be a binary IO stream or None")


class ProcessEnd(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ProcessExit:
    """A terminal process result; returning it implies proven quiescence.

    An integer ``exit_code`` is a trustworthy direct-child status.  ``None``
    means that no trustworthy direct-child status is available; it never means
    zero or successful completion.
    """

    end: ProcessEnd
    exit_code: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.end, ProcessEnd):
            raise SpecError("managed process end must be a ProcessEnd")
        if self.end is ProcessEnd.EXITED and type(self.exit_code) is not int:
            raise SpecError("managed process EXITED result requires an integer exit_code")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise SpecError("managed process exit_code must be an integer or None")


class StopStatus(StrEnum):
    """Result of reconciling durable ownership to proven quiescence.

    ``ABSENT`` means no ownership record exists for the scope.
    ``ALREADY_QUIESCENT`` means an ownership record exists and already proves
    quiescence.  ``STOPPED`` means this call reconciled a non-quiescent
    ownership record to proven ``QUIESCENT``.  For ``STOPPED``, the separate
    ``had_live_processes`` evidence may be either ``True`` or ``False``.
    """

    ABSENT = "absent"
    ALREADY_QUIESCENT = "already_quiescent"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class StopResult:
    """Reconciliation outcome plus independent live-process observation.

    ``ABSENT`` and ``ALREADY_QUIESCENT`` require ``had_live_processes=False``.
    ``STOPPED`` permits either boolean because reconciliation may prove a stale
    non-quiescent record had no remaining live process-tree member.
    """

    status: StopStatus
    had_live_processes: bool

    def __post_init__(self) -> None:
        if not isinstance(self.status, StopStatus):
            raise SpecError("managed process stop status must be a StopStatus")
        if type(self.had_live_processes) is not bool:
            raise SpecError("managed process had_live_processes must be a boolean")
        if self.status is not StopStatus.STOPPED and self.had_live_processes:
            raise SpecError("managed process stop result is inconsistent with its status")


class ManagedProcess(Protocol):
    """One started process whose adapter still owns its complete lifecycle.

    ``wait()`` and ``stop()`` are terminal and idempotent: after a terminal
    ``ProcessExit`` is recorded, repeated calls return that result without a
    new process effect or lifecycle transition.
    """

    @property
    def stdout(self) -> IO[bytes]: ...

    def wait(self) -> ProcessExit:
        """Return only after quiescence without starting or resetting timeout."""
        ...

    def stop(self, *, cause: StopCause) -> ProcessExit:
        """Perform bounded cleanup and return only after quiescence."""
        ...


class ManagedProcessPort(Protocol):
    """Launch and cross-process reconciliation boundary for API V1.

    Calling ``start()`` returns a context manager and has no effect before its
    ``__enter__``.  On entry, the adapter atomically admits at most one live
    ``PREPARED``, ``ACTIVE``, or ``STOPPING`` lifecycle per ``ProcessScope``.
    It durably records matching ownership in ``PREPARED`` before spawning a
    child or allowing any child effect.  The normal path is
    ``PREPARED -> ACTIVE -> STOPPING -> QUIESCENT``; startup failure or recovery
    uses ``PREPARED -> STOPPING -> QUIESCENT``.  No transition moves backwards
    or leaves ``QUIESCENT``.

    After the durable ``PREPARED`` ownership record exists, any startup
    (``__enter__``) or recovery failure initiates bounded cleanup before
    propagation, whether or not a child was spawned.  It may release that record
    only after proving quiescence.  If quiescence remains unproven, it retains
    the record for recovery and raises ``ProcessCleanupIncomplete``.

    ``ProcessOwner.operation_id`` is the idempotency key within a scope.  A
    matching retry reconciles the same lifecycle; a collision with another
    operation or mismatched owner raises ``ProcessOwnershipError`` rather than
    starting a second process.

    Immediately before the first persistence or process effect, ``__enter__``
    starts one monotonic deadline from the frozen ``timeout_seconds``.  The
    adapter enforces that deadline independently of reads from ``stdout`` or
    calls to ``wait()``; ``wait()`` never starts or resets it.  When the
    deadline expires, the adapter initiates bounded stop and returns no terminal
    result until the complete owned process tree is proven quiescent.

    ``wait()``, ``stop()``, and ``ensure_quiescent()`` are idempotent terminal
    operations.  Context ``__exit__`` performs bounded cleanup through proven
    quiescence and does not suppress an exception from the context body.  If
    quiescence cannot be proved, it raises ``ProcessCleanupIncomplete``.
    """

    def start(self, launch: ProcessLaunch) -> AbstractContextManager[ManagedProcess]:
        """Return an effect-free context manager whose work begins at entry."""
        ...

    def ensure_quiescent(self, scope: ProcessScope, *, cause: StopCause) -> StopResult:
        """Idempotently reconcile ``scope`` to durable ``QUIESCENT``."""
        ...
