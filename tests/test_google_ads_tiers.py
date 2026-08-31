"""Tiers derived from Google Ads access roles.

The security properties under test, in order of how expensive getting them
wrong would be:

  1. An unrecognised or future role never maps to something permissive.
  2. A failed lookup is a TierLookupError, never Tier.NONE and never a guess.
  3. Access on an account we do not manage does not light up write tools.
  4. A demotion takes effect without the user reconnecting.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gads_write.ads.reads import AdsReadError
from gads_write.auth.google_ads_roles import (
    GoogleAdsTierResolver,
    OverridingTierResolver,
    TierCache,
    tier_for_access_role,
)
from gads_write.auth.roles import RoleStore
from gads_write.auth.tiers import Tier, TierLookupError


@dataclass(frozen=True)
class FakeCaller:
    email: str


class FakeReader:
    """An AdsReader that answers from a dict instead of Google."""

    def __init__(self, roles: dict[tuple[str, str], str] | None = None) -> None:
        # (customer_id, email) -> access role name
        self.roles = roles or {}
        self.explode_on: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    async def access_role(self, *, customer_id: str, email: str) -> str | None:
        self.calls.append((customer_id, email))
        if customer_id in self.explode_on:
            raise AdsReadError(f"PERMISSION_DENIED reading {customer_id}")
        return self.roles.get((customer_id, email.lower()))

    async def accessible_customer_ids(self):  # pragma: no cover - unused here
        return ()

    async def account_summary(self, customer_id):  # pragma: no cover
        return None

    async def campaign_performance(self, **kwargs):  # pragma: no cover
        return ()

    async def search_terms(self, **kwargs):  # pragma: no cover
        return ()


MCC = "9999999999"
ACCOUNT = "1111111111"
OTHER = "2222222222"


def _resolver(
    reader: FakeReader,
    *,
    allowed: set[str] | None = None,
    ttl: float = 0,
) -> GoogleAdsTierResolver:
    return GoogleAdsTierResolver(
        reader=reader,
        login_customer_id=MCC,
        allowed_customer_ids=lambda: frozenset(allowed or {ACCOUNT}),
        cache=TierCache(ttl),
    )


# ---------------------------------------------------------------------------
# the role mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("ADMIN", Tier.LEAD),
        ("STANDARD", Tier.OPERATOR),
        ("READ_ONLY", Tier.READONLY),
        ("EMAIL_ONLY", Tier.NONE),
        ("UNSPECIFIED", Tier.NONE),
        ("UNKNOWN", Tier.NONE),
        (None, Tier.NONE),
    ],
)
def test_access_roles_map_to_tiers(role, expected) -> None:
    assert tier_for_access_role(role) is expected


def test_a_role_google_invents_later_is_not_permissive() -> None:
    """UNKNOWN means "a value newer than this client". A role we have never
    reviewed must never arrive already trusted."""
    assert tier_for_access_role("SUPER_ADMIN_PLUS") is Tier.NONE
    assert tier_for_access_role("BILLING_ONLY") is Tier.NONE


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

async def test_a_direct_grant_on_the_account_is_used() -> None:
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    tier = await _resolver(reader).resolve(FakeCaller("a@b.com"), ACCOUNT)
    assert tier is Tier.OPERATOR


async def test_a_role_inherited_from_the_manager_account_is_used() -> None:
    """The common case for a real team member.

    People are usually added once on the MCC and have no row at all on the
    child accounts they work in. Asking only the child would see "no row"
    and lock out the entire team.
    """
    reader = FakeReader({(MCC, "a@b.com"): "ADMIN"})
    tier = await _resolver(reader).resolve(FakeCaller("a@b.com"), ACCOUNT)
    assert tier is Tier.LEAD
    assert reader.calls == [(ACCOUNT, "a@b.com"), (MCC, "a@b.com")]


async def test_a_direct_grant_wins_over_the_inherited_one() -> None:
    reader = FakeReader(
        {(ACCOUNT, "a@b.com"): "READ_ONLY", (MCC, "a@b.com"): "ADMIN"}
    )
    tier = await _resolver(reader).resolve(FakeCaller("a@b.com"), ACCOUNT)
    assert tier is Tier.READONLY
    # The manager was never consulted: the specific answer settled it.
    assert reader.calls == [(ACCOUNT, "a@b.com")]


async def test_no_row_anywhere_is_a_definite_none() -> None:
    reader = FakeReader({})
    tier = await _resolver(reader).resolve(FakeCaller("stranger@b.com"), ACCOUNT)
    assert tier is Tier.NONE


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------

async def test_an_unreadable_access_table_raises_rather_than_denying() -> None:
    """The distinction the whole design rests on.

    Tier.NONE would be indistinguishable from a genuine refusal in the audit
    log, and anything permissive would make causing an outage an escalation
    path.
    """
    reader = FakeReader({})
    reader.explode_on = {ACCOUNT}

    with pytest.raises(TierLookupError):
        await _resolver(reader).resolve(FakeCaller("a@b.com"), ACCOUNT)


async def test_the_lookup_failure_names_the_fallback() -> None:
    """This message is how the open question in CLAUDE.md gets answered.

    If non-admins cannot read their own access row, every non-admin lands
    here, and whoever reads the log needs to know what to do next.
    """
    reader = FakeReader({})
    reader.explode_on = {ACCOUNT}

    with pytest.raises(TierLookupError) as caught:
        await _resolver(reader).resolve(FakeCaller("a@b.com"), ACCOUNT)

    message = str(caught.value)
    assert "read-only service credential" in message
    assert "customer_user_access is readable only by admins" in message


async def test_a_caller_with_no_email_is_none_not_an_outage() -> None:
    tier = await _resolver(FakeReader({})).resolve(FakeCaller(""), ACCOUNT)
    assert tier is Tier.NONE


# ---------------------------------------------------------------------------
# visible_tier - the menu
# ---------------------------------------------------------------------------

async def test_visible_tier_ignores_accounts_we_do_not_manage() -> None:
    """A real control, not tidiness.

    Being ADMIN on a personal Google Ads account must not light up every
    `lead` tool on this server.
    """
    reader = FakeReader(
        {(OTHER, "a@b.com"): "ADMIN", (ACCOUNT, "a@b.com"): "READ_ONLY"}
    )
    resolver = _resolver(reader, allowed={ACCOUNT})
    assert await resolver.visible_tier(FakeCaller("a@b.com")) is Tier.READONLY


async def test_visible_tier_is_the_highest_across_managed_accounts() -> None:
    reader = FakeReader(
        {(ACCOUNT, "a@b.com"): "READ_ONLY", (OTHER, "a@b.com"): "STANDARD"}
    )
    resolver = _resolver(reader, allowed={ACCOUNT, OTHER})
    assert await resolver.visible_tier(FakeCaller("a@b.com")) is Tier.OPERATOR


async def test_an_empty_allowlist_grants_nothing() -> None:
    resolver = _resolver(FakeReader({}), allowed=set())
    assert await resolver.visible_tier(FakeCaller("a@b.com")) is Tier.NONE


async def test_visible_tier_survives_a_partial_outage() -> None:
    """One unreachable account must not blank the menu, but it must not
    invent access either - the result is the highest of what we could read."""
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    reader.explode_on = {OTHER}
    resolver = _resolver(reader, allowed={ACCOUNT, OTHER})
    assert await resolver.visible_tier(FakeCaller("a@b.com")) is Tier.OPERATOR


async def test_visible_tier_raises_when_every_account_fails() -> None:
    reader = FakeReader({})
    reader.explode_on = {ACCOUNT, OTHER}
    resolver = _resolver(reader, allowed={ACCOUNT, OTHER})
    with pytest.raises(TierLookupError):
        await resolver.visible_tier(FakeCaller("a@b.com"))


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------

class StepClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_a_cached_tier_avoids_a_second_lookup() -> None:
    clock = StepClock()
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    resolver = GoogleAdsTierResolver(
        reader=reader,
        login_customer_id=MCC,
        allowed_customer_ids=lambda: frozenset({ACCOUNT}),
        cache=TierCache(60, clock=clock),
    )

    await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT)
    await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT)
    assert len(reader.calls) == 1


async def test_a_demotion_lands_once_the_ttl_expires() -> None:
    """The bounded weakening of "resolve on every call".

    No reconnect, no restart - the change bites within the TTL.
    """
    clock = StepClock()
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    resolver = GoogleAdsTierResolver(
        reader=reader,
        login_customer_id=MCC,
        allowed_customer_ids=lambda: frozenset({ACCOUNT}),
        cache=TierCache(60, clock=clock),
    )

    assert await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT) is Tier.OPERATOR

    reader.roles[(ACCOUNT, "a@b.com")] = "READ_ONLY"  # demoted in the Ads UI
    clock.advance(61)

    assert await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT) is Tier.READONLY


async def test_a_zero_ttl_disables_caching_entirely() -> None:
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    resolver = _resolver(reader, ttl=0)

    await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT)
    await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT)
    assert len(reader.calls) == 2


async def test_an_outage_is_never_cached() -> None:
    """A sticky failure would turn a blip into a long refusal, and would
    remember "we do not know" as though it were an answer."""
    clock = StepClock()
    reader = FakeReader({})
    reader.explode_on = {ACCOUNT, MCC}
    resolver = GoogleAdsTierResolver(
        reader=reader,
        login_customer_id=MCC,
        allowed_customer_ids=lambda: frozenset({ACCOUNT}),
        cache=TierCache(60, clock=clock),
    )

    with pytest.raises(TierLookupError):
        await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT)

    reader.explode_on = set()
    reader.roles[(ACCOUNT, "a@b.com")] = "ADMIN"

    # Recovers on the very next call, with no TTL to wait out.
    assert await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT) is Tier.LEAD


# ---------------------------------------------------------------------------
# break-glass
# ---------------------------------------------------------------------------

def _google_ads_roles(write_roles, users: dict) -> RoleStore:
    return RoleStore(write_roles({"mode": "google_ads", "users": users}))


async def test_no_overrides_means_google_decides(write_roles) -> None:
    store = _google_ads_roles(write_roles, {})
    reader = FakeReader({(ACCOUNT, "a@b.com"): "STANDARD"})
    resolver = OverridingTierResolver(overrides=store, primary=_resolver(reader))

    assert await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT) is Tier.OPERATOR


async def test_an_override_wins_when_google_cannot_answer(write_roles) -> None:
    """The escape hatch if the open question resolves badly.

    A deliberate, in-git file edit - not an automatic fallback on error.
    """
    store = _google_ads_roles(write_roles, {"a@b.com": "operator"})
    reader = FakeReader({})
    reader.explode_on = {ACCOUNT, MCC}
    resolver = OverridingTierResolver(overrides=store, primary=_resolver(reader))

    assert await resolver.resolve(FakeCaller("a@b.com"), ACCOUNT) is Tier.OPERATOR
    # Google was never asked: the override short-circuits it.
    assert reader.calls == []


async def test_an_override_does_not_apply_to_other_people(write_roles) -> None:
    store = _google_ads_roles(write_roles, {"a@b.com": "lead"})
    reader = FakeReader({(ACCOUNT, "other@b.com"): "READ_ONLY"})
    resolver = OverridingTierResolver(overrides=store, primary=_resolver(reader))

    assert await resolver.resolve(FakeCaller("other@b.com"), ACCOUNT) is Tier.READONLY


async def test_the_source_string_makes_break_glass_visible(write_roles) -> None:
    """health_check has to show that someone is on an override."""
    store = _google_ads_roles(write_roles, {"a@b.com": "lead"})
    resolver = OverridingTierResolver(
        overrides=store, primary=_resolver(FakeReader({}))
    )
    assert "break-glass" in resolver.source
