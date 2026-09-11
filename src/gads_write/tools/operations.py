"""What must be true for one write tool, in one place, for BOTH steps.

Every write crosses this module twice. `tools/writes.py` calls it when it
drafts a plan; `tools/confirm.py` calls it again before applying one. That is
the entire point of the file: the checks run at draft time and the checks
re-run at confirm time must be literally the same code, or a plan can be
applied under rules it was never checked against.

They used to be different code, and the gap was real. `REVALIDATORS` carried
only the argument-shape validator, and confirm re-ran that faithfully. But
the money rules lived in an `evaluate` closure inside each draft tool's
body, reachable from nowhere else. Confirm never passed one, and
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
)
from ..safety.units import MICROS_PER_UNIT, MoneyError, coerce_units
from ..safety.validators import (
    ValidationResult,
    validate_ad_group_name,
    validate_geo_target_ids,
    validate_bidding_strategy,
    validate_campaign_name,
    validate_campaign_schedule,
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
    # Needs TWO ids, not one: an ad_group_criterion is addressed as
    # `{ad_group_id}~{criterion_id}`. `id_argument` names the criterion and
    # `read_current` reads the ad group id alongside it.
    KEYWORD = "keyword"


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


def validate_ad_group_status_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Validate the arguments of an ad group status change.

    There is no status to validate, and that is the point: the status is
    hardcoded per tool in tools/writes.py and per operation in
    ads/executor.py, so it never arrives as input and can never be REMOVED.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    return result


def validate_keyword_status_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Validate a keyword status change. BOTH ids, and no status.

    Both ids because an ad_group_criterion resource name is
    `{ad_group_id}~{criterion_id}`: criterion ids are unique within an ad
    group, not within an account, so one id alone identifies nothing.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    result.extend(
        validate_numeric_id(arguments.get("criterion_id", ""), field_name="criterion_id")
    )
    return result


def validate_keyword_bid_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Same ids as a keyword status change; the amount is checked by policy."""
    return validate_keyword_status_args(policy, arguments)


def validate_ad_status_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Validate an ad status change. Both ids, and no status."""
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    result.extend(validate_numeric_id(arguments.get("ad_id", ""), field_name="ad_id"))
    return result


def validate_update_campaign_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Name and/or run dates, and at least one of them.

    A mutation that changes nothing is refused rather than applied: it would
    produce an audit line for a change nobody made, which is the same reason
    the status tools return "nothing to do" instead of drafting a plan.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )

    name = arguments.get("name")
    start_date = arguments.get("start_date")
    end_date = arguments.get("end_date")
    supplied = [
        value for value in (name, start_date, end_date)
        if value is not None and str(value).strip() != ""
    ]
    if not supplied:
        result.add(
            "name",
            "give at least one of name, start_date or end_date - there is "
            "nothing to change otherwise",
        )
        return result

    if name is not None and str(name).strip() != "":
        result.extend(validate_campaign_name(name))
    # Shape only. An end date supplied WITHOUT a start date can only be judged
    # against the campaign's current start, which this cannot see - that check
    # lives in `update_campaign_verdict`, which runs at both steps with the
    # campaign in hand.
    result.extend(validate_campaign_schedule(start_date, end_date))
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


def validate_create_ad_group_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Shape of the arguments for a new ad group.

    `max_cpc` is checked for being a usable, positive amount ONLY. Whether it
    is required at all, or must be absent, depends on the campaign's bidding
    strategy, which this cannot see - that lives in
    `ad_group_campaign_verdict` below, which runs at both steps.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    result.extend(validate_ad_group_name(arguments.get("name")))

    max_cpc = arguments.get("max_cpc")
    if max_cpc is None or str(max_cpc).strip() == "":
        return result
    try:
        bid_units = coerce_units(max_cpc, field="max_cpc")
    except MoneyError as exc:
        result.add("max_cpc", str(exc))
        return result
    if bid_units <= 0:
        result.add("max_cpc", f"a max CPC must be above zero, got {bid_units}")
    return result


# Campaign bidding strategies under which an ad group's OWN cpc_bid_micros is
# the bid Google uses. Verified against the v25 AdGroup proto, which says of
# cpc_bid_micros: "This field is used when the ad group's effective bidding
# strategy is Manual CPC." ENHANCED_CPC is the legacy manual strategy with a
# Google adjustment applied on top, so the ad group bid is still the base.
MANUAL_BIDDING_STRATEGIES = frozenset({"MANUAL_CPC", "ENHANCED_CPC"})


def ad_group_campaign_verdict(current: Any, payload: dict[str, Any]) -> PolicyVerdict:
    """Whether this ad group can legitimately go in THIS campaign, as it is now.

    Three things have to hold, and none of them can be judged from the
    arguments alone:

      the campaign exists and is not REMOVED - removal is terminal in Google
      Ads, so an ad group added to a removed campaign could never serve;

      the campaign is a SEARCH campaign - `type_` on an ad group is IMMUTABLE
      in the API, and this server only ever sets SEARCH_STANDARD. There are no
      delete tools, so a SEARCH_STANDARD ad group created in a Display campaign
      could never be corrected here;

      the bid matches the bidding strategy - required under Manual CPC, where
      the Google Ads UI also demands it, and absent under an automated
      strategy, where Google ignores the field entirely. Accepting a number
      Google ignores would put a figure on the preview that does nothing, and
      a preview that lies is what breaks the approval model.

    Checked at BOTH steps, which is the point of it living here. A campaign
    moved from Manual CPC to Maximize Clicks between drafting and confirming
    is exactly what a draft-time-only check misses.
    """
    if current is None:
        return PolicyVerdict.deny(
            "the campaign this ad group would go in could not be read, so we "
            "cannot tell whether it needs a default bid or whether an ad group "
            "belongs in it at all."
        )

    if str(getattr(current, "status", "") or "").upper() == "REMOVED":
        return PolicyVerdict.deny(
            "this campaign is REMOVED. Removal is permanent in Google Ads, so "
            "an ad group added to it could never serve."
        )

    channel = str(getattr(current, "channel_type", "") or "").strip().upper()
    if channel != "SEARCH":
        return PolicyVerdict.deny(
            f"this is a {channel or 'unreadable'} campaign, and this server only "
            "creates SEARCH_STANDARD ad groups. An ad group's type is immutable "
            "in Google Ads and there is no tool here to remove one, so the wrong "
            "type could never be corrected. Create it in the Google Ads UI."
        )

    strategy = str(getattr(current, "bidding_strategy_type", "") or "").strip().upper()
    if not strategy:
        return PolicyVerdict.deny(
            "the campaign's bidding strategy could not be established, so we "
            "cannot tell whether this ad group needs a default max CPC. "
            "Refusing rather than guessing."
        )

    has_bid = payload.get("cpc_bid_micros") is not None
    if strategy in MANUAL_BIDDING_STRATEGIES and not has_bid:
        return PolicyVerdict.deny(
            f"this campaign bids by {strategy}, so the ad group needs its own "
            "default max CPC - the Google Ads UI requires one on this screen "
            "too. Pass max_cpc in whole currency units."
        )
    if strategy not in MANUAL_BIDDING_STRATEGIES and has_bid:
        return PolicyVerdict.deny(
            f"this campaign bids by {strategy}, which sets bids for you, so "
            "Google ignores an ad group's max CPC entirely. Drop max_cpc rather "
            "than putting a number on the preview that does nothing."
        )

    return PolicyVerdict.allow()


def recheck_create_ad_group(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """The same verdict the draft ran, against the campaign as it is NOW."""
    return ad_group_campaign_verdict(current, payload)


def validate_location_target_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    result.extend(validate_geo_target_ids(arguments.get("location_ids")))
    return result


def location_target_verdict(current: Any, payload: dict[str, Any]) -> PolicyVerdict:
    """Whether this location change still means what its preview said.

    Two things, and both are about the preview rather than about spending:

      the campaign exists and is not REMOVED - removal is terminal in Google
      Ads, so a location added to a removed campaign could never serve;

      the campaign's presence setting has not moved since drafting. This is
      the one that matters. `positive_geo_target_type` is CAMPAIGN-level, not
      a criterion, and Google's default (PRESENCE_OR_INTEREST) serves ads to
      anyone in the world showing interest in a targeted place. So a campaign
      that was already PRESENCE when this was drafted needs no change and the
      preview says "unchanged" - but if somebody switched it back to
      presence-or-interest in the Google Ads UI in the meantime, applying the
      plan would add the locations and leave interest targeting ON, under a
      preview that promised otherwise. Refuse and make them draft again.

    Checked at BOTH steps, which is why it lives here rather than in the tool.
    """
    if current is None:
        return PolicyVerdict.deny(
            "the campaign these locations would be added to could not be read, "
            "so we cannot tell what this change would actually do."
        )

    if str(getattr(current, "status", "") or "").upper() == "REMOVED":
        return PolicyVerdict.deny(
            "this campaign is REMOVED. Removal is permanent in Google Ads, so "
            "locations added to it could never serve."
        )

    if not payload.get("geo_target_constant_ids"):
        return PolicyVerdict.deny(
            "this change names no locations, so there is nothing to apply."
        )

    expected = str(payload.get("expected_positive_geo_target_type") or "")
    actual = str(getattr(current, "positive_geo_target_type", "") or "")
    if expected and actual and expected != actual:
        return PolicyVerdict.deny(
            "this campaign's location setting has changed since this was "
            f"drafted: the preview was built when it was {expected}, and it is "
            f"now {actual}. Applying the plan would not do what the preview "
            "said. Draft it again."
        )

    return PolicyVerdict.allow()


def recheck_location_target(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """The same verdict the draft ran, against the campaign as it is NOW."""
    return location_target_verdict(current, payload)


def keyword_bid_baseline_micros(current: Any) -> int:
    """The bid a keyword actually uses today, in micros.

    Deliberately `effective_cpc_bid_micros` and NOT the keyword's own
    `cpc_bid_micros`. A keyword with no bid of its own has
    cpc_bid_micros == 0 and bids the ad group's default instead - and
    `evaluate_bid_change` refuses a rise from zero, because a percentage from
    zero is undefined. Using the own-bid as the baseline would therefore
    refuse the single most ordinary keyword bid change there is: setting one
    for the first time.

    The effective bid is the number the keyword is bidding right now, which is
    what a percentage should be measured against and what the person sees in
    the Google Ads UI.
    """
    effective = int(getattr(current, "effective_cpc_bid_micros", 0) or 0)
    return effective or int(getattr(current, "cpc_bid_micros", 0) or 0)


def recheck_keyword_bid(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    """The typo backstop, against the bid the keyword uses NOW."""
    new_units = _units_from(arguments, "new_max_cpc")
    if new_units is None:
        return PolicyVerdict.deny(
            "new_max_cpc is not a usable number, so this change cannot be "
            "checked against the bid limits."
        )
    if current is None:
        return PolicyVerdict.deny(
            "the keyword's current bid could not be established, so a "
            "percentage limit cannot be applied to this change."
        )
    if str(getattr(current, "status", "") or "").upper() == "REMOVED":
        return PolicyVerdict.deny(
            "this keyword is REMOVED. Removal is permanent in Google Ads, so "
            "its bid cannot be changed."
        )
    return evaluate_bid_change(
        policy,
        tier=tier,
        current_units=units_from_micros(keyword_bid_baseline_micros(current)),
        new_units=new_units,
    )


def update_campaign_verdict(current: Any, arguments: dict[str, Any]) -> PolicyVerdict:
    """The campaign exists, is not REMOVED, and the resulting dates cohere.

    The date check is here rather than only in the argument validator because
    it needs the campaign's CURRENT start date: setting just an end date is
    ordinary, and whether it is valid depends on a value only the account
    holds. Confirm re-reads, so a start date moved in the Google Ads UI
    between drafting and confirming is caught rather than rejected opaquely by
    Google.
    """
    if current is None:
        return PolicyVerdict.deny(
            "the campaign could not be read, so this change cannot be checked."
        )
    if str(getattr(current, "status", "") or "").upper() == "REMOVED":
        return PolicyVerdict.deny(
            "this campaign is REMOVED. Removal is permanent in Google Ads and "
            "a removed campaign cannot be edited."
        )

    end_date = arguments.get("end_date")
    if end_date is None or str(end_date).strip() == "":
        return PolicyVerdict.allow()

    start_date = arguments.get("start_date")
    if start_date is not None and str(start_date).strip() != "":
        # Both supplied: already compared by the argument validator.
        return PolicyVerdict.allow()

    result = validate_campaign_schedule(
        None, end_date, current_start=_date_part(getattr(current, "start_date_time", ""))
    )
    if not result.ok:
        return PolicyVerdict.deny(*result.as_messages())
    return PolicyVerdict.allow()


def recheck_update_campaign(
    policy: Policy,
    *,
    tier: Tier,
    arguments: dict[str, Any],
    current: Any,
    payload: dict[str, Any],
    spend_today: Decimal,
) -> PolicyVerdict:
    return update_campaign_verdict(current, arguments)


def _date_part(date_time: object) -> str:
    """The date out of a "yyyy-MM-dd HH:mm:ss" campaign timestamp."""
    return str(date_time or "").strip().split(" ", 1)[0]


def validate_create_campaign_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    """Shape of the arguments for a new campaign.

    There are no defaults here, deliberately. The server cannot see whether
    the person was asked for a bidding strategy or whether one was chosen for
    them, so the next best thing is to leave the model nothing safe to fall
    back on: every value must be supplied, and every value appears on the
    preview.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(validate_campaign_name(arguments.get("name")))
    result.extend(validate_bidding_strategy(arguments.get("bidding_strategy")))

    try:
        budget = coerce_units(arguments.get("daily_budget"), field="daily_budget")
    except MoneyError as exc:
        result.add("daily_budget", str(exc))
        return result
    if budget <= 0:
        result.add("daily_budget", f"a daily budget must be above zero, got {budget}")
    return result


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
    # No recheck and no read at CONFIRM time. A status change has no relative
    # rule to re-evaluate - there is no percentage and no amount - so what it
    # skips is the second gate pass, not the read. Drafting still reads the ad
    # group, because "ad group 449283710 -> PAUSED" is not something a person
    # can approve.
    "pause_ad_group": OperationChecks(validate=validate_ad_group_status_args),
    "enable_ad_group": OperationChecks(validate=validate_ad_group_status_args),
    # Keyword and ad status: the same shape one and two levels further down.
    # Drafting reads for the preview; confirm has nothing relative to re-check.
    "pause_keyword": OperationChecks(validate=validate_keyword_status_args),
    "enable_keyword": OperationChecks(validate=validate_keyword_status_args),
    "pause_ad": OperationChecks(validate=validate_ad_status_args),
    "enable_ad": OperationChecks(validate=validate_ad_status_args),
    # A bid IS relative, so this one re-reads - and reads a KEYWORD, which
    # takes two ids rather than one.
    "update_keyword_bid": OperationChecks(
        validate=validate_keyword_bid_args,
        recheck=recheck_keyword_bid,
        reads=ReadKind.KEYWORD,
        id_argument="criterion_id",
    ),
    # Re-reads because an end date supplied on its own is only meaningful
    # against the campaign's current start date, and that can move in the
    # Google Ads UI between drafting and confirming.
    "update_campaign": OperationChecks(
        validate=validate_update_campaign_args,
        recheck=recheck_update_campaign,
        reads=ReadKind.CAMPAIGN,
        id_argument="campaign_id",
    ),
    "update_campaign_budget": OperationChecks(
        validate=validate_budget_args,
        recheck=recheck_budget,
        reads=ReadKind.CAMPAIGN,
        id_argument="campaign_id",
        spend_delta=budget_spend_delta,
    ),
    "add_negative_keyword": OperationChecks(validate=validate_negative_keyword_args),
    # No recheck and no read: a new campaign is measured against nothing that
    # already exists, so there is no current state to re-establish at confirm.
    "create_campaign": OperationChecks(validate=validate_create_campaign_args),
    # An ad group DOES have current state to be measured against: the campaign
    # it goes in decides whether it needs a default bid, and whether a
    # SEARCH_STANDARD ad group belongs there at all. So unlike create_campaign
    # it pays for the second gate pass and the re-read.
    "create_ad_group": OperationChecks(
        validate=validate_create_ad_group_args,
        recheck=recheck_create_ad_group,
        reads=ReadKind.CAMPAIGN,
        id_argument="campaign_id",
    ),
    # Reads the campaign for the same reason: this change writes a
    # CAMPAIGN-level field (the presence setting) as well as the criteria, so
    # confirm has to re-establish that the field still holds the value the
    # preview was built from.
    #
    # A known gap, recorded rather than papered over: the campaign's EXISTING
    # location criteria are re-read at draft time but not at confirm. If
    # somebody adds the same location in the Google Ads UI in between, the
    # mutate is refused by Google as a duplicate, the plan is burnt and the
    # error is surfaced verbatim. That is a loud, all-or-nothing failure with
    # nothing half-applied, which is the failure mode this server is built
    # around - so it is accepted rather than paid for with a third ReadKind.
    "add_location_target": OperationChecks(
        validate=validate_location_target_args,
        recheck=recheck_location_target,
        reads=ReadKind.CAMPAIGN,
        id_argument="campaign_id",
    ),
    "add_keyword": OperationChecks(
        validate=validate_keyword_args,
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
    if checks.reads is ReadKind.KEYWORD:
        ad_group_id = str(arguments.get("ad_group_id") or "").strip()
        if not ad_group_id:
            return None
        return await reader.keyword_by_id(
            customer_id=customer_id,
            ad_group_id=ad_group_id,
            criterion_id=entity_id,
        )
    return await reader.ad_group_by_id(customer_id=customer_id, ad_group_id=entity_id)


__all__ = [
    "MANUAL_BIDDING_STRATEGIES",
    "OPERATIONS",
    "SpendDelta",
    "ad_group_campaign_verdict",
    "location_target_verdict",
    "recheck_location_target",
    "validate_location_target_args",
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
    "recheck_create_ad_group",
    "validate_bid_args",
    "validate_budget_args",
    "keyword_bid_baseline_micros",
    "recheck_keyword_bid",
    "recheck_update_campaign",
    "update_campaign_verdict",
    "validate_ad_group_status_args",
    "validate_ad_status_args",
    "validate_campaign_status_args",
    "validate_keyword_bid_args",
    "validate_keyword_status_args",
    "validate_update_campaign_args",
    "validate_create_ad_group_args",
    "validate_keyword_args",
    "validate_negative_keyword_args",
    "validate_rsa_args",
]
