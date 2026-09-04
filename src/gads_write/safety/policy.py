"""The spending rules, and the pure functions that evaluate them.

There is no policy file. The design principle these rules answer to:

    A person should be able to do here what they could already do in the
    Google Ads UI. The UI has no guardrails at all - somebody types a number
    and saves - so the control there is the human. Here the human is still
    the control, but they are shown a preview first and must accept it, and
    every change is audited. That makes this strictly safer than the UI
    without being more restrictive than it.

So this module REFUSES very little, and only where refusing protects the
approval model itself:

  - an increase past `max_increase_percent`, which is a TYPO BACKSTOP and
    not an operating limit. It exists because the server cannot see what the
    person actually asked for - the tool call carries a number, never the
    conversation - so it can only judge magnitude. A decimal-place slip
    (50,000 for 5,000) looks much like the real thing to someone approving
    in a hurry; an eleven-fold jump does not arrive by intent. Set high
    enough that ordinary work never meets it.
  - a budget or bid of zero or less, and a rise from zero, where a
    percentage is undefined.

Everything else that used to be refused here is now either information on
the preview or gone entirely:

  min_daily / max_daily / max_cpc   REMOVED. Absolute rupee figures are wrong
    for some account and stale for all of them. `min_daily` was worse than
    useless - it blocked LOWERING a budget, the one change that can only
    reduce spend.

  the per-user daily ceiling        REMOVED as a refusal. It blocked ordinary
    work: raising five campaigns for a seasonal push would stop after the
    second. The running total is still computed from the audit log and shown
    on the preview, so the person approving can see "you have already raised
    budgets by X today" and decide for themselves.

  broad match on manual CPC        Now a WARNING on the preview, not a
    refusal. The Google Ads UI permits it; so do we, having said it is risky.

What is still refused lives elsewhere, and only for these reasons: an
operation that cannot be undone (there are no delete tools), an account
outside our MCC (safety/accounts.py - that protects our developer token, not
your budget), and a change whose preview would LIE about what it does
(a shared budget names one campaign and changes several - see
`shared_budget_verdict`). Anything that corrupts the preview destroys the one
control everything else rests on.

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
    # The only budget rule left. Relative, so it never needs tuning, and set
    # high enough to be a typo backstop rather than an operating limit.
    max_increase_percent: Decimal


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
    allowed_final_url_domains: frozenset[str]


# ---------------------------------------------------------------------------
# fixed rules
# ---------------------------------------------------------------------------
# These were settings. They are constants now because there is no plausible
# reason to want the other value: nobody wants a new keyword to start
# spending before a human has seen it, and nobody wants broad match on a
# manual-CPC campaign.
NEW_ENTITIES_START_PAUSED = True

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
        budget=BudgetLimits(max_increase_percent=Decimal(0)),
        bids=BidLimits(max_increase_percent=Decimal(0)),
    )

    limits_by_tier: dict[str, TierLimits] = {}
    for tier in Tier:
        if tier in NON_WRITING_TIERS:
            limits_by_tier[tier.value] = denied_everything
            continue
        limits_by_tier[tier.value] = TierLimits(
            budget=BudgetLimits(max_increase_percent=max_increase_percent),
            bids=BidLimits(max_increase_percent=max_increase_percent),
        )

    return Policy(
        rules=Rules(
            new_entities_start_paused=NEW_ENTITIES_START_PAUSED,
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

    It deliberately exposes no `last_error`: there is no edit left that could
    be refused, so a field that could only ever be None would be a standing
    invitation to believe this file still reloads. `/healthz` reports the
    roles table instead, which genuinely can fail that way.
    """

    def __init__(self, settings: Any) -> None:
        self._policy = build_policy(
            max_increase_percent=settings.max_increase_percent,
            allowed_final_url_domains=settings.allowed_url_domains,
        )

    def current(self) -> Policy:
        return self._policy


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
) -> PolicyVerdict:
    """Check one daily-budget change against the typo backstop.

    One rule: the increase may not exceed `max_increase_percent`. That is
    deliberately set high enough never to meet ordinary work - it exists
    because the server cannot see what the person actually ASKED for, only
    the number Claude produced, so it can judge magnitude and nothing else.

    Boundaries are inclusive. Only INCREASES are constrained: lowering a
    budget can only reduce spend, so there is no minimum and never was a good
    reason for one.

    The per-user daily total is no longer checked here. It is still computed
    from the audit log and shown on the preview, so the person approving sees
    what they have already done today and decides - which is the same
    position they are in using the Google Ads UI, with more information.
    """
    limits = policy.limits_for(tier).budget
    current = coerce_units(current_units, field="current_units")
    proposed = coerce_units(new_units, field="new_units")
    code = policy.currency_code

    if proposed <= 0:
        return PolicyVerdict.deny(
            f"a daily budget must be above zero, got {proposed} {code}"
        )

    if proposed <= current:
        # A decrease. Nothing to check - it cannot raise spend.
        return PolicyVerdict.allow()

    if current == 0:
        # percent_change from zero is undefined. Refuse rather than invent a
        # percentage.
        return PolicyVerdict.deny(
            "cannot raise a budget from zero through this server; the "
            "percentage increase is undefined. Set it in the Google Ads UI "
            "once, then adjust it here."
        )

    increase_percent = percent_change(current, proposed)
    if increase_percent > limits.max_increase_percent:
        return PolicyVerdict.deny(
            f"a {increase_percent}% increase is past the {limits.max_increase_percent}% "
            f"safety backstop ({current} -> {proposed} {code}). That limit is "
            "set far above normal work, so this usually means a digit went "
            "astray. Make the change in two steps if it is genuinely intended."
        )

    return PolicyVerdict.allow()


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
                    f"a {increase_percent}% increase is past the "
                    f"{limits.max_increase_percent}% safety backstop "
                    f"({current} -> {proposed} {code}). That limit is set far "
                    "above normal work, so this usually means a digit went "
                    "astray."
                )

    return PolicyVerdict.deny(*reasons) if reasons else PolicyVerdict.allow()


def evaluate_operation(policy: Policy, operation: str) -> PolicyVerdict:
    if policy.blocks_operation(operation):
        return PolicyVerdict.deny(
            f"operation {operation!r} is refused outright; there is no tool "
            "for it and there will not be one"
        )
    return PolicyVerdict.allow()


def broad_match_warning(
    *, match_type: str, bidding_strategy: str
) -> str | None:
    """A warning for the preview, or None. NOT a refusal.

    Broad match under manual CPC is how accounts quietly haemorrhage money,
    and it used to be refused outright. The Google Ads UI allows it, so this
    server does too - the person approving is told why it is risky and
    decides, which is the position they are in in the UI, with more
    information rather than less.

    An unreadable bidding strategy produces no warning rather than a refusal:
    failing to establish an advisory fact is not a reason to block a change
    the UI would have allowed.
    """
    if str(match_type).strip().upper() != "BROAD":
        return None
    if "MANUAL_CPC" not in str(bidding_strategy).strip().upper():
        return None
    return (
        "WARNING: broad match on a manual-CPC campaign is the classic way to "
        "spend quickly on searches you did not intend. Consider phrase or "
        "exact match, or an automated bidding strategy."
    )
