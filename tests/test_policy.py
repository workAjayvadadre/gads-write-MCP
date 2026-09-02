"""Policy parsing, per-tier merge, hot reload, and every rule at its boundary.

Boundary convention under test: limits are INCLUSIVE. Exactly at the limit
is allowed; a hair over is not. Each rule is checked just-under, exactly-at,
and just-over, because "<=" vs "<" is the single most likely bug in this file
and it is invisible on inspection.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from gads_write.auth.roles import Tier
from gads_write.safety.policy import (
    PolicyError,
    PolicyStore,
    evaluate_bid_change,
    evaluate_budget_change,
    evaluate_customer,
    evaluate_match_type_against_bidding,
    evaluate_operation,
    load_policy_file,
)


# ---------------------------------------------------------------------------
# parsing and tier merge
# ---------------------------------------------------------------------------

def test_tier_override_merges_key_by_key(write_policy) -> None:
    policy = load_policy_file(write_policy())

    operator = policy.limits_for(Tier.OPERATOR).budget
    lead = policy.limits_for(Tier.LEAD).budget

    # operator overrides max_daily but not min_daily, which comes from defaults
    assert operator.max_daily_units == Decimal(2000)
    assert operator.min_daily_units == Decimal(50)
    # lead has an empty override block, so it is pure defaults
    assert lead.max_daily_units == Decimal(5000)


def test_tier_none_gets_zeros_even_if_config_omits_it(write_policy) -> None:
    # policy.yaml has no `none` block. It must not inherit the defaults.
    policy = load_policy_file(write_policy())
    limits = policy.limits_for(Tier.NONE)
    assert limits.budget.max_daily_units == Decimal(0)
    assert limits.bids.max_cpc_units == Decimal(0)


def test_the_shipped_policy_file_actually_loads() -> None:
    """Regression guard: the real config/policy.yaml must parse.

    Every other test in this file builds its own policy, which means they
    all passed while the shipped file was unloadable. Without this test the
    first sign of that would have been the server refusing to boot on deploy.
    """
    shipped = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"
    policy = load_policy_file(shipped)
    assert policy.version == 2
    assert policy.allowed_customer_ids
    # and the non-writing tiers are pinned to zero regardless of the file
    assert policy.limits_for(Tier.READONLY).budget.max_daily_units == Decimal(0)
    assert policy.limits_for(Tier.NONE).budget.max_daily_units == Decimal(0)


def test_unknown_version_is_refused(write_policy) -> None:
    with pytest.raises(PolicyError, match="version"):
        load_policy_file(write_policy({"version": 99}))


def test_empty_allowlist_is_refused(write_policy) -> None:
    with pytest.raises(PolicyError, match="no wildcard"):
        load_policy_file(write_policy({"allowed_customer_ids": []}))


def test_unknown_tier_name_is_refused(write_policy) -> None:
    # A typo like `oprator:` must break loudly, not silently do nothing.
    with pytest.raises(PolicyError, match="not a known tier"):
        load_policy_file(
            write_policy({"limits": {"tiers": {"oprator": {"budget": {}}}}})
        )


def test_min_above_max_is_refused(write_policy) -> None:
    with pytest.raises(PolicyError, match="would block every change"):
        load_policy_file(
            write_policy({"limits": {"defaults": {"budget": {"min_daily": 9000}}}})
        )


# ---------------------------------------------------------------------------
# account allowlist
# ---------------------------------------------------------------------------

def test_account_allowlist(write_policy) -> None:
    policy = load_policy_file(write_policy())
    assert evaluate_customer(policy, "1234567890").allowed
    verdict = evaluate_customer(policy, "9999999999")
    assert not verdict.allowed
    assert "not on the allowlist" in verdict.describe()


def test_blocked_operation(write_policy) -> None:
    policy = load_policy_file(write_policy())
    assert not evaluate_operation(policy, "remove_campaign").allowed
    assert evaluate_operation(policy, "pause_campaign").allowed


# ---------------------------------------------------------------------------
# budget: max daily, at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("new_units", "allowed"),
    [("1999.99", True), ("2000", True), ("2000.01", False)],
)
def test_operator_max_daily_boundary(write_policy, new_units: str, allowed: bool) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy, tier=Tier.OPERATOR, current_units=1900, new_units=new_units
    )
    assert verdict.allowed is allowed


@pytest.mark.parametrize(
    ("new_units", "allowed"),
    [("50", True), ("49.99", False)],
)
def test_min_daily_boundary(write_policy, new_units: str, allowed: bool) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy, tier=Tier.LEAD, current_units=100, new_units=new_units
    )
    assert verdict.allowed is allowed


# ---------------------------------------------------------------------------
# budget: increase percent, at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("new_units", "allowed"),
    [
        ("1199", True),    # 19.9% - under the operator's 20%
        ("1200", True),    # exactly 20%
        ("1200.01", False),  # a hair over
    ],
)
def test_operator_increase_percent_boundary(
    write_policy, new_units: str, allowed: bool
) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy, tier=Tier.OPERATOR, current_units=1000, new_units=new_units
    )
    assert verdict.allowed is allowed


def test_decrease_is_never_blocked_by_the_increase_limit(write_policy) -> None:
    # Lowering a budget by 90% is a large change but not a risky one.
    policy = load_policy_file(write_policy())
    assert evaluate_budget_change(
        policy, tier=Tier.OPERATOR, current_units=1000, new_units=100
    ).allowed


def test_raising_from_zero_is_refused_rather_than_treated_as_zero_percent(
    write_policy,
) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy, tier=Tier.LEAD, current_units=0, new_units=500
    )
    assert not verdict.allowed
    assert "undefined" in verdict.describe()


# ---------------------------------------------------------------------------
# budget: per-user daily ceiling, at the boundary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("already_today", "allowed"),
    [
        ("2799", True),   # +200 lands at 2999, under the operator's 3000
        ("2800", True),   # +200 lands exactly on 3000
        ("2800.01", False),  # a hair over
    ],
)
def test_daily_ceiling_boundary(write_policy, already_today: str, allowed: bool) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy,
        tier=Tier.OPERATOR,
        current_units=1000,
        new_units=1200,  # a +200 increase, within the 20% per-change limit
        already_increased_today_units=already_today,
    )
    assert verdict.allowed is allowed


def test_daily_ceiling_counts_the_proposed_change_too(write_policy) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_budget_change(
        policy,
        tier=Tier.OPERATOR,
        current_units=1000,
        new_units=1200,
        already_increased_today_units=2900,
    )
    assert not verdict.allowed
    assert "daily ceiling" in verdict.describe()


def test_readonly_tier_is_refused_everything(write_policy) -> None:
    policy = load_policy_file(write_policy())
    assert not evaluate_budget_change(
        policy, tier=Tier.READONLY, current_units=100, new_units=101
    ).allowed
    assert not evaluate_budget_change(
        policy, tier=Tier.NONE, current_units=100, new_units=101
    ).allowed


# ---------------------------------------------------------------------------
# bids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("new_units", "allowed"),
    [("99.99", True), ("100", True), ("100.01", False)],
)
def test_operator_max_cpc_boundary(write_policy, new_units: str, allowed: bool) -> None:
    policy = load_policy_file(write_policy())
    verdict = evaluate_bid_change(
        policy, tier=Tier.OPERATOR, current_units=90, new_units=new_units
    )
    assert verdict.allowed is allowed


@pytest.mark.parametrize(
    ("new_units", "allowed"),
    [("12.9", True), ("13", True), ("13.01", False)],
)
def test_bid_increase_percent_boundary(
    write_policy, new_units: str, allowed: bool
) -> None:
    # default max_increase_percent is 30; from 10 that allows up to 13.
    policy = load_policy_file(write_policy())
    verdict = evaluate_bid_change(
        policy, tier=Tier.LEAD, current_units=10, new_units=new_units
    )
    assert verdict.allowed is allowed


# ---------------------------------------------------------------------------
# broad match under manual CPC
# ---------------------------------------------------------------------------

def test_broad_match_blocked_on_manual_cpc(write_policy) -> None:
    policy = load_policy_file(write_policy())
    assert not evaluate_match_type_against_bidding(
        policy, match_type="BROAD", bidding_strategy="MANUAL_CPC"
    ).allowed
    assert evaluate_match_type_against_bidding(
        policy, match_type="PHRASE", bidding_strategy="MANUAL_CPC"
    ).allowed
    assert evaluate_match_type_against_bidding(
        policy, match_type="BROAD", bidding_strategy="MAXIMIZE_CONVERSIONS"
    ).allowed


def test_broad_match_rule_can_be_turned_off_in_config(write_policy) -> None:
    policy = load_policy_file(
        write_policy({"rules": {"block_broad_match_with_manual_cpc": False}})
    )
    assert evaluate_match_type_against_bidding(
        policy, match_type="BROAD", bidding_strategy="MANUAL_CPC"
    ).allowed


# ---------------------------------------------------------------------------
# hot reload
# ---------------------------------------------------------------------------

def test_valid_edit_takes_effect_without_restart(write_policy, tmp_path: Path) -> None:
    path = write_policy()
    store = PolicyStore(path)
    assert store.current().limits_for(Tier.LEAD).budget.max_daily_units == Decimal(5000)

    write_policy({"limits": {"defaults": {"budget": {"max_daily": 7000}}}})
    assert store.current().limits_for(Tier.LEAD).budget.max_daily_units == Decimal(7000)
    assert store.reload_count == 1
    assert store.last_error is None


def test_invalid_edit_keeps_the_last_good_policy(write_policy) -> None:
    # The important property: a broken edit must not relax limits, and must
    # not crash a request that is mid-flight.
    path = write_policy()
    store = PolicyStore(path)
    assert store.current().limits_for(Tier.LEAD).budget.max_daily_units == Decimal(5000)

    path.write_text("this: is: not: valid: yaml:\n  - [", encoding="utf-8")

    still = store.current()
    assert still.limits_for(Tier.LEAD).budget.max_daily_units == Decimal(5000)
    assert store.last_error is not None
    assert store.reload_count == 0


def test_structurally_invalid_edit_is_also_refused(write_policy) -> None:
    # Parses as YAML, but the allowlist is now empty. Must not be adopted.
    path = write_policy()
    store = PolicyStore(path)
    write_policy({"allowed_customer_ids": []})

    assert store.current().allows_customer("1234567890")
    assert store.last_error is not None


@pytest.mark.parametrize("broken_block", [None, 5, "daily"])
def test_a_mis_shaped_tier_block_is_refused_rather_than_crashing(
    write_policy, broken_block
) -> None:
    """Valid YAML, wrong shape.

    `budget:` with nothing under it parses to None, and the tier merge
    overwrites the defaults dict with it. The parser must name the problem
    as a PolicyError like every other bad edit - anything else escapes the
    store's safety net and reaches a live request.
    """
    with pytest.raises(PolicyError) as caught:
        load_policy_file(
            write_policy({"limits": {"tiers": {"operator": {"budget": broken_block}}}})
        )
    assert "budget" in str(caught.value)


def test_a_mis_shaped_tier_block_keeps_the_last_good_policy(write_policy) -> None:
    """The same edit, arriving as a hot reload rather than at boot."""
    path = write_policy()
    store = PolicyStore(path)
    assert store.current().limits_for(Tier.OPERATOR).budget.max_daily_units == Decimal(2000)

    write_policy({"limits": {"tiers": {"operator": {"budget": None}}}})

    still = store.current()
    assert still.limits_for(Tier.OPERATOR).budget.max_daily_units == Decimal(2000)
    assert store.last_error is not None
    assert store.reload_count == 0


def test_an_unexpected_reload_failure_still_keeps_the_last_good_policy(
    write_policy, monkeypatch
) -> None:
    """The safety net, independent of any particular bad edit.

    PolicyStore used to catch only PolicyError, so a parser bug that raised
    anything else - a TypeError on a mis-shaped block, say - escaped into
    the caller's request AND left last_error unset, so /healthz went on
    reporting a healthy server. Whatever goes wrong, the rule is the same:
    keep the last good policy, record it, never raise into a request.
    """
    import gads_write.safety.policy as policy_module

    path = write_policy()
    store = PolicyStore(path)
    assert store.current().limits_for(Tier.LEAD).budget.max_daily_units == Decimal(5000)

    def explode(_path):
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(policy_module, "load_policy_file", explode)
    write_policy({"limits": {"defaults": {"budget": {"max_daily": 9999}}}})

    still = store.current()
    assert still.limits_for(Tier.LEAD).budget.max_daily_units == Decimal(5000)
    assert store.last_error is not None
    assert "something nobody anticipated" in store.last_error
    assert store.reload_count == 0


def test_a_bad_edit_followed_by_a_good_one_recovers(write_policy) -> None:
    path = write_policy()
    store = PolicyStore(path)

    path.write_text("garbage: [", encoding="utf-8")
    store.current()
    assert store.last_error is not None

    write_policy({"limits": {"defaults": {"budget": {"max_daily": 6000}}}})
    assert store.current().limits_for(Tier.LEAD).budget.max_daily_units == Decimal(6000)
    assert store.last_error is None


def test_a_plan_drafted_under_old_limits_fails_once_they_are_tightened(
    write_policy,
) -> None:
    """The gate condition, at the policy layer.

    A change is drafted while the cap is 5000 and would be allowed. The cap
    is then lowered to 1000. Re-evaluating the SAME change - which is what
    guards.py does at confirm time - now refuses it.
    """
    path = write_policy()
    store = PolicyStore(path)

    drafted = dict(tier=Tier.LEAD, current_units=1000, new_units=1200)
    assert evaluate_budget_change(store.current(), **drafted).allowed

    write_policy({"limits": {"defaults": {"budget": {"max_daily": 1000}}}})

    verdict = evaluate_budget_change(store.current(), **drafted)
    assert not verdict.allowed
    assert "exceeds" in verdict.describe()
