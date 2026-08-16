"""Evidence Execution Port V1: value type validation and result-matrix tests."""

from __future__ import annotations

import dataclasses

import pytest

from dagvane.domain.models import SpecError
from dagvane.ports.evidence import (
    EVIDENCE_MAX_OUTPUT_BYTES,
    EVIDENCE_PORT_API_VERSION,
    EvidenceArtifactRefV1,
    EvidenceCommandV1,
    EvidenceExecutorV1,
    EvidencePurposeV1,
    EvidenceReportV1,
    EvidenceSandboxRequirementV1,
    EvidenceValidityV1,
    EvidenceViewV1,
    validate_api_version,
    validate_fresh_views,
    validate_report_binding,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "sha256:" + "c" * 64


def make_command(**overrides: object) -> EvidenceCommandV1:
    fields: dict[str, object] = {
        "command_id": "cmd-1",
        "purpose": EvidencePurposeV1.VERIFY,
        "source_sha": SHA_A,
        "argv": ("pytest", "-q"),
        "wall_timeout_seconds": 30.0,
        "max_output_bytes": 4096,
        "sandbox_requirement": EvidenceSandboxRequirementV1.REQUIRED,
        "grant_ref": None,
    }
    fields.update(overrides)
    return EvidenceCommandV1(**fields)  # type: ignore[arg-type]


def make_view(**overrides: object) -> EvidenceViewV1:
    fields: dict[str, object] = {
        "view_id": "view-1",
        "source_sha": SHA_A,
        "disposable": True,
        "command_ordinal": 0,
    }
    fields.update(overrides)
    return EvidenceViewV1(**fields)  # type: ignore[arg-type]


def make_artifact(**overrides: object) -> EvidenceArtifactRefV1:
    fields: dict[str, object] = {
        "digest": DIGEST_A,
        "size_bytes": 128,
        "media_type": "text/plain",
        "role": "stdout",
    }
    fields.update(overrides)
    return EvidenceArtifactRefV1(**fields)  # type: ignore[arg-type]


def make_report(**overrides: object) -> EvidenceReportV1:
    fields: dict[str, object] = {
        "command_id": "cmd-1",
        "view_id": "view-1",
        "source_sha": SHA_A,
        "exit_status": 0,
        "head_before": SHA_A,
        "head_after": SHA_A,
        "tracked_or_index_mutation": False,
        "untracked_or_ignored_observed": False,
        "output_artifacts": (make_artifact(),),
        "timed_out": False,
        "cancelled": False,
        "cleanup_complete": True,
        "validity": EvidenceValidityV1.VALID,
    }
    fields.update(overrides)
    return EvidenceReportV1(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# API version
# ---------------------------------------------------------------------------


def test_api_version_constant_is_one() -> None:
    assert EVIDENCE_PORT_API_VERSION == 1


def test_validate_api_version_accepts_exact_int() -> None:
    assert validate_api_version(1) == 1


@pytest.mark.parametrize("value", [True, False, 2, 0, -1, 1.0, "1", None])
def test_validate_api_version_rejects_non_exact_int(value: object) -> None:
    with pytest.raises(SpecError):
        validate_api_version(value)


# ---------------------------------------------------------------------------
# EvidenceCommandV1
# ---------------------------------------------------------------------------


def test_command_valid_construction() -> None:
    command = make_command()
    assert command.command_id == "cmd-1"
    assert command.argv == ("pytest", "-q")


def test_command_argv_never_a_bare_string() -> None:
    with pytest.raises(SpecError, match="tuple"):
        make_command(argv="pytest -q")


def test_command_argv_empty_rejected() -> None:
    with pytest.raises(SpecError, match="nonempty"):
        make_command(argv=())


def test_command_argv_non_str_element_rejected() -> None:
    with pytest.raises(SpecError):
        make_command(argv=("pytest", 1))


def test_command_argv_list_container_rejected() -> None:
    with pytest.raises(SpecError):
        make_command(argv=["pytest", "-q"])


@pytest.mark.parametrize("bad_char", ["\x00", "\x01", "\x1f", "\x7f", "\n"])
def test_command_argv_control_and_nul_rejected(bad_char: str) -> None:
    with pytest.raises(SpecError, match="control"):
        make_command(argv=("pytest", f"bad{bad_char}arg"))


def test_command_argv_oversized_element_rejected() -> None:
    with pytest.raises(SpecError, match="length"):
        make_command(argv=("x" * 5000,))


def test_command_argv_oversized_count_rejected() -> None:
    with pytest.raises(SpecError, match="element count"):
        make_command(argv=tuple(f"arg{i}" for i in range(300)))


def test_command_id_invalid_rejected() -> None:
    with pytest.raises(SpecError):
        make_command(command_id="../escape")


@pytest.mark.parametrize("value", ["not-40-hex", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "", None])
def test_command_source_sha_invalid_rejected(value: object) -> None:
    with pytest.raises(SpecError, match="SHA-1"):
        make_command(source_sha=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_command_timeout_non_finite_or_non_positive_rejected(value: float) -> None:
    with pytest.raises(SpecError):
        make_command(wall_timeout_seconds=value)


@pytest.mark.parametrize("value", [True, False, 30, "30"])
def test_command_timeout_rejects_non_float(value: object) -> None:
    with pytest.raises(SpecError, match="float"):
        make_command(wall_timeout_seconds=value)


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "4096"])
def test_command_max_output_bytes_rejects_bad_int(value: object) -> None:
    with pytest.raises(SpecError):
        make_command(max_output_bytes=value)


def test_command_max_output_bytes_exceeding_ceiling_rejected() -> None:
    with pytest.raises(SpecError, match="ceiling"):
        make_command(max_output_bytes=EVIDENCE_MAX_OUTPUT_BYTES + 1)


def test_command_max_output_bytes_at_ceiling_accepted() -> None:
    command = make_command(max_output_bytes=EVIDENCE_MAX_OUTPUT_BYTES)
    assert command.max_output_bytes == EVIDENCE_MAX_OUTPUT_BYTES


def test_command_purpose_rejects_raw_string() -> None:
    with pytest.raises(SpecError, match="EvidencePurposeV1"):
        make_command(purpose="verify")


def test_command_sandbox_requirement_rejects_raw_string() -> None:
    with pytest.raises(SpecError, match="EvidenceSandboxRequirementV1"):
        make_command(sandbox_requirement="required")


def test_command_required_sandbox_forbids_grant() -> None:
    with pytest.raises(SpecError, match="forbids"):
        make_command(
            sandbox_requirement=EvidenceSandboxRequirementV1.REQUIRED,
            grant_ref="grant-abc",
        )


def test_command_trusted_grant_requires_reference() -> None:
    with pytest.raises(SpecError, match="requires a grant"):
        make_command(
            sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
            grant_ref=None,
        )


def test_command_trusted_grant_rejects_empty_reference() -> None:
    with pytest.raises(SpecError, match="nonempty"):
        make_command(
            sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
            grant_ref="",
        )


def test_command_trusted_grant_rejects_oversized_reference() -> None:
    with pytest.raises(SpecError, match="length"):
        make_command(
            sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
            grant_ref="g" * 5000,
        )


def test_command_trusted_grant_accepted_with_reference() -> None:
    command = make_command(
        sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
        grant_ref="grant-abc",
    )
    assert command.grant_ref == "grant-abc"


def test_command_argv_invalid_element_not_reflected_in_exception() -> None:
    sentinel = "SENTINEL_ARGV_VALUE\x00"
    with pytest.raises(SpecError) as excinfo:
        make_command(argv=("pytest", sentinel))
    assert sentinel not in str(excinfo.value)
    assert "SENTINEL_ARGV_VALUE" not in str(excinfo.value)


def test_command_grant_ref_invalid_value_not_reflected_in_exception() -> None:
    sentinel = "SENTINEL_GRANT_\x01VALUE"
    with pytest.raises(SpecError) as excinfo:
        make_command(
            sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
            grant_ref=sentinel,
        )
    assert sentinel not in str(excinfo.value)
    assert "SENTINEL_GRANT_" not in str(excinfo.value)


def test_command_repr_redacts_argv_and_grant() -> None:
    command = make_command(
        sandbox_requirement=EvidenceSandboxRequirementV1.TRUSTED_PROJECT_GRANT,
        grant_ref="super-secret-grant",
        argv=("pytest", "--secret-flag=hunter2"),
    )
    text = repr(command)
    assert "super-secret-grant" not in text
    assert "hunter2" not in text
    assert "redacted" in text


def test_command_is_frozen() -> None:
    command = make_command()
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.command_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EvidenceViewV1
# ---------------------------------------------------------------------------


def test_view_valid_construction() -> None:
    view = make_view()
    assert view.disposable is True
    assert view.command_ordinal == 0


def test_view_disposable_false_rejected() -> None:
    with pytest.raises(SpecError, match="True"):
        make_view(disposable=False)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_view_disposable_rejects_non_bool(value: object) -> None:
    with pytest.raises(SpecError):
        make_view(disposable=value)


@pytest.mark.parametrize("value", [-1, True, False, 1.5, "0"])
def test_view_command_ordinal_rejects_bad_values(value: object) -> None:
    with pytest.raises(SpecError):
        make_view(command_ordinal=value)


def test_view_id_invalid_rejected() -> None:
    with pytest.raises(SpecError):
        make_view(view_id="/abs/path")


def test_view_source_sha_invalid_rejected() -> None:
    with pytest.raises(SpecError, match="SHA-1"):
        make_view(source_sha="not-a-sha")


# ---------------------------------------------------------------------------
# EvidenceArtifactRefV1
# ---------------------------------------------------------------------------


def test_artifact_valid_construction() -> None:
    artifact = make_artifact()
    assert artifact.digest == DIGEST_A


@pytest.mark.parametrize(
    "value",
    ["sha256:short", "md5:" + "a" * 32, "sha256:" + "A" * 64, "sha256" + "a" * 64, ""],
)
def test_artifact_digest_malformed_rejected(value: str) -> None:
    with pytest.raises(SpecError, match="digest"):
        make_artifact(digest=value)


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "128"])
def test_artifact_size_bytes_rejects_bad_values(value: object) -> None:
    with pytest.raises(SpecError):
        make_artifact(size_bytes=value)


def test_artifact_size_bytes_exceeding_ceiling_rejected() -> None:
    with pytest.raises(SpecError, match="ceiling"):
        make_artifact(size_bytes=EVIDENCE_MAX_OUTPUT_BYTES + 1)


def test_artifact_media_type_empty_rejected() -> None:
    with pytest.raises(SpecError, match="nonempty"):
        make_artifact(media_type="")


def test_artifact_media_type_oversized_rejected() -> None:
    with pytest.raises(SpecError, match="length"):
        make_artifact(media_type="m" * 300)


def test_artifact_media_type_control_char_rejected() -> None:
    with pytest.raises(SpecError, match="control"):
        make_artifact(media_type="text/\x00plain")


def test_artifact_media_type_invalid_value_not_reflected_in_exception() -> None:
    sentinel = "SENTINEL_MEDIA_\x02TYPE"
    with pytest.raises(SpecError) as excinfo:
        make_artifact(media_type=sentinel)
    assert sentinel not in str(excinfo.value)
    assert "SENTINEL_MEDIA_" not in str(excinfo.value)


def test_artifact_role_empty_rejected() -> None:
    with pytest.raises(SpecError, match="nonempty"):
        make_artifact(role="")


def test_artifact_role_control_char_rejected() -> None:
    with pytest.raises(SpecError, match="control"):
        make_artifact(role="std\nout")


# ---------------------------------------------------------------------------
# EvidenceReportV1: valid matrix
# ---------------------------------------------------------------------------


def test_report_valid_clean_run() -> None:
    report = make_report()
    assert report.validity is EvidenceValidityV1.VALID


def test_report_valid_with_untracked_observed_does_not_invalidate() -> None:
    report = make_report(untracked_or_ignored_observed=True)
    assert report.validity is EvidenceValidityV1.VALID


def test_report_command_failed_on_nonzero_exit() -> None:
    report = make_report(exit_status=1, validity=EvidenceValidityV1.COMMAND_FAILED)
    assert report.validity is EvidenceValidityV1.COMMAND_FAILED


def test_report_command_failed_on_none_exit_status() -> None:
    report = make_report(exit_status=None, validity=EvidenceValidityV1.COMMAND_FAILED)
    assert report.validity is EvidenceValidityV1.COMMAND_FAILED


def test_report_evidence_invalid_on_moved_head() -> None:
    report = make_report(
        head_after=SHA_B,
        validity=EvidenceValidityV1.EVIDENCE_INVALID,
    )
    assert report.validity is EvidenceValidityV1.EVIDENCE_INVALID


def test_report_evidence_invalid_on_tracked_mutation() -> None:
    report = make_report(
        tracked_or_index_mutation=True,
        validity=EvidenceValidityV1.EVIDENCE_INVALID,
    )
    assert report.validity is EvidenceValidityV1.EVIDENCE_INVALID


def test_report_cancelled_with_clean_view() -> None:
    report = make_report(cancelled=True, validity=EvidenceValidityV1.CANCELLED)
    assert report.validity is EvidenceValidityV1.CANCELLED


def test_report_cancelled_with_incomplete_cleanup() -> None:
    report = make_report(
        cancelled=True,
        cleanup_complete=False,
        validity=EvidenceValidityV1.CLEANUP_INCOMPLETE,
    )
    assert report.validity is EvidenceValidityV1.CLEANUP_INCOMPLETE


def test_report_timed_out_command_failed() -> None:
    report = make_report(
        timed_out=True, exit_status=None, validity=EvidenceValidityV1.COMMAND_FAILED
    )
    assert report.validity is EvidenceValidityV1.COMMAND_FAILED


def test_report_timed_out_with_incomplete_cleanup() -> None:
    report = make_report(
        timed_out=True,
        exit_status=None,
        cleanup_complete=False,
        validity=EvidenceValidityV1.CLEANUP_INCOMPLETE,
    )
    assert report.validity is EvidenceValidityV1.CLEANUP_INCOMPLETE


# ---------------------------------------------------------------------------
# EvidenceReportV1: full invalid matrix (validity inconsistent with fields)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"exit_status": 1},  # nonzero exit but claims VALID
        {"head_after": SHA_B},  # moved HEAD but claims VALID
        {"head_before": SHA_B},
        {"tracked_or_index_mutation": True},
        {"timed_out": True},
        {"cancelled": True},
    ],
    ids=["nonzero-exit", "head-after-moved", "head-before-moved", "mutation", "timeout", "cancel"],
)
def test_report_valid_claim_rejected_when_matrix_disagrees(overrides: dict[str, object]) -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(**overrides)  # validity stays VALID by default


def test_report_evidence_invalid_claim_rejected_on_clean_run() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(validity=EvidenceValidityV1.EVIDENCE_INVALID)


def test_report_command_failed_claim_rejected_on_clean_exit() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(validity=EvidenceValidityV1.COMMAND_FAILED)


def test_report_cancelled_claim_rejected_without_cancelled_flag() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(validity=EvidenceValidityV1.CANCELLED)


def test_report_cleanup_incomplete_claim_rejected_without_cancel_or_timeout() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(validity=EvidenceValidityV1.CLEANUP_INCOMPLETE)


def test_report_moved_head_wins_over_cancelled_claim() -> None:
    """Mutation/HEAD-move is dispositive even when cancelled is also set."""
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(
            cancelled=True,
            head_after=SHA_B,
            validity=EvidenceValidityV1.CANCELLED,
        )


def test_report_mutation_forces_evidence_invalid_even_when_cancelled() -> None:
    report = make_report(
        cancelled=True,
        tracked_or_index_mutation=True,
        validity=EvidenceValidityV1.EVIDENCE_INVALID,
    )
    assert report.validity is EvidenceValidityV1.EVIDENCE_INVALID


# ---------------------------------------------------------------------------
# EvidenceReportV1: cleanup_complete gates every other claim
# ---------------------------------------------------------------------------


def test_report_cleanup_incomplete_forces_only_cleanup_incomplete_on_clean_exit() -> None:
    report = make_report(cleanup_complete=False, validity=EvidenceValidityV1.CLEANUP_INCOMPLETE)
    assert report.validity is EvidenceValidityV1.CLEANUP_INCOMPLETE


def test_report_valid_claim_rejected_when_cleanup_incomplete() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(cleanup_complete=False, validity=EvidenceValidityV1.VALID)


def test_report_cancelled_claim_rejected_when_cleanup_incomplete() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(
            cancelled=True,
            cleanup_complete=False,
            validity=EvidenceValidityV1.CANCELLED,
        )


def test_report_timed_out_command_failed_rejected_when_cleanup_incomplete() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(
            timed_out=True,
            exit_status=None,
            cleanup_complete=False,
            validity=EvidenceValidityV1.COMMAND_FAILED,
        )


def test_report_cleanup_incomplete_dominates_moved_head() -> None:
    """CLEANUP_INCOMPLETE wins even when HEAD moved, since the move itself
    is unproven stable while descendants may still be running."""
    report = make_report(
        head_after=SHA_B,
        cleanup_complete=False,
        validity=EvidenceValidityV1.CLEANUP_INCOMPLETE,
    )
    assert report.validity is EvidenceValidityV1.CLEANUP_INCOMPLETE


def test_report_evidence_invalid_claim_rejected_when_cleanup_incomplete() -> None:
    with pytest.raises(SpecError, match="inconsistent"):
        make_report(
            head_after=SHA_B,
            cleanup_complete=False,
            validity=EvidenceValidityV1.EVIDENCE_INVALID,
        )


def test_report_cleanup_incomplete_dominates_mutation() -> None:
    report = make_report(
        tracked_or_index_mutation=True,
        cleanup_complete=False,
        validity=EvidenceValidityV1.CLEANUP_INCOMPLETE,
    )
    assert report.validity is EvidenceValidityV1.CLEANUP_INCOMPLETE


# ---------------------------------------------------------------------------
# EvidenceReportV1: field-level validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_report_cleanup_complete_rejects_non_bool(value: object) -> None:
    with pytest.raises(SpecError):
        make_report(cleanup_complete=value)


@pytest.mark.parametrize("value", [True, False, -1, 1.5, "0"])
def test_report_exit_status_rejects_bad_values(value: object) -> None:
    with pytest.raises(SpecError):
        make_report(exit_status=value)


def test_report_exit_status_none_allowed_with_matching_validity() -> None:
    report = make_report(
        exit_status=None, timed_out=True, validity=EvidenceValidityV1.COMMAND_FAILED
    )
    assert report.exit_status is None


def test_report_head_sha_malformed_rejected() -> None:
    with pytest.raises(SpecError, match="SHA-1"):
        make_report(head_before="not-a-sha")


def test_report_validity_rejects_raw_string() -> None:
    with pytest.raises(SpecError, match="EvidenceValidityV1"):
        make_report(validity="valid")


def test_report_output_artifacts_rejects_list_container() -> None:
    with pytest.raises(SpecError, match="tuple"):
        make_report(output_artifacts=[make_artifact()])


def test_report_output_artifacts_rejects_non_artifact_elements() -> None:
    with pytest.raises(SpecError):
        make_report(output_artifacts=("not-an-artifact",))


def test_report_output_artifacts_oversized_rejected() -> None:
    with pytest.raises(SpecError, match="reference count"):
        make_report(output_artifacts=tuple(make_artifact() for _ in range(100)))


def test_report_repr_redacts_output_artifacts() -> None:
    text = repr(make_report())
    assert DIGEST_A not in text
    assert "redacted" in text


def test_report_is_frozen() -> None:
    report = make_report()
    with pytest.raises(dataclasses.FrozenInstanceError):
        report.validity = EvidenceValidityV1.COMMAND_FAILED  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Binding invariants
# ---------------------------------------------------------------------------


def test_validate_report_binding_accepts_matching_triple() -> None:
    validate_report_binding(make_report(), make_command(), make_view())


def test_validate_report_binding_rejects_command_id_mismatch() -> None:
    report = make_report(command_id="cmd-2")
    with pytest.raises(SpecError, match="command_id"):
        validate_report_binding(report, make_command(), make_view())


def test_validate_report_binding_rejects_view_id_mismatch() -> None:
    report = make_report(view_id="view-2")
    with pytest.raises(SpecError, match="view_id"):
        validate_report_binding(report, make_command(), make_view())


def test_validate_report_binding_rejects_source_sha_mismatch() -> None:
    report = make_report(source_sha=SHA_B, head_before=SHA_B, head_after=SHA_B)
    with pytest.raises(SpecError, match="source_sha"):
        validate_report_binding(report, make_command(), make_view())


def test_validate_report_binding_rejects_command_view_sha_disagreement() -> None:
    view = make_view(source_sha=SHA_B)
    with pytest.raises(SpecError, match="source_sha"):
        validate_report_binding(make_report(), make_command(), view)


def test_validate_fresh_views_accepts_unique_view_ids() -> None:
    reports = (
        make_report(command_id="cmd-1", view_id="view-1"),
        make_report(command_id="cmd-2", view_id="view-2"),
    )
    validate_fresh_views(reports)


def test_validate_fresh_views_rejects_view_reused_across_commands() -> None:
    reports = (
        make_report(command_id="cmd-1", view_id="view-1"),
        make_report(command_id="cmd-2", view_id="view-1"),
    )
    with pytest.raises(SpecError, match="fresh view"):
        validate_fresh_views(reports)


def test_validate_fresh_views_rejects_view_reused_for_same_command() -> None:
    reports = (
        make_report(command_id="cmd-1", view_id="view-1"),
        make_report(command_id="cmd-1", view_id="view-1"),
    )
    with pytest.raises(SpecError, match="fresh view"):
        validate_fresh_views(reports)


def test_validate_fresh_views_accepts_empty_sequence() -> None:
    validate_fresh_views(())


# ---------------------------------------------------------------------------
# Protocol shape: a minimal fake proves the interface is implementable
# ---------------------------------------------------------------------------


class _FakeExecutor:
    def execute(self, command: EvidenceCommandV1, view: EvidenceViewV1) -> EvidenceReportV1:
        validate_report_binding(
            make_report(command_id=command.command_id, view_id=view.view_id),
            command,
            view,
        )
        return make_report(command_id=command.command_id, view_id=view.view_id)


def test_fake_executor_satisfies_protocol() -> None:
    executor: EvidenceExecutorV1 = _FakeExecutor()
    report = executor.execute(make_command(), make_view())
    assert report.validity is EvidenceValidityV1.VALID


def test_fake_executor_report_binds_exactly() -> None:
    executor = _FakeExecutor()
    command = make_command(command_id="cmd-9")
    view = make_view(view_id="view-9")
    report = executor.execute(command, view)
    validate_report_binding(report, command, view)
