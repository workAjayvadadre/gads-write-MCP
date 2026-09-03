"""What must be true for one write tool, in one place, for BOTH steps.

Every write crosses this module twice. `tools/writes.py` calls it when it
drafts a plan; `tools/confirm.py` calls it again before applying one. That is
the entire point of the file: the checks run at draft time and the checks
re-run at confirm time must be literally the same code, or a plan can be
applied under rules it was never checked against.

They used to be different code, and the gap was real. `REVALIDATORS` carried
only the argument-shape validator, and confirm re-ran that faithfully. But
the money rules - `max_increase_percent`, broad match under manual CPC, and
the per-user daily ceiling - lived in an `evaluate` closure inside each draft
tool's body, reachable from nowhere else. Confirm never passed one, and
`Guard.check` skips the whole policy evaluation when `evaluate is None`. So a
plan drafted while the ceiling was wide still applied after it narrowed.

The other half of the same bug: budget and bid rules are RELATIVE. A plan
stores an ABSOLUTE target (`amount_micros`), so re-checking one means
re-establishing what the account looks like NOW. `reads` names the entity a
tool's recheck needs, and confirm fetches it fresh rather than trusting the
value the preview was built from - otherwise an approved "100 -> 120", a +20%
change, silently becomes +1100% when someone lowers the budget to 10 in the
Google Ads UI first.

Both holes are pinned shut by regression tests in tests/test_phase5_tools.py.

Python notes for a TypeScript reader:
  - `Protocol` with `__call__` types a function *shape*, the equivalent of a
    TS call signature `(policy: Policy, opts: {...}) => PolicyVerdict`.
  - Keyword-only parameters (everything after the bare `*`) are what stops a
    future edit swapping `current_units` and `new_units` at a call site. In a
    1,000,000x-error domain that is worth the extra typing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from ..ads.reads import AdsReader
from ..auth.tiers import Tier
from ..safety.policy import (
    Policy,
    PolicyVerdict,
    evaluate_bid_change,
    evaluate_budget_change,
    evaluate_match_type_against_bidding,
)
from ..safety.units import MICROS_PER_UNIT, MoneyError, coerce_units
from ..safety.validators import (
    ValidationResult,
    validate_customer_id,
    validate_keyword_text,
    validate_match_type,
    validate_numeric_id,
    validate_rsa,
)


class ReadKind(str, Enum):
    """Which entity a tool's recheck needs the CURRENT state of.

    `NONE` means the tool's rules are absolute - a negative keyword or an RSA
    is judged on its own arguments, so there is nothing to re-read and confirm
    does not pay for a round trip to Google.
    """

    NONE = "none"
    CAMPAIGN = "campaign"
    AD_GROUP = "ad_group"


class Validate(Protocol):
    """Argument shape and content. Cheap, and needs no account state."""

    def __call__(
        self, policy: Policy, arguments: dict[str, Any]
    ) -> ValidationResult: ...


class Recheck(Protocol):
    """The policy rules. Needs the tier, the account state and the ledger.

    `payload` is what will actually be sent to Google. It is here because a
    rule has to be able to compare the thing being CHECKED against the thing
    being CHANGED: policy is evaluated from `current`, but the mutation is
    addressed to the resource named in the payload, and those two can drift
    apart between drafting a plan and confirming it.
    """

    def __call__(
        self,
        policy: Policy,
        *,
        tier: Tier,
        arguments: dict[str, Any],
        current: Any,
        payload: dict[str, Any],
        spend_today: Decimal,
    ) -> PolicyVerdict: ...


class SpendDelta(Protocol):
    """How much this change raises daily spend by, in currency units.

    None means "nothing to charge against the ceiling" - a decrease, or a
    change that moves no money. Computed from the CURRENT state at both
    steps, never carried over from the plan: the ceiling is derived from the
    audit log, so a stale figure written there is a permanent hole in it.
    """

    def __call__(
        self, arguments: dict[str, Any], current: Any
    ) -> Decimal | None: ...


@dataclass(frozen=True)
class OperationChecks:
    """Everything the gate needs to judge one write tool, at either step."""

    validate: Validate
    # None when the tool has no policy rules beyond its argument validation.
    recheck: Recheck | None = None
    reads: ReadKind = ReadKind.NONE
    # Which argument carries the id of the entity named by `reads`.
    id_argument: str = ""
    # None when the tool cannot raise daily spend.
    spend_delta: SpendDelta | None = None
    # True only for rules measured against the ACCOUNT'S total daily budget -
    # in practice, the per-user daily ceiling on budget changes. The gate
    # fetches that total from Google, so declaring it here is what keeps the
    # read off every other tool's path: a bid has no daily ceiling behind it
    # and must not pay for a round trip it cannot use.
    needs_account_total: bool = False


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------
# Every one of these takes (policy, arguments) even when it ignores the
# policy, so that the table below is uniform and a caller cannot pick the
# wrong calling convention for a tool.


def validate_campaign_status_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Validate the arguments of a campaign status change."""
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    return result


def validate_budget_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    return result


def validate_negative_keyword_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    target = arguments.get("campaign_id") or arguments.get("ad_group_id") or ""
    result.extend(validate_numeric_id(target, field_name="target_id"))
    result.extend(validate_keyword_text(arguments.get("keyword_text", "")))
    result.extend(validate_match_type(arguments.get("match_type", "")))
    return result


def validate_keyword_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    result.extend(validate_keyword_text(arguments.get("keyword_text", "")))
    result.extend(validate_match_type(arguments.get("match_type", "")))
    return result


def validate_bid_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    return result


def validate_rsa_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    result.extend(
        validate_rsa(
            headlines=list(arguments.get("headlines") or []),
            descriptions=list(arguments.get("descriptions") or []),
            final_urls=list(arguments.get("final_urls") or []),
            allowed_domains=policy.rules.allowed_final_url_domains,
            path1=arguments.get("path1"),
            path2=arguments.get("path2"),
        )
    )
    return result


# ---------------------------------------------------------------------------
# policy rechecks
# ---------------------------------------------------------------------------


def _units_from(arguments: dict[str, Any], field: str) -> Decimal | None:
    """Plans store amounts as strings. None means 'not a usable number'."""
    try:
        return coerce_units(arguments.get(field), field=field)
    except MoneyError:
        return None


def units_from_micros(micros: object) -> Decimal:
    """Exact micros -> units, with no rounding.

    Deliberately not `safety/units.from_micros`, which quantizes to two
    decimal places for DISPLAY. A limit comparison has to use the exact
    value, or a change could round its way under a cap it is actually over.
    """
    return Decimal(int(micros or 0)) / Decimal(MICROS_PER_UNIT)


def shared_budget_verdict(current: Any) -> PolicyVerdict:
    """Refuse any budget that is not used by exactly one campaign.

    A budget feeding several campaigns changes all of their spending, while
    the preview a human approves names one. A misleading preview breaks the
    entire approval model, which is worth more than the convenience.

    Checked at BOTH steps. A budget that picks up a second campaign between
    drafting and confirming is precisely what a draft-time-only check misses.
    """
    count = int(getattr(current, "budget_reference_count", 0) or 0)
    if count == 1:
        return PolicyVerdict.allow()
    if count > 1:
        return PolicyVerdict.deny(
            f"this campaign uses a SHARED budget - {count} campaigns draw on "
            "it, so changing it would change their spending too, and this "
            "preview can only describe one campaign. Change it in the Google "
            "Ads UI, or give this campaign its own budget."
        )
    return PolicyVerdict.deny(
        "Google did not report how many campaigns use this budget, so we "
        "cannot tell whether changing it would affect campaigns this preview "
        "does not name. Refusing rather than guessing."
    )


def recheck_budget(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """Tier limits and the per-user daily ceiling, against the CURRENT budget."""
    new_units = _units_from(arguments, "new_daily_budget")
    if new_units is None:
        return PolicyVerdict.deny(
            "new_daily_budget is not a usable number, so this change cannot be "
            "checked against the budget limits."
        )
    if current is None:
        return PolicyVerdict.deny(
            "the campaign's current budget could not be established, so a "
            "percentage limit cannot be applied to this change."
        )

    shared = shared_budget_verdict(current)
    if not shared.allowed:
        return shared

    # The limits below are evaluated from `current`, but the mutation is
    # addressed to the resource named in the payload. If the campaign has
    # been moved onto a different budget since this was drafted, those are
    # two different resources: we would check one and change the other.
    target = str(payload.get("budget_resource_name") or "")
    actual = str(getattr(current, "budget_resource_name", "") or "")
    if target and actual and target != actual:
        return PolicyVerdict.deny(
            "this campaign no longer uses the budget this change was drafted "
            f"against ({target}); it now uses {actual}. The limits would be "
            "checked against one budget and the change applied to another, so "
            "this plan is refused. Draft it again."
        )

    return evaluate_budget_change(
        policy,
        tier=tier,
        current_units=units_from_micros(current.daily_budget_micros),
        new_units=new_units,
        already_increased_today_units=spend_today,
    )


def budget_spend_delta(arguments: dict[str, Any], current: Any) -> Decimal | None:
    """The increase this change makes, or None if it does not raise spend."""
    new_units = _units_from(arguments, "new_daily_budget")
    if new_units is None or current is None:
        return None
    delta = new_units - units_from_micros(current.daily_budget_micros)
    return delta if delta > 0 else None


def recheck_bid(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """Max CPC and the percentage cap, against the CURRENT bid."""
    new_units = _units_from(arguments, "new_max_cpc")
    if new_units is None:
        return PolicyVerdict.deny(
            "new_max_cpc is not a usable number, so this change cannot be "
            "checked against the bid limits."
        )
    if current is None:
        return PolicyVerdict.deny(
            "the ad group's current bid could not be established, so a "
            "percentage limit cannot be applied to this change."
        )
    return evaluate_bid_change(
        policy,
        tier=tier,
        current_units=units_from_micros(current.cpc_bid_micros),
        new_units=new_units,
    )


def recheck_keyword(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """Broad match under manual CPC is the classic way to burn money.

    The bidding strategy is read from the ad group rather than remembered,
    because a campaign can be moved off manual CPC - or onto it - between
    drafting a keyword and confirming it.
    """
    if current is None:
        return PolicyVerdict.deny(
            "the ad group's bidding strategy could not be established, so this "
            "keyword cannot be checked against the match-type rule."
        )
    return evaluate_match_type_against_bidding(
        policy,
        match_type=str(arguments.get("match_type", "")),
        bidding_strategy=current.bidding_strategy_type,
    )


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
# tool name -> its checks. tools/confirm.py fails closed on a tool that is
# missing from here: no entry means no way to re-check, which means no way to
# apply. Registering a tool in tools/registry.py without adding it here makes
# it draftable and unconfirmable, which is the safe direction to get it wrong.

OPERATIONS: dict[str, OperationChecks] = {
    "pause_campaign": OperationChecks(validate=validate_campaign_status_args),
    "enable_campaign": OperationChecks(validate=validate_campaign_status_args),
    "update_campaign_budget": OperationChecks(
        validate=validate_budget_args,
        recheck=recheck_budget,
        reads=ReadKind.CAMPAIGN,
        id_argument="campaign_id",
        spend_delta=budget_spend_delta,
        needs_account_total=True,
    ),
    "add_negative_keyword": OperationChecks(validate=validate_negative_keyword_args),
    "add_keyword": OperationChecks(
        validate=validate_keyword_args,
        recheck=recheck_keyword,
        reads=ReadKind.AD_GROUP,
        id_argument="ad_group_id",
    ),
    "update_ad_group_bid": OperationChecks(
        validate=validate_bid_args,
        recheck=recheck_bid,
        reads=ReadKind.AD_GROUP,
        id_argument="ad_group_id",
    ),
    "create_responsive_search_ad": OperationChecks(validate=validate_rsa_args),
}


async def read_current(
    reader: AdsReader, checks: OperationChecks, *, customer_id: str, arguments: dict
) -> Any | None:
    """Fetch the entity `checks.reads` names, as it is right now.

    Returns None when the tool needs no state, or when the entity is gone.
    Raises `AdsReadError` if the read itself failed - callers must not
    conflate "it is not there" with "we could not find out".
    """
    if checks.reads is ReadKind.NONE:
        return None
    entity_id = str(arguments.get(checks.id_argument) or "").strip()
    if not entity_id:
        return None
    if checks.reads is ReadKind.CAMPAIGN:
        return await reader.campaign_by_id(
            customer_id=customer_id, campaign_id=entity_id
        )
    return await reader.ad_group_by_id(customer_id=customer_id, ad_group_id=entity_id)


__all__ = [
    "OPERATIONS",
    "SpendDelta",
    "budget_spend_delta",
    "shared_budget_verdict",
    "units_from_micros",
    "OperationChecks",
    "ReadKind",
    "Recheck",
    "Validate",
    "read_current",
    "recheck_bid",
    "recheck_budget",
    "recheck_keyword",
    "validate_bid_args",
    "validate_budget_args",
    "validate_campaign_status_args",
    "validate_keyword_args",
    "validate_negative_keyword_args",
    "validate_rsa_args",
]
