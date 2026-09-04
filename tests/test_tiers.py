"""Tier ranking, and the break-glass override table.

`FileTierResolver` is gone with roles.yaml's `mode`. Tiers always come from
Google Ads now (tests/test_google_ads_tiers.py), and this file exists to
cover the one thing left in the roles file: a deliberate override, used when
Google cannot tell us someone's role and the team would otherwise be locked
out with no way back in but a redeploy.

The file itself is OPTIONAL. Its absence means "no overrides", which is the
ordinary state; its presence is the signal that something is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gads_write.auth.roles import RoleConfigError, RoleStore, RoleTable
from gads_write.auth.tiers import Tier, highest, tier_at_least
from gads_write.tools.registry import spec_for


@dataclass(frozen=True)
class FakeCaller:
    """Stands in for auth.identity.Caller without needing a FastMCP request."""

    email: str


def _roles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "roles.yaml"
    path.write_text(body, encoding="utf-8")
    return path


OVERRIDES = """
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
    assert highest([Tier.NONE, Tier.OPERATOR, Tier.READONLY]) is Tier.OPERATOR
    assert highest([]) is Tier.NONE


# ---------------------------------------------------------------------------
# the override table
# ---------------------------------------------------------------------------


def test_a_missing_file_means_no_overrides(tmp_path: Path) -> None:
    """The healthy state. Overrides are the exception, so requiring the file
    would mean shipping an empty one just to say "nothing to see here"."""
    table = RoleTable.load(tmp_path / "absent.yaml")
    assert table.users == {}
    assert table.override_for("anyone@example.com") is None


def test_an_empty_file_means_no_overrides(tmp_path: Path) -> None:
    table = RoleTable.load(_roles(tmp_path, "# nothing overridden\n"))
    assert table.users == {}


def test_a_listed_person_gets_their_override(tmp_path: Path) -> None:
    table = RoleTable.load(_roles(tmp_path, OVERRIDES))
    assert table.override_for("lead@example.com") is Tier.LEAD
    assert table.override_for("op@example.com") is Tier.OPERATOR


def test_an_unlisted_person_falls_through_to_google(tmp_path: Path) -> None:
    """None means "no override", NEVER "no access" - the caller asks Google
    on None, so conflating the two would lock out everyone not listed."""
    table = RoleTable.load(_roles(tmp_path, OVERRIDES))
    assert table.override_for("stranger@example.com") is None
    assert table.override_for(None) is None
    assert table.override_for("") is None


def test_email_matching_is_case_insensitive(tmp_path: Path) -> None:
    table = RoleTable.load(_roles(tmp_path, OVERRIDES))
    assert table.override_for("LEAD@Example.COM") is Tier.LEAD


def test_an_empty_users_map_is_fine(tmp_path: Path) -> None:
    """No lead is required. Requiring one made sense when this file WAS the
    permission model; it is a break-glass now and is normally empty."""
    assert RoleTable.load(_roles(tmp_path, "users: {}\n")).users == {}


def test_invalid_tier_is_rejected_at_load(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigError, match="not one of"):
        RoleTable.load(_roles(tmp_path, 'users:\n  "a@b.com": superuser\n'))


def test_a_non_mapping_users_block_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RoleConfigError, match="must be a mapping"):
        RoleTable.load(_roles(tmp_path, "users:\n  - a@b.com\n"))


# ---------------------------------------------------------------------------
# hot reload - the reason this stayed a file rather than moving into .env
# ---------------------------------------------------------------------------


def test_an_override_applies_on_the_next_call(tmp_path: Path) -> None:
    """Break-glass is for the moment you are ALREADY locked out. A file edit
    applies on the next request; an env var would need a restart."""
    path = _roles(tmp_path, "users: {}\n")
    store = RoleStore(path)
    assert store.current().override_for("op@example.com") is None

    path.write_text('users:\n  "op@example.com": operator\n', encoding="utf-8")

    assert store.current().override_for("op@example.com") is Tier.OPERATOR


def test_removing_an_override_also_applies_immediately(tmp_path: Path) -> None:
    path = _roles(tmp_path, 'users:\n  "op@example.com": operator\n')
    store = RoleStore(path)
    assert store.current().override_for("op@example.com") is Tier.OPERATOR

    path.write_text("users: {}\n", encoding="utf-8")

    assert store.current().override_for("op@example.com") is None


def test_a_broken_edit_keeps_the_last_good_table(tmp_path: Path) -> None:
    """Never relax to defaults, never crash a live request. The error is
    surfaced by health_check and /healthz instead."""
    path = _roles(tmp_path, OVERRIDES)
    store = RoleStore(path)

    path.write_text("users:\n  - [\n", encoding="utf-8")

    assert store.current().override_for("lead@example.com") is Tier.LEAD
    assert store.last_error is not None


def test_a_bad_edit_followed_by_a_good_one_recovers(tmp_path: Path) -> None:
    path = _roles(tmp_path, OVERRIDES)
    store = RoleStore(path)
    path.write_text("users:\n  - [\n", encoding="utf-8")
    store.current()
    assert store.last_error is not None

    path.write_text('users:\n  "new@example.com": lead\n', encoding="utf-8")

    assert store.current().override_for("new@example.com") is Tier.LEAD
    assert store.last_error is None


# ---------------------------------------------------------------------------
# the registry default
# ---------------------------------------------------------------------------


def test_an_unregistered_tool_fails_closed() -> None:
    spec = spec_for("something_nobody_registered")
    assert spec.required_tier is Tier.LEAD
    assert spec.writes is True


def test_health_check_is_available_at_tier_none() -> None:
    assert spec_for("health_check").required_tier is Tier.NONE
