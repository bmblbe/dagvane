"""Canonical filesystem identifier contract: boundary matrix.

Covers the shared validator directly, plus proof that the protocol boundary
(fixture ``run_id``) enforces the identical rule and passes through valid
values unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dagvane.domain.identifiers import validate_filesystem_id
from dagvane.domain.models import SpecError
from dagvane.protocol.documents import load_fixture_file

VALID = [
    "a",
    "A",
    "0",
    "a" * 64,
    "run-01",
    "run_01",
    "run.01",
    "Run.1-2_3",
    "0abc",
    "a.b.c",
]


@pytest.mark.parametrize("value", VALID)
def test_valid_boundaries_pass_through_unchanged(value: str) -> None:
    assert validate_filesystem_id(value, ctx="id") == value


INVALID_NON_STRING: list[object] = [None, 123, 1.5, True, [], {}, b"run-01"]


@pytest.mark.parametrize("value", INVALID_NON_STRING)
def test_non_string_rejected(value: object) -> None:
    with pytest.raises(SpecError, match="must be a string"):
        validate_filesystem_id(value, ctx="id")


INVALID_STRINGS = [
    "",
    ".",
    "..",
    ".hidden",
    "-leading-dash",
    "_leading-underscore",
    "/abs/path",
    "../escape",
    "a/b",
    "a\\b",
    "C:\\Windows",
    r"\\server\share",
    "a b",
    " leading",
    "trailing ",
    "\ta",
    "a\n",
    "a\x00b",
    "a\x7fb",
    "a" * 65,
    "café",
    "run\u2044id",  # fraction slash confusable
    "run\u200bid",  # zero-width space
    "\u202eesrever",  # right-to-left override (bidi)
    "рun-01",  # Cyrillic "р" confusable for latin "p"
]


@pytest.mark.parametrize("value", INVALID_STRINGS)
def test_invalid_strings_rejected(value: str) -> None:
    with pytest.raises(SpecError, match="must match"):
        validate_filesystem_id(value, ctx="id")


def test_error_context_included() -> None:
    with pytest.raises(SpecError, match=r"my ctx"):
        validate_filesystem_id("bad/id", ctx="my ctx")


def test_protocol_boundary_shares_same_rule_and_value_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps({"fixture_version": 1, "run_id": "Run.1-2_3", "responses": {"m": {"text": "t"}}})
    )
    spec = load_fixture_file(path)
    assert spec.run_id == "Run.1-2_3"


def test_protocol_boundary_rejects_invalid_run_id(tmp_path: Path) -> None:
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps({"fixture_version": 1, "run_id": "../escape", "responses": {"m": {"text": "t"}}})
    )
    with pytest.raises(SpecError, match="run_id"):
        load_fixture_file(path)
