"""The spending rules, and the pure functions that evaluate them.

The design principle: a person should be able to do here what they could
already do in the Google Ads UI. The UI has no guardrails - the control there
is the human. Here the human is still the control, but sees a preview first
and must accept it.

So there is very little left to refuse, and these tests are mostly about
proving that ordinary work is NOT blocked:

    per change   an increase past `max_increase_percent`, which is a typo
                 backstop set far above normal work - not an operating limit
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from gads_write.auth.tiers import Tier
from gads_write.safety.policy import (
    Policy,
    PolicyStore,
    build_policy,
    evaluate_bid_change,
    evaluate_budget_change,
    broad_match_warning,
    evaluate_operation,
)
from gads_write.settings import Settings

def _settings(tmp_path: Path, *, max_increase_percent: int = 1000) -> Settings:
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


def _policy(*, max_increase_percent: int = 1000) -> Policy:
    return build_policy(
        max_increase_percent=Decimal(max_increase_percent),
        allowed_final_url_domains=frozenset({"indiraivf.com"}),
    ).for_account(currency_code="INR", timezone="Asia/Kolkata")


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
    """Not trusted to any table or setting. A zero backstop refuses every
    increase, which is what `none` and `readonly` mean. Pinning it here makes
    the guarantee independent of GADS_MAX_INCREASE_PERCENT."""
    limits = _policy().limits_for(tier)
    assert limits.budget.max_increase_percent == Decimal(0)
    assert limits.bids.max_increase_percent == Decimal(0)


def test_the_store_exposes_no_reload_error(tmp_path: Path) -> None:
    """There is no edit left that could be refused, so a `last_error` that
    could only ever be None would be a standing invitation to believe this
    still reloads. /healthz reports roles.yaml instead, which genuinely can
    fail that way."""
    assert not hasattr(PolicyStore(_settings(tmp_path)), "last_error")


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
# budget: the typo backstop, and everything it deliberately does NOT block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_units, allowed",
    [
        ("11000", True),   # exactly +1000%, inclusive
        ("11000.01", False),
        ("5000", True),
    ],
)
def test_increase_percent_boundary(new_units: str, allowed: bool) -> None:
    verdict = evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="1000", new_units=new_units
    )
    assert verdict.allowed is allowed


def test_ordinary_work_is_not_blocked() -> None:
    """The whole point of raising the backstop.

    A campaign sitting at 500 from a test, raised to 5,000 for a festive
    push, is a ten-second job in the Google Ads UI. Under the old 100% cap it
    took four days. It must go through in one step.
    """
    assert evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="500", new_units="5000"
    ).allowed


def test_a_stray_digit_is_still_caught() -> None:
    """What the backstop is actually for.

    The server never sees what the person ASKED for - a tool call carries a
    number, never the conversation - so magnitude is all it can judge. 5,000
    and 500,000 look alike to someone approving in a hurry.
    """
    verdict = evaluate_budget_change(
        _policy(), tier=Tier.OPERATOR, current_units="500", new_units="500000"
    )
    assert not verdict.allowed
    assert "backstop" in verdict.describe()


def test_a_large_budget_on_a_large_account_is_ordinary() -> None:
    assert evaluate_budget_change(
        _policy(), tier=Tier.LEAD, current_units="50000", new_units="60000"
    ).allowed


def test_the_backstop_is_relative_so_it_holds_at_any_scale() -> None:
    for current, new in (("100", "1000"), ("100000", "1000000")):
        assert evaluate_budget_change(
            _policy(), tier=Tier.LEAD, current_units=current, new_units=new
        ).allowed


def test_there_is_no_daily_ceiling_any_more() -> None:
    """It blocked ordinary work: raising five campaigns for a seasonal push
    stopped after the second. The running total is shown on the preview
    instead, and the person approving decides."""
    for _ in range(10):
        assert evaluate_budget_change(
            _policy(), tier=Tier.OPERATOR, current_units="1000", new_units="2000"
        ).allowed


def test_a_decrease_is_never_blocked() -> None:
    """There is no minimum. Lowering a budget is the one change that can only
    ever reduce spend, so refusing it protected nothing."""
    for new_units in ("1", "0.01", "500"):
        assert evaluate_budget_change(
            _policy(), tier=Tier.OPERATOR, current_units="1000", new_units=new_units
        ).allowed, new_units


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


def test_readonly_tier_is_refused_everything() -> None:
    assert not evaluate_budget_change(
        _policy(), tier=Tier.READONLY, current_units="1000", new_units="1001"
    ).allowed


# ---------------------------------------------------------------------------
# bids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "new_units, allowed",
    [("100", True), ("1100", True), ("1100.01", False)],
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


def test_broad_match_on_manual_cpc_warns_rather_than_refusing() -> None:
    """The Google Ads UI permits this, so this server does too.

    It used to be refused outright. Refusing something the UI allows is the
    kind of block that makes people work around the tool; saying plainly why
    it is risky and letting them decide is the position the UI leaves them
    in, with more information rather than less.
    """
    warning = broad_match_warning(
        match_type="BROAD", bidding_strategy="MANUAL_CPC"
    )
    assert warning is not None
    assert "broad match" in warning.lower()


@pytest.mark.parametrize(
    "match_type, strategy",
    [
        ("PHRASE", "MANUAL_CPC"),
        ("EXACT", "MANUAL_CPC"),
        ("BROAD", "MAXIMIZE_CONVERSIONS"),
    ],
)
def test_no_warning_for_other_combinations(match_type: str, strategy: str) -> None:
    assert broad_match_warning(
        match_type=match_type, bidding_strategy=strategy
    ) is None


def test_an_unreadable_bidding_strategy_produces_no_warning() -> None:
    """Failing to establish an ADVISORY fact is not a reason to block a change
    the UI would have allowed."""
    assert broad_match_warning(match_type="BROAD", bidding_strategy="") is None


def test_the_structural_rules_are_fixed_rather_than_configurable() -> None:
    """This used to be a setting. A setting nobody should ever change is not
    a setting - and "new keywords start ENABLED" is not a thing anyone wants."""
    assert _policy().rules.new_entities_start_paused is True


# ---------------------------------------------------------------------------
# the backstop switched off
# ---------------------------------------------------------------------------


def _uncapped() -> Policy:
    return build_policy(
        max_increase_percent=None, allowed_final_url_domains=frozenset()
    ).for_account(currency_code="INR", timezone="Asia/Kolkata")


def test_no_backstop_means_any_increase_is_allowed() -> None:
    """Deliberate: the server cannot see what was ASKED for, only the number
    that arrived, so it judges magnitude and never intent. Where the client
    prompts on write tools it shows the actual figure, which catches a wrong
    number of any size - and this rule then adds nothing."""
    assert evaluate_budget_change(
        _uncapped(), tier=Tier.LEAD, current_units="100", new_units="1000000"
    ).allowed
    assert evaluate_bid_change(
        _uncapped(), tier=Tier.LEAD, current_units="10", new_units="99999"
    ).allowed


def test_switching_it_off_does_not_unpin_the_non_writing_tiers() -> None:
    """`none` and `readonly` are pinned to zero in code, independent of the
    setting. Turning the backstop off must not promote anybody."""
    for tier in (Tier.NONE, Tier.READONLY):
        assert not evaluate_budget_change(
            _uncapped(), tier=tier, current_units="100", new_units="101"
        ).allowed, tier


def test_the_rules_that_are_not_the_backstop_still_hold() -> None:
    """Zero, negatives and rises from zero are refused because they are
    incoherent, not because of a magnitude limit."""
    uncapped = _uncapped()
    assert not evaluate_budget_change(
        uncapped, tier=Tier.LEAD, current_units="100", new_units="0"
    ).allowed
    assert not evaluate_budget_change(
        uncapped, tier=Tier.LEAD, current_units="0", new_units="100"
    ).allowed
    assert not evaluate_bid_change(
        uncapped, tier=Tier.LEAD, current_units="10", new_units="0"
    ).allowed
