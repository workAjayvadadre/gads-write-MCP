"""The spending rules, and the pure functions that evaluate them.

There is no policy FILE any more. Every value here is either derived from
the account being changed, fixed in code, or set once at deploy in the
environment - because a rupee limit in a YAML file was unmaintainable by
construction. ₹50 means nothing without knowing the account: too high for a
test campaign, absurdly low for a real one, and stale the moment budgets
move. Somebody has to keep it current, and that somebody was a developer.

What replaced each thing, and why it is safe:

  min_daily / max_daily / max_cpc   REMOVED. They bounded nothing a human
    could not already do in the Google Ads UI, and Google itself offers no
    manager-set ceiling on a campaign budget for us to lean on. The error
    they actually caught was an LLM turning "bump it a bit" into 50000, and
    a RELATIVE cap catches that without knowing anything about the account.
    `min_daily` was worse than useless: it blocked LOWERING a budget, which
    is the safe direction.

  max_increase_percent             KEPT, and now the load-bearing control.
    "Never more than double in one change" is correct for a ₹100 campaign
    and a ₹100,000 one, never goes stale, and needs no setup.
    GADS_MAX_INCREASE_PERCENT, default 100.

  max_total_increase_per_user_per_day   KEPT, but expressed as a percentage
    of the ACCOUNT'S OWN total daily budget rather than a rupee amount, so
    it scales itself. It closes the incremental-creep hole that the
    per-change cap cannot: ten changes each under the per-change limit still
    compound. Still derived from the audit log, never a counter - see
    safety/spend.py.

  the structural rules             Fixed in code. Nobody was ever going to
    want "new keywords start ENABLED" or "broad match on manual CPC is fine".
    A setting nobody should change is not a setting.

`PolicyStore` survives as the seam every caller already reads through, so
that removing the file changed no call site downstream. It no longer reloads
anything, and `last_error` is now always None - there is no longer any edit
that can be refused.

Python notes for a TypeScript reader:
  - `Decimal` again for money. Never float.
  - `frozenset` is an immutable Set. Using it makes accidental mutation of
    a shared snapshot a TypeError rather than a silent policy change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from ..auth.tiers import Tier
from .units import MoneyError, coerce_units, percent_change

logger = logging.getLogger(__name__)

SUPPORTED_VERSION = 2

# Tiers that may never change anything, enforced in code rather than trusted
# to config. See parse_policy.
NON_WRITING_TIERS = (Tier.NONE, Tier.READONLY)


class PolicyError(RuntimeError):
    """Raised when the policy cannot be assembled or read."""


# ---------------------------------------------------------------------------
# immutable snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BudgetLimits:
    # Per change. Relative, so it never needs tuning per account.
    max_increase_percent: Decimal
    # Per user per day, as a percentage of the account's own total daily
    # budget. Relative for the same reason, and it is what stops ten small
    # increases compounding past what one large one would have been refused
    # for.
    max_total_increase_percent_of_account: Decimal


@dataclass(frozen=True)
class BidLimits:
    max_increase_percent: Decimal


@dataclass(frozen=True)
class TierLimits:
    budget: BudgetLimits
    bids: BidLimits


@dataclass(frozen=True)
class Rules:
    new_entities_start_paused: bool
    block_broad_match_with_manual_cpc: bool
    allowed_final_url_domains: frozenset[str]


# ---------------------------------------------------------------------------
# fixed rules
# ---------------------------------------------------------------------------
# These were settings. They are constants now because there is no plausible
# reason to want the other value: nobody wants a new keyword to start
# spending before a human has seen it, and nobody wants broad match on a
# manual-CPC campaign.
NEW_ENTITIES_START_PAUSED = True
BLOCK_BROAD_MATCH_WITH_MANUAL_CPC = True

# A plan is a draft awaiting human approval. Ten minutes is long enough to
# read a preview and short enough that an abandoned one cannot be applied
# later by accident.
PLAN_TTL_SECONDS = 600
PLAN_SINGLE_USE = True

# Second line of defence only: none of these tools exist. If one ever fires,
# something regressed.
BLOCKED_OPERATIONS = frozenset(
    {
        "remove_campaign",
        "remove_ad_group",
        "remove_keyword",
        "remove_budget",
        "remove_conversion_action",
        "remove_ad",
    }
)

# The daily ceiling, per tier, as a percentage of the account's total daily
# budget. Fixed in code rather than configured, and deliberately different
# per tier: it is the one place the operator/lead distinction still carries a
# number. An operator may add a tenth of the account's daily spend in a day;
# a lead a quarter.
DAILY_INCREASE_PERCENT_BY_TIER: dict[Tier, Decimal] = {
    Tier.OPERATOR: Decimal(10),
    Tier.LEAD: Decimal(25),
}


def build_policy(
    *,
    max_increase_percent: Decimal,
    allowed_final_url_domains: frozenset[str],
) -> "Policy":
    """Assemble the policy from settings and the constants above.

    Replaces `parse_policy`. `none` and `readonly` are still pinned to zero
    HERE, in code, for the same reason they always were: a config file that
    omitted their block would have had them silently inherit the permissive
    defaults, which is exactly the bug a test caught while this module was
    first written. There is no file to omit anything now, but the pinning is
    what makes that guarantee independent of how the table above is edited.
    """
    denied_everything = TierLimits(
        budget=BudgetLimits(
            max_increase_percent=Decimal(0),
            max_total_increase_percent_of_account=Decimal(0),
        ),
        bids=BidLimits(max_increase_percent=Decimal(0)),
    )

    limits_by_tier: dict[str, TierLimits] = {}
    for tier in Tier:
        if tier in NON_WRITING_TIERS:
            limits_by_tier[tier.value] = denied_everything
            continue
        limits_by_tier[tier.value] = TierLimits(
            budget=BudgetLimits(
                max_increase_percent=max_increase_percent,
                max_total_increase_percent_of_account=(
                    DAILY_INCREASE_PERCENT_BY_TIER[tier]
                ),
            ),
            bids=BidLimits(max_increase_percent=max_increase_percent),
        )

    return Policy(
        rules=Rules(
            new_entities_start_paused=NEW_ENTITIES_START_PAUSED,
            block_broad_match_with_manual_cpc=BLOCK_BROAD_MATCH_WITH_MANUAL_CPC,
            allowed_final_url_domains=allowed_final_url_domains,
        ),
        blocked_operations=BLOCKED_OPERATIONS,
        plan_ttl_seconds=PLAN_TTL_SECONDS,
        plan_single_use=PLAN_SINGLE_USE,
        _limits_by_tier=limits_by_tier,
    )


@dataclass(frozen=True)
class Policy:
    rules: Rules
    blocked_operations: frozenset[str]
    plan_ttl_seconds: int
    plan_single_use: bool
    _limits_by_tier: dict[str, TierLimits]
    # Stamped on per request by the gate, from the account being changed.
    # See `for_account` and `with_account_budget`.
    currency_code: str = ""
    timezone: str = "UTC"
    # The account's total daily budget across its campaigns. `None` means it
    # was never established, which is why the daily-ceiling rule refuses
    # rather than assuming - see evaluate_budget_change.
    account_total_budget_units: Decimal | None = None

    def limits_for(self, tier: Tier) -> TierLimits:
        """Limits for a tier, with the non-writing tiers pinned to zero."""
        try:
            return self._limits_by_tier[tier.value]
        except KeyError as exc:  # pragma: no cover - every Tier is populated
            raise PolicyError(f"no limits configured for tier {tier.value!r}") from exc

    def for_account(self, *, currency_code: str, timezone: str) -> "Policy":
        """This policy, stamped with one account's currency and timezone.

        The seam that let `currency_code` and `timezone` stop being config.
        Every downstream evaluator already reads them off the snapshot, so
        rebinding them here means no evaluator signature had to change - and
        an account in a different currency can no longer be compared against
        a limit expressed in someone else's.
        """
        return replace(
            self,
            currency_code=(currency_code or "").strip() or self.currency_code,
            timezone=(timezone or "").strip() or self.timezone,
        )

    def with_account_budget(self, total_units: Decimal | None) -> "Policy":
        """This policy, stamped with the account's total daily budget.

        Same seam, one step later: the gate fetches this ONLY when a rule
        actually needs it, so a read - which has no spend to check - still
        costs no Google round trip. See safety/guards.py step 7.
        """
        return replace(self, account_total_budget_units=total_units)

    def blocks_operation(self, operation: str) -> bool:
        return str(operation).strip() in self.blocked_operations


# ---------------------------------------------------------------------------
# the snapshot holder
# ---------------------------------------------------------------------------

class PolicyStore:
    """Holds the active policy.

    This used to own a YAML file and hot-reload it. It no longer does, because
    there is no file: every value is derived, fixed in code, or set once in
    the environment. The class survives because it is the seam every caller
    already reads through - removing it would have churned the whole codebase
    to express "there is nothing to reload".

    `last_error` is kept, and is now always None. It feeds `health_check` and
    `/healthz`, which still report a refused ROLES reload; there is simply no
    longer any policy edit that can be refused.
    """

    def __init__(self, settings: Any) -> None:
        self._policy = build_policy(
            max_increase_percent=settings.max_increase_percent,
            allowed_final_url_domains=settings.allowed_url_domains,
        )

    def current(self) -> Policy:
        return self._policy

    @property
    def last_error(self) -> str | None:
        """Always None. There is no config edit left that could be refused."""
        return None


# ---------------------------------------------------------------------------
# evaluation - pure functions over a snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyVerdict:
    """Why a change was allowed or refused."""

    allowed: bool
    reasons: tuple[str, ...] = ()

    @classmethod
    def allow(cls) -> "PolicyVerdict":
        return cls(allowed=True)

    @classmethod
    def deny(cls, *reasons: str) -> "PolicyVerdict":
        return cls(allowed=False, reasons=tuple(reasons))

    def merged_with(self, other: "PolicyVerdict") -> "PolicyVerdict":
        if self.allowed and other.allowed:
            return self
        return replace(
            self, allowed=False, reasons=self.reasons + other.reasons
        )

    def describe(self) -> str:
        return "allowed" if self.allowed else "; ".join(self.reasons)


def evaluate_budget_change(
    policy: Policy,
    *,
    tier: Tier,
    current_units: object,
    new_units: object,
    already_increased_today_units: object = 0,
) -> PolicyVerdict:
    """Check one daily-budget change against the tier's relative limits.

    Two rules, both relative, neither needing to know anything about this
    particular account in advance:

      per change   the increase may not exceed max_increase_percent
      per day      this user's TOTAL increases today, this one included, may
                   not exceed a percentage of the account's total daily budget

    The second is what the first cannot do. A 100% per-change cap still allows
    100 -> 200 -> 400 -> 800 inside an afternoon; the daily ceiling bounds the
    day.

    Boundaries are inclusive: exactly at a limit is allowed, a hair over is
    not. Only INCREASES are constrained. There is deliberately no minimum any
    more - refusing to lower a budget was refusing the one change that can
    only ever reduce spend.
    """
    limits = policy.limits_for(tier).budget
    reasons: list[str] = []

    current = coerce_units(current_units, field="current_units")
    proposed = coerce_units(new_units, field="new_units")
    spent_today = coerce_units(
        already_increased_today_units, field="already_increased_today_units"
    )
    code = policy.currency_code

    if proposed <= 0:
        return PolicyVerdict.deny(
            f"a daily budget must be above zero, got {proposed} {code}"
        )

    delta = proposed - current
    if delta <= 0:
        # A decrease. Nothing to check: it cannot raise spend, and it must not
        # create headroom under the daily ceiling either - see safety/spend.py,
        # which counts positive deltas only.
        return PolicyVerdict.allow()

    if current == 0:
        # percent_change from zero is undefined. Treat any rise off a zero
        # budget as unbounded and refuse it rather than invent a percentage.
        reasons.append(
            "cannot raise a budget from zero through this server; the "
            "percentage increase is undefined. Set it in the Google Ads UI "
            "once, then adjust it here."
        )
    else:
        increase_percent = percent_change(current, proposed)
        if increase_percent > limits.max_increase_percent:
            reasons.append(
                f"a {increase_percent}% increase exceeds the limit of "
                f"{limits.max_increase_percent}% per change "
                f"({current} -> {proposed} {code})"
            )

    account_total = policy.account_total_budget_units
    if account_total is None:
        # Refuse rather than skip. The ceiling is a real control, and "we could
        # not work out the base" is a reason to stop, not a reason to wave an
        # unbounded change through.
        reasons.append(
            "the account's total daily budget could not be established, so "
            "your daily increase ceiling cannot be applied. Nothing was "
            "changed; try again in a moment."
        )
    elif account_total > 0:
        ceiling = (
            account_total * limits.max_total_increase_percent_of_account
        ) / Decimal(100)
        running_total = spent_today + delta
        if running_total > ceiling:
            reasons.append(
                f"this raises your total budget increases today to "
                f"{running_total} {code}, over your {tier.value} ceiling of "
                f"{ceiling} {code} "
                f"({limits.max_total_increase_percent_of_account}% of the "
                f"account's {account_total} {code} total daily budget; "
                f"already {spent_today} {code} today)"
            )

    return PolicyVerdict.deny(*reasons) if reasons else PolicyVerdict.allow()


def evaluate_bid_change(
    policy: Policy,
    *,
    tier: Tier,
    current_units: object,
    new_units: object,
) -> PolicyVerdict:
    """Check one max-CPC change against the tier's relative limit.

    There is no absolute max CPC any more, for the same reason there is no
    absolute max budget: the right number depends entirely on the account, and
    a wrong one either blocks every real change or protects nothing. A bid has
    no daily ceiling behind it the way a budget does, which is why
    `update_ad_group_bid` sits at `lead` in tools/registry.py.
    """
    limits = policy.limits_for(tier).bids
    reasons: list[str] = []

    current = coerce_units(current_units, field="current_units")
    proposed = coerce_units(new_units, field="new_units")
    code = policy.currency_code

    if proposed <= 0:
        return PolicyVerdict.deny(f"a bid must be above zero, got {proposed} {code}")

    if proposed > current:
        if current == 0:
            reasons.append(
                "cannot raise a bid from zero through this server; the "
                "percentage increase is undefined"
            )
        else:
            increase_percent = percent_change(current, proposed)
            if increase_percent > limits.max_increase_percent:
                reasons.append(
                    f"a {increase_percent}% increase exceeds the limit of "
                    f"{limits.max_increase_percent}% per change "
                    f"({current} -> {proposed} {code})"
                )

    return PolicyVerdict.deny(*reasons) if reasons else PolicyVerdict.allow()


def evaluate_operation(policy: Policy, operation: str) -> PolicyVerdict:
    if policy.blocks_operation(operation):
        return PolicyVerdict.deny(
            f"operation {operation!r} is refused outright; there is no tool "
            "for it and there will not be one"
        )
    return PolicyVerdict.allow()


def evaluate_match_type_against_bidding(
    policy: Policy, *, match_type: str, bidding_strategy: str
) -> PolicyVerdict:
    """Broad match under manual CPC is how accounts quietly haemorrhage money."""
    if not policy.rules.block_broad_match_with_manual_cpc:
        return PolicyVerdict.allow()
    if (
        str(match_type).strip().upper() == "BROAD"
        and "MANUAL_CPC" in str(bidding_strategy).strip().upper()
    ):
        return PolicyVerdict.deny(
            "broad match is not permitted on a manual CPC campaign "
            "(rules.block_broad_match_with_manual_cpc). Use phrase or exact, "
            "or move the campaign to an automated bidding strategy."
        )
    return PolicyVerdict.allow()
