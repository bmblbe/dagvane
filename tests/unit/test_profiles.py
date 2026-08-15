"""Strict TOML profile boundary validation (G1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from dagvane.domain.models import SpecError
from dagvane.protocol.frames import sha256_hex
from dagvane.protocol.profiles import (
    COUNCIL_ROLE_SLOTS,
    load_profile_file,
    parse_profile,
)

HAPPY_PROFILE = """\
profile_version = 1

[connections.anthro]
kind = "anthropic"
credential_env = "TEST_ANTHROPIC_KEY"

[connections.compat]
kind = "openai_compat"
base_url = "https://api.example.test/v1"
credential_env = "TEST_COMPAT_KEY"
timeout_seconds = 45

[routes.prop-a]
connection = "anthro"
model = "claude-sonnet-5"
max_output_tokens = 4096
input_microusd_per_mtok = 3000000
output_microusd_per_mtok = 15000000

[routes.prop-b]
connection = "compat"
model = "other-model"
max_output_tokens = 2048
input_microusd_per_mtok = 1000000
output_microusd_per_mtok = 2000000

[council]
proposer_a = "prop-a"
proposer_b = "prop-b"
reviewer_a = "prop-b"
reviewer_b = "prop-a"
judge = "prop-a"
"""


def parse(text: str) -> object:
    return parse_profile(text.encode("utf-8"), source="test-profile")


def test_happy_profile_parses_completely() -> None:
    profile = parse_profile(HAPPY_PROFILE.encode("utf-8"), source="test-profile")
    assert profile.sha256 == sha256_hex(HAPPY_PROFILE.encode("utf-8"))
    assert set(profile.connections.keys()) == {"anthro", "compat"}
    anthro = profile.connections["anthro"]
    assert anthro.kind == "anthropic"
    assert anthro.credential_env == "TEST_ANTHROPIC_KEY"
    assert anthro.base_url is None
    assert anthro.timeout_seconds == 120  # default
    compat = profile.connections["compat"]
    assert compat.base_url == "https://api.example.test/v1"
    assert compat.timeout_seconds == 45

    route = profile.routes["prop-a"]
    assert route.backend == "anthro"
    assert route.model == "claude-sonnet-5"
    assert route.max_output_tokens == 4096
    assert route.pricing.input_microusd_per_mtok == 3_000_000

    role_routes = profile.role_routes()
    assert set(role_routes.keys()) == set(COUNCIL_ROLE_SLOTS)
    assert role_routes["judge"].route_id == "prop-a"
    assert set(profile.used_connections().keys()) == {"anthro", "compat"}


def test_no_credential_values_anywhere_in_profile() -> None:
    profile = parse_profile(HAPPY_PROFILE.encode("utf-8"), source="test-profile")
    # A profile names environment variables; it can never carry values.
    for connection in profile.connections.values():
        assert connection.credential_env.isidentifier()


def test_load_profile_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="cannot read profile"):
        load_profile_file(tmp_path / "absent.toml")


def test_load_profile_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "p.toml"
    path.write_text(HAPPY_PROFILE, encoding="utf-8")
    profile = load_profile_file(path)
    assert profile.council["proposer_a"] == "prop-a"


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda s: "not toml [", "not valid TOML"),
        (lambda s: s.replace("profile_version = 1", "profile_version = 2"), "unsupported"),
        (lambda s: s.replace("profile_version = 1", ""), "profile_version"),
        (lambda s: s + "\nstray = 1\n", "unknown keys"),
        (lambda s: s.replace('kind = "anthropic"', 'kind = "mystery"'), "must be one of"),
        (
            lambda s: s.replace('credential_env = "TEST_ANTHROPIC_KEY"\n', ""),
            "credential_env",
        ),
        (
            lambda s: s.replace('base_url = "https://api.example.test/v1"\n', ""),
            "base_url is required",
        ),
        (
            lambda s: s.replace(
                'base_url = "https://api.example.test/v1"', 'base_url = "ftp://x"'
            ),
            "must start with http",
        ),
        (
            lambda s: s.replace("timeout_seconds = 45", "timeout_seconds = 0"),
            "timeout_seconds",
        ),
        (
            lambda s: s.replace('connection = "anthro"', 'connection = "ghost"', 1),
            "unknown connection",
        ),
        (
            lambda s: s.replace("max_output_tokens = 4096", "max_output_tokens = 0"),
            "max_output_tokens",
        ),
        (
            lambda s: s.replace(
                "input_microusd_per_mtok = 3000000", "input_microusd_per_mtok = -1"
            ),
            "input_microusd_per_mtok",
        ),
        (lambda s: s.replace('judge = "prop-a"\n', ""), "judge"),
        (lambda s: s.replace('judge = "prop-a"', 'judge = "ghost-route"'), "unknown route"),
        (lambda s: s + '\n[council.extra]\nx = "y"\n', "unknown keys"),
        (
            lambda s: s.replace("[routes.prop-a]", "[routes.prop-a]\nsurprise = 1"),
            "unknown keys",
        ),
    ],
)
def test_malformed_profiles_are_rejected(mutation, match: str) -> None:  # type: ignore[no-untyped-def]
    mutated = mutation(HAPPY_PROFILE)
    with pytest.raises(SpecError, match=match):
        parse_profile(mutated.encode("utf-8"), source="test-profile")
