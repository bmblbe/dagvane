"""SecretScrubber unit tests (Codex B2/B3 round 2)."""

from __future__ import annotations

import json

import pytest

from dagvane.domain.models import SpecError
from dagvane.domain.secrets import SecretScrubber, process_scrubber

TRICKY = 'sk-tr"ick\\y\'key-42'


def test_scrub_removes_all_first_and_second_level_renderings() -> None:
    scrubber = SecretScrubber()
    scrubber.register(TRICKY)
    forms = [
        TRICKY,
        TRICKY.encode("unicode_escape").decode("ascii"),
        json.dumps(TRICKY)[1:-1],
        repr(TRICKY)[1:-1],
        json.dumps(json.dumps(TRICKY)[1:-1])[1:-1],  # JSON-in-JSON
    ]
    for form in forms:
        scrubbed = scrubber.scrub(f"prefix {form} suffix")
        assert TRICKY not in scrubbed
        assert form not in scrubbed
        assert "[redacted]" in scrubbed


def test_nested_json_reflection_is_not_recoverable() -> None:
    """Codex probe: json.dumps({"error": json.dumps({"token": secret})}) —
    double-decoding the scrubbed body must not reconstruct the credential."""
    scrubber = SecretScrubber()
    scrubber.register(TRICKY)
    body = json.dumps({"error": json.dumps({"token": TRICKY})})
    scrubbed = scrubber.scrub(body)
    assert TRICKY not in scrubbed
    outer = json.loads(scrubbed)
    inner = json.loads(outer["error"])
    assert inner["token"] != TRICKY
    assert TRICKY not in inner["token"]


@pytest.mark.parametrize(
    "value",
    [
        "[redacted]",
        "redacted",
        "a",
        "ed]",
        "[red",
        "acted",
        # Boundary overlaps: a marker insertion abutting untouched text could
        # regenerate these faster than scrub passes remove them (Codex R3
        # probe: "]TOKEN123" against "]" + "TOKEN123" * N).
        "]TOKEN123",
        "d]TOKEN99",
        "TOKEN99-ends[",
    ],
    ids=[
        "marker-exact",
        "marker-word",
        "one-char-in-marker",
        "suffix",
        "prefix",
        "infix",
        "boundary-prefix",
        "boundary-prefix-2",
        "boundary-suffix",
    ],
)
def test_credentials_overlapping_the_marker_are_refused(value: str) -> None:
    """A credential whose bytes overlap the replacement marker — containment
    or boundary overlap — cannot be scrubbed reliably; it must be refused at
    the configuration boundary."""
    scrubber = SecretScrubber()
    with pytest.raises(SpecError, match="redaction marker"):
        scrubber.register(value)


def test_non_overlapping_short_credential_is_accepted() -> None:
    scrubber = SecretScrubber()
    scrubber.register("zq0x")  # no byte overlap with "[redacted]"
    assert scrubber.scrub("key zq0x here") == "key [redacted] here"


def test_scrub_is_stable_under_juxtaposition() -> None:
    """Replacement must not resurrect a rendering by abutting marker text
    with surviving bytes; passes repeat until stable."""
    scrubber = SecretScrubber()
    scrubber.register("SECRETONE9")
    scrubber.register("SECRETTWO9")
    text = "xSECRETONE9SECRETTWO9y" * 3
    scrubbed = scrubber.scrub(text)
    assert "SECRETONE9" not in scrubbed
    assert "SECRETTWO9" not in scrubbed


def test_repr_never_exposes_values() -> None:
    scrubber = SecretScrubber()
    scrubber.register("super-secret-value-000")
    assert "super-secret-value-000" not in repr(scrubber)


def test_process_scrubber_is_one_shared_registry() -> None:
    assert process_scrubber() is process_scrubber()
