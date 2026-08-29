"""Tier resolution through the interface, never through roles.yaml directly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gads_write.auth.roles import (
    FileTierResolver,
    RoleConfigError,
    RoleStore,
    RoleTable,
)
from gads_write.auth.tiers import Tier, TierResolver, highest, tier_at_least
from gads_write.tools.registry import ToolSpec, spec_for


@dataclass(frozen=True)
class FakeCaller:
    """Stands in for auth.identity.Caller without needing a FastMCP request."""

    email: str


def _roles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "roles.yaml"
    path.write_text(body, encoding="utf-8")
    return path


FILE_MODE = """
mode: file
default_tier: none
users:
  "lead@example.com": lead
  "op@example.com": operator
  "analyst@example.com": readonly
"""


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------

def test_tier_ranking() -> None:
    assert tier_at_least(Tier.LEAD, Tier.OPERATOR)
    assert tier_at_least(Tier.OPERATOR, Tier.OPERATOR)
    assert not tier_at_least(Tier.READONLY, Tier.OPERATOR)
    assert not tier_at_least(Tier.NONE, Tier.READONLY)


def test_highest() -> None:
    assert highest([Tier.READONLY, Tier.LEAD, Tier.NONE]) is Tier.LEAD
    assert highest([]) is Tier.NONE


# ---------------------------------------------------------------------------
# the file resolver satisfies the interface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_resolver_satisfies_the_protocol(tmp_path: Path) -> None:
    resolver = FileTierResolver(RoleStore(_roles(tmp_path, FILE_MODE)))
    assert isinstance(resolver, TierResolver)


@pytest.mark.asyncio
async def test_resolves_known_users(tmp_path: Path) -> None:
    resolver = FileTierResolver(RoleStore(_roles(tmp_path, FILE_MODE)))
    assert await resolver.resolve(FakeCaller("lead@example.com"), "1234567890") is Tier.LEAD
    assert await resolver.resolve(FakeCaller("op@example.com"), "1234567890") is Tier.OPERATOR


@pytest.mark.asyncio
async def test_unknown_user_gets_none(tmp_path: Path) -> None:
    resolver = FileTierResolver(RoleStore(_roles(tmp_path, FILE_MODE)))
    assert await resolver.resolve(FakeCaller("stranger@example.com"), "1234567890") is Tier.NONE


@pytest.mark.asyncio
async def test_email_matching_is_case_insensitive(tmp_path: Path) -> None:
    resolver = FileTierResolver(RoleStore(_roles(tmp_path, FILE_MODE)))
    assert await resolver.resolve(FakeCaller("  LEAD@EXAMPLE.COM "), "1234567890") is Tier.LEAD


@pytest.mark.asyncio
async def test_customer_id_is_ignored_by_the_file_backing(tmp_path: Path) -> None:
    # Documents the known limitation: a file has no per-account concept.
    # Phase 3's resolver is where these two answers start to differ.
    resolver = FileTierResolver(RoleStore(_roles(tmp_path, FILE_MODE)))
    caller = FakeCaller("op@example.com")
    assert await resolver.resolve(caller, "1111111111") is Tier.OPERATOR
    assert await resolver.resolve(caller, "2222222222") is Tier.OPERATOR
    assert await resolver.visible_tier(caller) is Tier.OPERATOR


# ---------------------------------------------------------------------------
# hot reload: a demotion must land on the very next call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_demotion_takes_effect_on_the_next_call(tmp_path: Path) -> None:
    """The gate condition.

    No reconnect, no restart. Someone removed from the team must lose their
    powers immediately, not at the end of their session.
    """
    path = _roles(tmp_path, FILE_MODE)
    resolver = FileTierResolver(RoleStore(path))
    caller = FakeCaller("op@example.com")

    assert await resolver.resolve(caller, "1234567890") is Tier.OPERATOR

    path.write_text(
        'mode: file\ndefault_tier: none\nusers:\n  "lead@example.com": lead\n',
        encoding="utf-8",
    )

    assert await resolver.resolve(caller, "1234567890") is Tier.NONE


@pytest.mark.asyncio
async def test_a_promotion_also_takes_effect_immediately(tmp_path: Path) -> None:
    path = _roles(tmp_path, FILE_MODE)
    resolver = FileTierResolver(RoleStore(path))
    caller = FakeCaller("analyst@example.com")

    assert await resolver.resolve(caller, "1234567890") is Tier.READONLY

    path.write_text(
        'mode: file\ndefault_tier: none\nusers:\n'
        '  "lead@example.com": lead\n  "analyst@example.com": operator\n',
        encoding="utf-8",
    )

    assert await resolver.resolve(caller, "1234567890") is Tier.OPERATOR


@pytest.mark.asyncio
async def test_a_broken_edit_keeps_the_last_good_table(tmp_path: Path) -> None:
    # A YAML typo must not grant anyone anything, and must not crash a
    # request that is already in flight.
    path = _roles(tmp_path, FILE_MODE)
    store = RoleStore(path)
    resolver = FileTierResolver(store)
    caller = FakeCaller("op@example.com")

    path.write_text("users: [unclosed\n", encoding="utf-8")

    assert await resolver.resolve(caller, "1234567890") is Tier.OPERATOR
    assert store.last_error is not None
    assert store.reload_count == 0


@pytest.mark.asyncio
async def test_an_edit_that_removes_every_lead_is_refused(tmp_path: Path) -> None:
    path = _roles(tmp_path, FILE_MODE)
    store = RoleStore(path)
    resolver = FileTierResolver(store)

    path.write_text(
        'mode: file\ndefault_tier: none\nusers:\n  "op@example.com": operator\n',
        encoding="utf-8",
    )

    # last good table retained, so the existing lead keeps working
    assert await resolver.resolve(FakeCaller("lead@example.com"), "1234567890") is Tier.LEAD
    assert store.last_error is not None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def test_invalid_tier_is_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigError):
        RoleTable.load(
            _roles(tmp_path, 'mode: file\nusers:\n  "a@b.com": opperator\n')
        )


def test_invalid_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigError, match="mode"):
        RoleTable.load(_roles(tmp_path, 'mode: whatever\nusers:\n  "a@b.com": lead\n'))


def test_file_mode_requires_a_lead(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigError, match="lead"):
        RoleTable.load(_roles(tmp_path, 'mode: file\nusers:\n  "a@b.com": operator\n'))


def test_google_ads_mode_allows_an_empty_users_map(tmp_path: Path) -> None:
    # In Phase 3 this map is a break-glass override and is normally empty,
    # so the "must have a lead" rule must not apply.
    table = RoleTable.load(_roles(tmp_path, "mode: google_ads\nusers: {}\n"))
    assert table.mode == "google_ads"
    assert table.tier_for("anyone@example.com") is Tier.NONE


def test_the_shipped_roles_file_loads() -> None:
    shipped = Path(__file__).resolve().parents[1] / "config" / "roles.yaml"
    table = RoleTable.load(shipped)
    assert table.mode in {"file", "google_ads"}


# ---------------------------------------------------------------------------
# registry defaults
# ---------------------------------------------------------------------------

def test_an_unregistered_tool_fails_closed() -> None:
    """Forgetting to register a tool must make it MORE restricted, not less."""
    spec = spec_for("some_tool_nobody_registered")
    assert spec.required_tier is Tier.LEAD
    assert spec.writes is True


def test_health_check_is_available_at_tier_none() -> None:
    # Someone not yet listed must be able to find out why they have no tools.
    assert spec_for("health_check") == ToolSpec(
        name="health_check", required_tier=Tier.NONE, writes=False
    )
