"""The spending rules, and the pure functions that evaluate them.

There is no policy file, so there is nothing here about parsing, hot-reload,
or a refused edit. What is left is the part that was always the point: given
a snapshot and a proposed change, is it allowed, and exactly where is the
boundary.

The rules are now RELATIVE, which is what let the config file go. Both of
them scale with the account rather than with a number somebody typed:

    per change   the increase may not exceed max_increase_percent
    per day      this user's total increases today may not exceed a
                 percentage of the account's own total daily budget
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from gads_write.auth.tiers import Tier
from gads_write.safety.policy import (
    DAILY_INCREASE_PERCENT_BY_TIER,
    Policy,
    PolicyStore,
    build_policy,
    evaluate_bid_change,
    evaluate_budget_change,
    evaluate_match_type_against_bidding,
    evaluate_operation,
)
from gads_write.settings import Settings

# The account every case below is measured against: 10,000 total daily budget.
# At the fixed tier percentages that makes the ceilings 1,000 for an operator
# and 2,500 for a lead.
ACCOUNT_TOTAL = Decimal(10_000)


def _settings(tmp_path: Path, *, max_increase_percent: int = 100) -> Settings:
    return Settings(
        env="test",
        host="127.0.0.1",
        port=8081,
        base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com",
        oauth_client_secret="s",
        jwt_signing_key="k",
        developer_token="d",
        login_customer_id="9999999999",
        write_enabled=True,
        roles_path=tmp_path / "roles.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
        max_increase_percent=Decimal(max_increase_percent),
        allowed_url_domains=frozenset({"indiraivf.com"}),
    )


def _policy(*, max_increase_percent: int = 100, total=ACCOUNT_TOTAL) -> Policy:
    policy = build_policy(
        max_increase_percent=Decimal(max_increase_percent),
        allowed_final_url_domains=frozenset({"indiraivf.com"}),
    ).for_account(currency_code="INR", timezone="Asia/Kolkata")
    return policy.with_account_budget(total)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def test_the_increase_percent_comes_from_settings(tmp_path: Path) -> None:
    store = PolicyStore(_settings(tmp_path, max_increase_percent=25))
    limits = store.current().limits_for(Tier.OPERATOR)
    assert limits.budget.max_increase_percent == Decimal(25)
    assert limits.bids.max_increase_percent == Decimal(25)


@pytest.mark.parametrize("tier", [Tier.NONE, Tier.READONLY])
def test_non_writing_tiers_are_pinned_to_zero_in_code(tier: Tier) -> None:
    """Not trusted to any table. There is no file to omit their block now,
    but pinning here is what makes the guarantee independent of how
    DAILY_INCREASE_PERCENT_BY_TIER is edited."""
    limits = _policy().limits_for(tier)
    assert limits.budget.max_increase_percent == Decimal(0)
    assert limits.budget.max_total_increase_percent_of_account == Decimal(0)
    assert limits.bids.max_increase_percent == Decimal(0)


def test_a_lead_has_more_daily_headroom_than_an_operator() -> None:
    assert (
        DAILY_INCREASE_PERCENT_BY_TIER[Tier.LEAD]
        > DAILY_INCREASE_PERCENT_BY_TIER[Tier.OPERATOR]
    )


def test_the_store_has_no_error_to_report(tmp_path: Path) -> None:
    """There is no edit left that could be refused, so /healthz can only be
    degraded by roles.yaml now."""
    assert PolicyStore(_settings(tmp_path)).last_error is None


# ---------------------------------------------------------------------------
# per-account stamping
# ---------------------------------------------------------------------------


def test_for_account_stamps_the_accounts_own_currency_and_timezone() -> None:
    bare = build_policy(
        max_increase_percent=Decimal(100), allowed_final_url_domains=frozenset()
    )
    stamped = bare.for_account(currency_code="USD", timezone="America/New_York")

    assert (stamped.currency_code, stamped.timezone) == ("USD", "America/New_York")
    assert bare.currency_code == ""  # the original snapshot is untouched


def test_for_account_keeps_the_previous_value_when_google_returns_nothing() -> None:
    policy = _policy().for_account(currency_code="", timezone="")
    assert (policy.currency_code, policy.timezone) == ("INR", "Asia/Kolkata")


def test_blocked_operation() -> None:
    policy = _policy()
    assert not evaluate_operation(policy, "remove_campaign").allowed
    assert evaluate_operation(policy, "pause_campaign").allowed


# ---------------------------------------------------------------------------
# budget: the per-change percentage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_units, allowed",
    [
        ("2000", True),  # exactly +100%, inclusive
        ("2000.01", False),  # a hair over
        ("1500", True),
    ],
)
def test_increase_percent_boundary(new_units: str, allowed: bool) -> None:
    verdict = evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="1000", new_units=new_units
    )
    assert verdict.allowed is allowed


def test_the_percentage_is_relative_so_it_holds_at_any_scale() -> None:
    """The property that let the absolute caps go: one rule, correct for a
    tiny campaign and a huge one, with nothing to configure."""
    policy = _policy(total=Decimal(10_000_000))
    assert evaluate_budget_change(
        policy, tier=Tier.LEAD, current_units="100", new_units="200"
    ).allowed
    assert evaluate_budget_change(
        policy, tier=Tier.LEAD, current_units="100000", new_units="200000"
    ).allowed
    assert not evaluate_budget_change(
        policy, tier=Tier.LEAD, current_units="100000", new_units="300000"
    ).allowed


def test_there_is_no_absolute_ceiling_on_a_single_budget() -> None:
    """max_daily is gone. A large budget on a large account is ordinary, and
    refusing it was refusing every real campaign this server manages."""
    verdict = evaluate_budget_change(
        _policy(total=Decimal(1_000_000)),
        tier=Tier.LEAD,
        current_units="50000",
        new_units="60000",
    )
    assert verdict.allowed


def test_a_decrease_is_never_blocked() -> None:
    """There is no minimum any more. Lowering a budget is the one change that
    can only ever reduce spend, so refusing it protected nothing."""
    for new_units in ("1", "0.01", "500"):
        verdict = evaluate_budget_change(
            _policy(), tier=Tier.OPERATOR, current_units="1000", new_units=new_units
        )
        assert verdict.allowed, new_units


def test_zero_is_still_refused() -> None:
    assert not evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="1000", new_units="0"
    ).allowed


def test_raising_from_zero_is_refused_rather_than_treated_as_zero_percent() -> None:
    verdict = evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="0", new_units="100"
    )
    assert not verdict.allowed
    assert "undefined" in verdict.describe()


# ---------------------------------------------------------------------------
# budget: the daily ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "already_today, allowed",
    [
        ("0", True),
        ("900", True),  # 900 + 100 = 1000, exactly the operator ceiling
        ("900.01", False),
    ],
)
def test_daily_ceiling_boundary(already_today: str, allowed: bool) -> None:
    verdict = evaluate_budget_change(
        _policy(),
        tier=Tier.OPERATOR,
        current_units="1000",
        new_units="1100",  # +100
        already_increased_today_units=already_today,
    )
    assert verdict.allowed is allowed


def test_the_ceiling_scales_with_the_account() -> None:
    """The whole point of expressing it as a percentage: the same rule is
    right for a small account and a large one, with nothing to update."""
    small = evaluate_budget_change(
        _policy(total=Decimal(1_000)),
        tier=Tier.OPERATOR,
        current_units="1000",
        new_units="1200",  # +200, over 10% of 1,000
    )
    large = evaluate_budget_change(
        _policy(total=Decimal(100_000)),
        tier=Tier.OPERATOR,
        current_units="1000",
        new_units="1200",  # +200, well under 10% of 100,000
    )
    assert not small.allowed
    assert large.allowed


def test_the_ceiling_counts_the_proposed_change_too() -> None:
    """Otherwise the last change of the day is always free."""
    verdict = evaluate_budget_change(
        _policy(),
        tier=Tier.OPERATOR,
        current_units="1000",
        new_units="1600",  # +600
        already_increased_today_units="600",  # 1,200 total, over 1,000
    )
    assert not verdict.allowed


def test_a_lead_has_a_higher_ceiling_than_an_operator() -> None:
    # +1,500: over the operator's 1,000 ceiling, under the lead's 2,500.
    change = dict(current_units="2000", new_units="3500", already_increased_today_units="0")
    assert not evaluate_budget_change(_policy(), tier=Tier.OPERATOR, **change).allowed
    assert evaluate_budget_change(_policy(), tier=Tier.LEAD, **change).allowed


def test_an_unknown_account_total_refuses_rather_than_skipping_the_ceiling() -> None:
    """`None` means the read failed. Skipping the rule would silently turn a
    Google hiccup into an unbounded change."""
    policy = _policy().with_account_budget(None)
    verdict = evaluate_budget_change(
        policy, tier=Tier.OPERATOR, current_units="1000", new_units="1100"
    )
    assert not verdict.allowed
    assert "could not be established" in verdict.describe()


def test_an_unknown_account_total_still_permits_a_decrease() -> None:
    """A decrease returns before the ceiling is consulted, so a failed read
    never blocks the one change that can only reduce spend."""
    policy = _policy().with_account_budget(None)
    assert evaluate_budget_change(
        policy, tier=Tier.OPERATOR, current_units="1000", new_units="500"
    ).allowed


def test_readonly_tier_is_refused_everything() -> None:
    verdict = evaluate_budget_change(
        _policy(), tier=Tier.READONLY, current_units="1000", new_units="1001"
    )
    assert not verdict.allowed


# ---------------------------------------------------------------------------
# bids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_units, allowed",
    [("100", True), ("200", True), ("200.01", False)],
)
def test_bid_increase_percent_boundary(new_units: str, allowed: bool) -> None:
    verdict = evaluate_bid_change(
        _policy(), tier=Tier.LEAD, current_units="100", new_units=new_units
    )
    assert verdict.allowed is allowed


def test_there_is_no_absolute_max_cpc() -> None:
    """Removed for the same reason as max_daily: the right number is entirely
    account-specific, so any fixed one is wrong somewhere."""
    assert evaluate_bid_change(
        _policy(), tier=Tier.LEAD, current_units="5000", new_units="6000"
    ).allowed


def test_lowering_a_bid_is_always_allowed() -> None:
    assert evaluate_bid_change(
        _policy(), tier=Tier.LEAD, current_units="100", new_units="1"
    ).allowed


def test_a_bid_of_zero_is_refused() -> None:
    assert not evaluate_bid_change(
        _policy(), tier=Tier.LEAD, current_units="100", new_units="0"
    ).allowed


def test_raising_a_bid_from_zero_is_refused() -> None:
    verdict = evaluate_bid_change(
        _policy(), tier=Tier.LEAD, current_units="0", new_units="50"
    )
    assert not verdict.allowed
    assert "undefined" in verdict.describe()


# ---------------------------------------------------------------------------
# structural rules
# ---------------------------------------------------------------------------


def test_broad_match_blocked_on_manual_cpc() -> None:
    verdict = evaluate_match_type_against_bidding(
        _policy(), match_type="BROAD", bidding_strategy="MANUAL_CPC"
    )
    assert not verdict.allowed


@pytest.mark.parametrize(
    "match_type, strategy",
    [("PHRASE", "MANUAL_CPC"), ("EXACT", "MANUAL_CPC"), ("BROAD", "MAXIMIZE_CONVERSIONS")],
)
def test_other_combinations_are_permitted(match_type: str, strategy: str) -> None:
    assert evaluate_match_type_against_bidding(
        _policy(), match_type=match_type, bidding_strategy=strategy
    ).allowed


def test_the_structural_rules_are_fixed_rather_than_configurable() -> None:
    """These used to be settings. A setting nobody should ever change is not
    a setting - and "new keywords start ENABLED" is not a thing anyone wants."""
    rules = _policy().rules
    assert rules.new_entities_start_paused is True
    assert rules.block_broad_match_with_manual_cpc is True
