"""Pure contract tests for the vendor-neutral managed-process port."""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from io import BytesIO, StringIO
from pathlib import Path

import pytest

from dagvane.domain.models import DagvaneError, SpecError
from dagvane.ports.process import (
    PROCESS_PORT_API_VERSION,
    ManagedProcess,
    ManagedProcessPort,
    ProcessCleanupIncomplete,
    ProcessEnd,
    ProcessExit,
    ProcessLaunch,
    ProcessOwner,
    ProcessOwnershipError,
    ProcessPhase,
    ProcessPlatformUnsupported,
    ProcessScope,
    StopCause,
    StopResult,
    StopStatus,
)


def _scope() -> ProcessScope:
    return ProcessScope(goal_id="goal-a", run_id="goalrun-000001")


def _owner() -> ProcessOwner:
    return ProcessOwner(scope=_scope(), operation_id="agent-call-1", purpose="implement")


def _launch(**overrides: object) -> ProcessLaunch:
    values: dict[str, object] = {
        "owner": _owner(),
        "argv": ("agent", "--safe"),
        "cwd": Path("/worktree"),
        "timeout_seconds": 30.0,
        "env": {"LANG": "C.UTF-8"},
        "stdin": None,
    }
    values.update(overrides)
    return ProcessLaunch(**values)  # type: ignore[arg-type]


def test_closed_enums_have_exact_wire_values() -> None:
    assert tuple(ProcessPhase) == (
        ProcessPhase.PREPARED,
        ProcessPhase.ACTIVE,
        ProcessPhase.STOPPING,
        ProcessPhase.QUIESCENT,
    )
    assert [item.value for item in StopCause] == [
        "timeout",
        "caller_error",
        "external_request",
        "recovery",
        "normal_completion",
    ]
    assert [item.value for item in ProcessEnd] == ["exited", "timed_out", "stopped"]
    assert [item.value for item in StopStatus] == [
        "absent",
        "already_quiescent",
        "stopped",
    ]


def test_api_v1_documents_adapter_effect_and_lifecycle_obligations() -> None:
    assert PROCESS_PORT_API_VERSION == 1
    assert ManagedProcessPort.__doc__ is not None
    contract = " ".join(ManagedProcessPort.__doc__.lower().split())

    for obligation in (
        "no effect before its ``__enter__``",
        "at most one live",
        "``prepared``, ``active``, or ``stopping`` lifecycle per ``processscope``",
        "durably records matching ownership in ``prepared`` before spawning a child",
        "``processowner.operation_id`` is the idempotency key",
        "raises ``processownershiperror``",
        "``prepared -> active -> stopping -> quiescent``",
        "monotonic deadline",
        "independently of reads from ``stdout`` or calls to ``wait()``",
        "``wait()`` never starts or resets it",
        "idempotent terminal operations",
        "does not suppress an exception",
        "raises ``processcleanupincomplete``",
    ):
        assert obligation in contract


def test_api_v1_documents_both_forward_lifecycle_paths() -> None:
    assert ProcessPhase.__doc__ is not None
    assert ManagedProcessPort.__doc__ is not None
    phase_contract = " ".join(ProcessPhase.__doc__.lower().split())
    port_contract = " ".join(ManagedProcessPort.__doc__.lower().split())

    for contract in (phase_contract, port_contract):
        assert "``prepared -> active -> stopping -> quiescent``" in contract
        assert "``prepared -> stopping -> quiescent``" in contract
        assert "no transition moves backwards" in contract
        assert "leaves ``quiescent``" in contract


def test_api_v1_documents_all_post_prepare_failures_require_cleanup() -> None:
    assert ManagedProcessPort.__doc__ is not None
    contract = " ".join(ManagedProcessPort.__doc__.lower().split())

    assert "after the durable ``prepared`` ownership record exists" in contract
    assert "any startup (``__enter__``) or recovery failure initiates bounded cleanup" in contract
    assert "whether or not a child was spawned" in contract
    assert "release that record only after proving quiescence" in contract
    assert "retains the record for recovery" in contract
    assert "raises ``processcleanupincomplete``" in contract


def test_api_v1_documents_timeout_stop_before_terminal_result() -> None:
    assert ManagedProcessPort.__doc__ is not None
    contract = " ".join(ManagedProcessPort.__doc__.lower().split())

    assert "when the deadline expires" in contract
    assert "initiates bounded stop" in contract
    assert (
        "returns no terminal result until the complete owned process tree is proven quiescent"
        in contract
    )


def test_terminal_result_docstrings_define_status_evidence() -> None:
    assert ProcessExit.__doc__ is not None
    assert StopStatus.__doc__ is not None
    exit_contract = " ".join(ProcessExit.__doc__.lower().split())
    stop_contract = " ".join(StopStatus.__doc__.lower().split())

    assert "``none`` means that no trustworthy direct-child status is available" in exit_contract
    assert "it never means zero or successful completion" in exit_contract
    assert "``absent`` means no ownership record exists" in stop_contract
    assert "``already_quiescent`` means an ownership record exists" in stop_contract
    assert "already proves quiescence" in stop_contract
    assert "``stopped`` means this call reconciled a non-quiescent" in stop_contract
    assert "ownership record to proven ``quiescent``" in stop_contract
    assert "``had_live_processes`` evidence may be either ``true`` or ``false``" in stop_contract


def test_owner_values_are_frozen_slotted_and_preserve_valid_identity() -> None:
    scope = _scope()
    owner = _owner()
    assert scope.goal_id == "goal-a"
    assert owner.scope == scope
    assert owner.operation_id == "agent-call-1"
    assert owner.purpose == "implement"
    assert not hasattr(scope, "__dict__")
    assert not hasattr(owner, "__dict__")
    with pytest.raises(FrozenInstanceError):
        scope.goal_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        owner.purpose = "review"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("goal_id", ""),
        ("goal_id", "goal\nother"),
        ("goal_id", "goal\x00other"),
        ("goal_id", "../goal"),
        ("goal_id", 7),
        ("run_id", ""),
        ("run_id", "run\tother"),
        ("run_id", None),
    ],
)
def test_scope_rejects_invalid_identity_without_echoing_value(
    field_name: str, value: object
) -> None:
    values: dict[str, object] = {"goal_id": "goal-a", "run_id": "run-a"}
    values[field_name] = value
    with pytest.raises(ProcessOwnershipError) as raised:
        ProcessScope(**values)  # type: ignore[arg-type]
    assert repr(value) not in str(raised.value)


@pytest.mark.parametrize("operation_id", ["", "operation\n2", "operation\x002", "../operation", 1])
def test_owner_rejects_invalid_operation_identity_without_echoing_value(
    operation_id: object,
) -> None:
    with pytest.raises(ProcessOwnershipError) as raised:
        ProcessOwner(
            scope=_scope(),
            operation_id=operation_id,  # type: ignore[arg-type]
            purpose="implement",
        )
    assert repr(operation_id) not in str(raised.value)


@pytest.mark.parametrize(
    "purpose",
    [
        "",
        "   ",
        " review",
        "review ",
        "review\nunsafe",
        "review\x00unsafe",
        "x" * 65,
        3,
    ],
)
def test_owner_rejects_invalid_purpose_without_echoing_value(purpose: object) -> None:
    with pytest.raises(ProcessOwnershipError) as raised:
        ProcessOwner(
            scope=_scope(),
            operation_id="agent-call-1",
            purpose=purpose,  # type: ignore[arg-type]
        )
    assert repr(purpose) not in str(raised.value)


def test_owner_requires_typed_scope() -> None:
    with pytest.raises(ProcessOwnershipError, match="scope"):
        ProcessOwner(
            scope={"goal_id": "goal-a", "run_id": "run-a"},  # type: ignore[arg-type]
            operation_id="agent-call-1",
            purpose="implement",
        )


def test_launch_defensively_freezes_argv_env_and_timeout() -> None:
    argv = ["agent", "--mode", "safe"]
    env = {"TOKEN": "secret-value", "LANG": "C"}
    launch = _launch(argv=argv, env=env, timeout_seconds=3)

    argv.append("--mutated")
    env["TOKEN"] = "changed"
    env["NEW"] = "later"

    assert launch.argv == ("agent", "--mode", "safe")
    assert dict(launch.env) == {"TOKEN": "secret-value", "LANG": "C"}
    assert launch.timeout_seconds == 3.0
    with pytest.raises(TypeError):
        launch.env["NEW"] = "forbidden"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        launch.timeout_seconds = 4.0  # type: ignore[misc]
    assert not hasattr(launch, "__dict__")


def test_launch_repr_hides_argv_cwd_environment_stdin_and_purpose() -> None:
    stdin = BytesIO(b"stdin-secret")
    owner = ProcessOwner(
        scope=_scope(),
        operation_id="agent-call-1",
        purpose="purpose-secret",
    )
    launch = _launch(
        owner=owner,
        argv=("argv-secret", "--flag"),
        cwd=Path("/cwd-secret"),
        env={"SECRET_NAME": "secret-value"},
        stdin=stdin,
    )
    rendered = repr(launch)
    owner_rendered = repr(owner)
    for secret in (
        "argv-secret",
        "cwd-secret",
        "SECRET_NAME",
        "secret-value",
        "stdin-secret",
        "BytesIO",
        "purpose-secret",
    ):
        assert secret not in rendered
    assert "purpose-secret" not in owner_rendered
    for hidden_field in ("argv=", "cwd=", "env=", "stdin=", "purpose="):
        assert hidden_field not in rendered


@pytest.mark.parametrize(
    "argv",
    [
        (),
        "agent",
        b"agent",
        ("",),
        ("agent", "bad\nargument"),
        ("agent", "bad\x00argument"),
        ("agent", 4),
        {"agent": "value"},
        {"agent"},
        iter(["agent"]),
        (argument for argument in ["agent"]),
        None,
    ],
)
def test_launch_rejects_invalid_argv_without_echoing_entries(argv: object) -> None:
    with pytest.raises(SpecError) as raised:
        _launch(argv=argv)
    assert "bad\nargument" not in str(raised.value)
    assert "bad\x00argument" not in str(raised.value)


@pytest.mark.parametrize(
    "timeout",
    [0, -1, math.inf, -math.inf, math.nan, True, "30", None],
)
def test_launch_rejects_nonpositive_nonfinite_or_non_numeric_timeout(
    timeout: object,
) -> None:
    with pytest.raises(SpecError, match="finite number > 0"):
        _launch(timeout_seconds=timeout)


def test_launch_normalizes_huge_timeout_overflow_to_spec_error() -> None:
    with pytest.raises(SpecError, match="finite number > 0"):
        _launch(timeout_seconds=10**10000)


def test_launch_rejects_invalid_owner_cwd_and_stdin_types() -> None:
    with pytest.raises(ProcessOwnershipError, match="owner"):
        _launch(owner=_scope())
    with pytest.raises(SpecError, match="cwd"):
        _launch(cwd="/worktree")
    with pytest.raises(SpecError, match="stdin"):
        _launch(stdin=object())
    with pytest.raises(SpecError, match="stdin"):
        _launch(stdin=StringIO("text is not binary"))


@pytest.mark.parametrize(
    "env",
    [
        [],
        {1: "value"},
        {"NAME": 1},
        {"": "value"},
        {"BAD=NAME": "value"},
        {"BAD\nNAME": "value"},
        {"BAD\x00NAME": "value"},
        {"NAME": "bad\x00value"},
    ],
)
def test_launch_rejects_invalid_environment_without_echoing_values(env: object) -> None:
    with pytest.raises(SpecError) as raised:
        _launch(env=env)
    assert "bad\x00value" not in str(raised.value)


def test_terminal_values_are_frozen_slotted_and_strictly_typed() -> None:
    exited = ProcessExit(ProcessEnd.EXITED, 0)
    stopped = StopResult(StopStatus.STOPPED, True)
    assert exited.exit_code == 0
    assert stopped.had_live_processes is True
    assert not hasattr(exited, "__dict__")
    assert not hasattr(stopped, "__dict__")
    with pytest.raises(FrozenInstanceError):
        exited.exit_code = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stopped.had_live_processes = False  # type: ignore[misc]


@pytest.mark.parametrize("end", ["exited", None, 1])
def test_process_exit_requires_enum(end: object) -> None:
    with pytest.raises(SpecError, match="ProcessEnd"):
        ProcessExit(end=end, exit_code=0)  # type: ignore[arg-type]


@pytest.mark.parametrize("exit_code", [None, True, 1.5, "0", object()])
def test_process_exit_rejects_non_integer_exit_code(exit_code: object) -> None:
    with pytest.raises(SpecError, match="exit_code"):
        ProcessExit(ProcessEnd.EXITED, exit_code)  # type: ignore[arg-type]


def test_non_exited_terminal_results_may_have_no_exit_code() -> None:
    assert ProcessExit(ProcessEnd.TIMED_OUT, None).exit_code is None
    assert ProcessExit(ProcessEnd.STOPPED, None).exit_code is None


def test_stop_result_requires_enum_and_real_boolean() -> None:
    with pytest.raises(SpecError, match="StopStatus"):
        StopResult(status="stopped", had_live_processes=True)  # type: ignore[arg-type]
    with pytest.raises(SpecError, match="boolean"):
        StopResult(status=StopStatus.STOPPED, had_live_processes=1)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "had_live_processes"),
    [
        (StopStatus.ABSENT, False),
        (StopStatus.ALREADY_QUIESCENT, False),
        (StopStatus.STOPPED, False),
        (StopStatus.STOPPED, True),
    ],
)
def test_stop_result_accepts_only_consistent_status_and_liveness(
    status: StopStatus,
    had_live_processes: bool,
) -> None:
    assert StopResult(status, had_live_processes) == StopResult(status, had_live_processes)


@pytest.mark.parametrize(
    ("status", "had_live_processes"),
    [
        (StopStatus.ABSENT, True),
        (StopStatus.ALREADY_QUIESCENT, True),
    ],
)
def test_stop_result_rejects_inconsistent_status_and_liveness(
    status: StopStatus,
    had_live_processes: bool,
) -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        StopResult(status, had_live_processes)


def test_explicit_process_errors_share_the_domain_error_taxonomy() -> None:
    assert issubclass(ProcessOwnershipError, DagvaneError)
    assert issubclass(ProcessCleanupIncomplete, DagvaneError)
    assert issubclass(ProcessPlatformUnsupported, DagvaneError)


class _FakeManagedProcess:
    def __init__(self) -> None:
        self._stdout = BytesIO(b"bounded fake output")

    @property
    def stdout(self) -> BytesIO:
        return self._stdout

    def wait(self) -> ProcessExit:
        return ProcessExit(ProcessEnd.EXITED, 0)

    def stop(self, *, cause: StopCause) -> ProcessExit:
        del cause
        return ProcessExit(ProcessEnd.STOPPED, None)


class _FakeManagedProcessPort:
    @contextmanager
    def start(self, launch: ProcessLaunch) -> Iterator[ManagedProcess]:
        del launch
        yield _FakeManagedProcess()

    def ensure_quiescent(self, scope: ProcessScope, *, cause: StopCause) -> StopResult:
        del scope, cause
        return StopResult(StopStatus.ABSENT, False)


def _accept_managed_process(value: ManagedProcess) -> ManagedProcess:
    return value


def _accept_managed_port(value: ManagedProcessPort) -> ManagedProcessPort:
    return value


def test_protocols_are_structurally_usable_without_runtime_check_claims() -> None:
    process = _accept_managed_process(_FakeManagedProcess())
    port = _accept_managed_port(_FakeManagedProcessPort())
    with port.start(_launch()) as started:
        assert started.wait() == ProcessExit(ProcessEnd.EXITED, 0)
    assert process.stop(cause=StopCause.CALLER_ERROR).end is ProcessEnd.STOPPED
    assert port.ensure_quiescent(_scope(), cause=StopCause.RECOVERY) == StopResult(
        StopStatus.ABSENT, False
    )
