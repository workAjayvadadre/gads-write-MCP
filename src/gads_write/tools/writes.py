"""Phase 4 write tools. They do not write.

Every tool here validates, asks the gate, reads the campaign's current state,
builds a preview a human can read, and parks the intent as a plan. Nothing in
this module touches ads/executor.py. The only thing that applies a plan is
tools/confirm.py, and it re-runs the entire gate first.

That split is the whole two-step design:

    pause_campaign(...)      -> plan_id + "Brand - Exact: ENABLED -> PAUSED"
    confirm_and_apply(id)    -> the mutation actually happens

A plan is intent, never a permission. It carries no authority of its own, so
a plan drafted while someone was an operator is refused once they are
demoted, and a plan drafted under looser limits is refused once they tighten.

Both tools sit at `operator`. That is a deliberate, slightly uncomfortable
choice worth stating: pausing and enabling are NOT symmetric. Pausing stops
spend and is always safe; enabling resumes it, and in Phase 4 it is the only
lever in the server that can cause money to be spent, with no amount attached
for the policy engine to check. It is at `operator` because an operator is by
definition trusted to run campaigns - but if that ever feels wrong, raise
`enable_campaign` to `lead` in tools/registry.py and nothing else changes.

A known gap, recorded rather than papered over: enabling a campaign does not
count towards the per-user daily spend ceiling. The ceiling sums positive
budget *deltas*, and enabling a campaign changes no budget - it unlocks an
existing one. Inventing a delta here (say, the campaign's daily budget) would
make the ceiling fire on a number nobody actually changed. The honest
position is that Phase 4's ceiling does not constrain enables; Phase 5, where
budgets become editable, is where that ceiling starts doing real work.

Python note for a TypeScript reader:
  Same closure pattern as tools/reads.py - dependencies are passed in, not
  reached for, so tests build these tools against fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from fastmcp.exceptions import ToolError

from ..ads.reads import AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..safety.plans import PlanError, PlanStore
from ..safety.policy import (
    Policy,
    PolicyStore,
    evaluate_bid_change,
    evaluate_budget_change,
    evaluate_match_type_against_bidding,
)
from ..safety.units import MoneyError, coerce_units, format_micros, format_units, to_micros
from ..safety.validators import (
    ValidationResult,
    validate_customer_id,
    validate_keyword_text,
    validate_match_type,
    validate_numeric_id,
    validate_rsa,
)

logger = logging.getLogger(__name__)

# tool name -> the status it will set. Mirrors
# ads/executor.py:CAMPAIGN_STATUS_OPERATIONS, which is the enforcing copy.
TOOL_TARGET_STATUS: dict[str, str] = {
    "pause_campaign": "PAUSED",
    "enable_campaign": "ENABLED",
}


def validate_campaign_status_args(arguments: dict[str, Any]) -> ValidationResult:
    """Validate the arguments of a campaign status change."""
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    return result


def _validate_budget_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    return result


def _validate_negative_keyword_args(
    policy: Policy, arguments: dict[str, Any]
) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    target = arguments.get("campaign_id") or arguments.get("ad_group_id") or ""
    result.extend(validate_numeric_id(target, field_name="target_id"))
    result.extend(validate_keyword_text(arguments.get("keyword_text", "")))
    result.extend(validate_match_type(arguments.get("match_type", "")))
    return result


def _validate_keyword_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    result.extend(validate_keyword_text(arguments.get("keyword_text", "")))
    result.extend(validate_match_type(arguments.get("match_type", "")))
    return result


def _validate_bid_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("ad_group_id", ""), field_name="ad_group_id")
    )
    return result


def _validate_rsa_args(policy: Policy, arguments: dict[str, Any]) -> ValidationResult:
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


# tool name -> the validator that re-checks its arguments at confirm time.
#
# Shared with tools/confirm.py on purpose: the checks run at draft time and
# the checks re-run at confirm time must be the same code, or a plan could be
# applied under rules it was never checked against. A tool missing from this
# table cannot be confirmed at all - see confirm.py, which fails closed.
REVALIDATORS: dict[str, Callable[[Policy, dict[str, Any]], ValidationResult]] = {
    "pause_campaign": lambda _policy, args: validate_campaign_status_args(args),
    "enable_campaign": lambda _policy, args: validate_campaign_status_args(args),
    "update_campaign_budget": _validate_budget_args,
    "add_negative_keyword": _validate_negative_keyword_args,
    "add_keyword": _validate_keyword_args,
    "update_ad_group_bid": _validate_bid_args,
    "create_responsive_search_ad": _validate_rsa_args,
}


def register_write_tools(
    mcp: Any,
    *,
    guard: Any,
    reader: AdsReader,
    policy_store: PolicyStore,
    plan_store: PlanStore,
    caller_provider: Callable[[], Any] = current_caller,
) -> None:
    """Define the draft-only write tools on `mcp`."""

    async def _draft_status_change(tool: str, customer_id: str, campaign_id: str) -> dict:
        target_status = TOOL_TARGET_STATUS[tool]
        customer_id = str(customer_id).strip()
        campaign_id = str(campaign_id).strip()
        arguments = {"customer_id": customer_id, "campaign_id": campaign_id}

        caller = caller_provider()
        decision = await guard.check(
            tool=tool,
            caller=caller,
            customer_id=customer_id,
            arguments=arguments,
            validate=lambda _policy: validate_campaign_status_args(arguments),
        )
        if not decision.allowed:
            raise ToolError(f"{decision.reason_text}. Nothing was changed.")

        # Read the current state. This is what turns the preview from "will
        # set PAUSED" into "ENABLED -> PAUSED", and it is also how a
        # non-existent campaign is caught before a plan_id is handed out.
        try:
            campaign = await reader.campaign_by_id(
                customer_id=customer_id, campaign_id=campaign_id
            )
        except AdsReadError as exc:
            raise ToolError(
                f"could not read campaign {campaign_id} in account {customer_id}: "
                f"{exc}. Nothing was changed."
            ) from exc

        if campaign is None:
            raise ToolError(
                f"No campaign {campaign_id} in account {customer_id}. "
                "Check the ID with get_campaign_performance. Nothing was changed."
            )

        if campaign.status == "REMOVED":
            # Removal is terminal in Google Ads - a removed campaign can
            # never be enabled again. Catching it here gives a clear message
            # instead of an opaque API rejection after a confirm.
            raise ToolError(
                f"Campaign {campaign.name!r} ({campaign_id}) is REMOVED. Removal "
                "is permanent in Google Ads and cannot be undone by this server. "
                "Nothing was changed."
            )

        if campaign.status == target_status:
            # Not an error, and deliberately not a plan. Issuing a plan_id to
            # perform a no-op would mean a confirmed, audited "change" that
            # changed nothing, which makes the audit log harder to read.
            return {
                "ok": True,
                "no_change_needed": True,
                "campaign_id": campaign.campaign_id,
                "campaign_name": campaign.name,
                "status": campaign.status,
                "message": (
                    f"Campaign {campaign.name!r} is already {target_status}. "
                    "Nothing to do."
                ),
            }

        policy: Policy = decision.policy or policy_store.current()
        preview = _preview(
            customer_id=customer_id,
            campaign=campaign,
            target_status=target_status,
            currency_code=policy.currency_code,
        )

        try:
            plan = plan_store.draft(
                owner_email=caller.email,
                tool=tool,
                customer_id=customer_id,
                arguments=arguments,
                preview=preview,
                ttl_seconds=policy.plan_ttl_seconds,
                # No spend delta. See the module docstring: enabling a
                # campaign changes no budget, so there is no honest number to
                # put here and the daily ceiling does not apply.
                spend_delta_units=None,
                metadata={
                    "current_status": campaign.status,
                    "target_status": target_status,
                    "campaign_name": campaign.name,
                    "operation": tool,
                    "payload": {"campaign_id": campaign_id},
                },
            )
        except PlanError as exc:
            raise ToolError(str(exc)) from exc

        summary = plan.summary()
        summary.update(
            ok=True,
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.name,
            current_status=campaign.status,
            target_status=target_status,
        )
        return summary

    # ------------------------------------------------------------------
    # shared gate helpers for tools that need current state
    # ------------------------------------------------------------------
    # Budgets, bids and keywords cannot have their policy checked until we
    # know the CURRENT value, and we must not read an account before the gate
    # has authorised it. So these tools gate twice:
    #
    #   1. authorise  tier, kill switch, account allowlist, argument shape.
    #                 Marked dry_run so the audit line reads as "looked",
    #                 not "changed".
    #   2. decide     the same chain again, now with the numbers, running
    #                 the policy evaluation and the daily spend ceiling.
    #
    # Two audit lines per draft is deliberate. They record two genuinely
    # different checkpoints, and the alternative - reading the account before
    # the allowlist has been checked - is the thing the allowlist exists to
    # prevent.

    async def _authorise(
        tool: str, customer_id: str, arguments: dict[str, Any]
    ) -> Any:
        caller = caller_provider()
        decision = await guard.check(
            tool=tool,
            caller=caller,
            customer_id=customer_id,
            arguments=arguments,
            dry_run=True,
        )
        if not decision.allowed:
            raise ToolError(f"{decision.reason_text}. Nothing was changed.")
        return caller, decision

    async def _decide(
        tool: str,
        customer_id: str,
        arguments: dict[str, Any],
        *,
        validate: Callable[[Policy], ValidationResult],
        evaluate: Callable[[Policy, Any, Decimal], Any] | None = None,
        spend_delta_units: Decimal | None = None,
    ) -> Any:
        caller = caller_provider()
        decision = await guard.check(
            tool=tool,
            caller=caller,
            customer_id=customer_id,
            arguments=arguments,
            validate=validate,
            evaluate=evaluate,
            spend_delta_units=spend_delta_units,
        )
        if not decision.allowed:
            raise ToolError(f"{decision.reason_text}. Nothing was changed.")
        return decision

    async def _campaign_or_fail(customer_id: str, campaign_id: str) -> Any:
        try:
            campaign = await reader.campaign_by_id(
                customer_id=customer_id, campaign_id=campaign_id
            )
        except AdsReadError as exc:
            raise ToolError(
                f"could not read campaign {campaign_id} in account {customer_id}: "
                f"{exc}. Nothing was changed."
            ) from exc
        if campaign is None:
            raise ToolError(
                f"No campaign {campaign_id} in account {customer_id}. "
                "Check the ID with get_campaign_performance. Nothing was changed."
            )
        return campaign

    async def _ad_group_or_fail(customer_id: str, ad_group_id: str) -> Any:
        try:
            ad_group = await reader.ad_group_by_id(
                customer_id=customer_id, ad_group_id=ad_group_id
            )
        except AdsReadError as exc:
            raise ToolError(
                f"could not read ad group {ad_group_id} in account {customer_id}: "
                f"{exc}. Nothing was changed."
            ) from exc
        if ad_group is None:
            raise ToolError(
                f"No ad group {ad_group_id} in account {customer_id}. "
                "Nothing was changed."
            )
        return ad_group

    def _park(
        *,
        caller: Any,
        tool: str,
        customer_id: str,
        arguments: dict[str, Any],
        preview: str,
        policy: Policy,
        spend_delta_units: Decimal | None = None,
        metadata: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict:
        try:
            plan = plan_store.draft(
                owner_email=caller.email,
                tool=tool,
                customer_id=customer_id,
                arguments=arguments,
                preview=preview,
                ttl_seconds=policy.plan_ttl_seconds,
                spend_delta_units=(
                    str(spend_delta_units) if spend_delta_units is not None else None
                ),
                metadata=dict(metadata or {}),
            )
        except PlanError as exc:
            raise ToolError(str(exc)) from exc
        summary = plan.summary()
        summary["ok"] = True
        summary.update(extra or {})
        return summary

    # ------------------------------------------------------------------
    # budgets
    # ------------------------------------------------------------------

    @mcp.tool
    async def update_campaign_budget(
        customer_id: str, campaign_id: str, new_daily_budget: float
    ) -> dict:
        """Draft a change to a campaign's daily budget. Does NOT change it.

        `new_daily_budget` is in whole currency units (rupees), never micros.
        Returns a preview and a plan_id; nothing changes until
        confirm_and_apply. Subject to your tier's minimum, maximum, maximum
        percentage increase, and your running daily total.
        """
        customer_id = str(customer_id).strip()
        campaign_id = str(campaign_id).strip()
        arguments = {
            "customer_id": customer_id,
            "campaign_id": campaign_id,
            "new_daily_budget": str(new_daily_budget),
        }

        caller, _ = await _authorise("update_campaign_budget", customer_id, arguments)
        campaign = await _campaign_or_fail(customer_id, campaign_id)

        if campaign.budget_is_shared:
            # A shared budget is used by several campaigns, so changing it
            # would affect all of them while the preview names only one.
            # A misleading preview breaks the entire approval model, so this
            # is refused rather than warned about.
            raise ToolError(
                f"Campaign {campaign.name!r} uses a SHARED budget "
                f"({campaign.budget_resource_name}), which other campaigns also "
                "use. Changing it would affect them too, and this preview can "
                "only describe one campaign. Change it in the Google Ads UI, or "
                "give this campaign its own budget. Nothing was changed."
            )
        if not campaign.budget_resource_name:
            raise ToolError(
                f"Campaign {campaign.name!r} has no readable budget resource. "
                "Nothing was changed."
            )

        current_units = Decimal(campaign.daily_budget_micros) / Decimal(1_000_000)
        try:
            new_units = coerce_units(new_daily_budget, field="new_daily_budget")
        except MoneyError as exc:
            raise ToolError(f"{exc}. Nothing was changed.") from exc

        delta = new_units - current_units
        spend_delta = delta if delta > 0 else None

        def validate(policy: Policy) -> ValidationResult:
            return _validate_budget_args(policy, arguments)

        def evaluate(policy: Policy, tier: Any, spend_today: Decimal) -> Any:
            return evaluate_budget_change(
                policy,
                tier=tier,
                current_units=current_units,
                new_units=new_units,
                already_increased_today_units=spend_today,
            )

        decision = await _decide(
            "update_campaign_budget",
            customer_id,
            arguments,
            validate=validate,
            evaluate=evaluate,
            spend_delta_units=spend_delta,
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        direction = "increase" if delta > 0 else "decrease"
        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"Campaign {campaign.name!r} (id {campaign.campaign_id})",
                f"  budget id    : {campaign.budget_id}",
                f"  daily budget : {format_micros(campaign.daily_budget_micros, code)}"
                f" -> {format_units(new_units, code)}"
                f"   ({direction} of {format_units(abs(delta), code)})",
                f"  your total increase today would become "
                f"{format_units(decision.spend_today_units + (spend_delta or 0), code)}",
            ]
        )

        return _park(
            caller=caller,
            tool="update_campaign_budget",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            spend_delta_units=spend_delta,
            metadata={
                "campaign_name": campaign.name,
                "current_budget_micros": campaign.daily_budget_micros,
                "operation": "update_campaign_budget",
                "payload": {
                    "budget_resource_name": campaign.budget_resource_name,
                    "amount_micros": int(to_micros(new_units)),
                },
            },
            extra={
                "campaign_id": campaign.campaign_id,
                "campaign_name": campaign.name,
                "current_daily_budget": format_micros(campaign.daily_budget_micros, code),
                "new_daily_budget": format_units(new_units, code),
            },
        )

    # ------------------------------------------------------------------
    # negative keywords
    # ------------------------------------------------------------------

    @mcp.tool
    async def add_negative_keyword(
        customer_id: str,
        keyword_text: str,
        match_type: str,
        campaign_id: str | None = None,
        ad_group_id: str | None = None,
    ) -> dict:
        """Draft a negative keyword, excluding a search term. Does NOT add it.

        Give exactly one of `campaign_id` (excludes across the whole campaign)
        or `ad_group_id` (excludes within one ad group). `match_type` is
        EXACT, PHRASE or BROAD. Pass the bare keyword text - no brackets or
        quotes; the match type is a separate field.

        Negative keywords only ever reduce spend, but they are still drafted
        and confirmed like every other change.
        """
        customer_id = str(customer_id).strip()
        match_type = str(match_type).strip().upper()
        keyword_text = str(keyword_text).strip()

        if bool(campaign_id) == bool(ad_group_id):
            raise ToolError(
                "give exactly one of campaign_id or ad_group_id: a negative "
                "keyword applies either to a whole campaign or to one ad group. "
                "Nothing was changed."
            )

        at_campaign = bool(campaign_id)
        tool = "add_negative_keyword"
        target_id = str(campaign_id or ad_group_id).strip()
        arguments = {
            "customer_id": customer_id,
            "keyword_text": keyword_text,
            "match_type": match_type,
            "campaign_id": str(campaign_id).strip() if campaign_id else None,
            "ad_group_id": str(ad_group_id).strip() if ad_group_id else None,
        }

        caller, _ = await _authorise(tool, customer_id, arguments)

        if at_campaign:
            target = await _campaign_or_fail(customer_id, target_id)
            where = f"campaign {target.name!r} (id {target.campaign_id})"
        else:
            target = await _ad_group_or_fail(customer_id, target_id)
            where = (
                f"ad group {target.name!r} (id {target.ad_group_id}) "
                f"in campaign {target.campaign_name!r}"
            )

        def validate(policy: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_numeric_id(target_id, field_name="target_id"))
            result.extend(validate_keyword_text(keyword_text))
            result.extend(validate_match_type(match_type))
            return result

        decision = await _decide(tool, customer_id, arguments, validate=validate)
        policy = decision.policy or policy_store.current()

        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"Add NEGATIVE keyword to {where}",
                f"  keyword    : {keyword_text}",
                f"  match type : {match_type}",
                "  Effect: stops ads showing for searches this matches. "
                "Reduces spend; never increases it.",
            ]
        )

        return _park(
            caller=caller,
            tool=tool,
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            metadata={
                "level": "campaign" if at_campaign else "ad_group",
                # Two different Google Ads resources, so two executor
                # operations behind one tool.
                "operation": (
                    "add_campaign_negative_keyword"
                    if at_campaign
                    else "add_ad_group_negative_keyword"
                ),
                "payload": {
                    ("campaign_id" if at_campaign else "ad_group_id"): target_id,
                    "keyword_text": keyword_text,
                    "match_type": match_type,
                },
            },
            extra={"level": "campaign" if at_campaign else "ad_group"},
        )

    # ------------------------------------------------------------------
    # keywords
    # ------------------------------------------------------------------

    @mcp.tool
    async def add_keyword(
        customer_id: str, ad_group_id: str, keyword_text: str, match_type: str
    ) -> dict:
        """Draft a new positive keyword in an ad group. Does NOT add it.

        Created PAUSED when policy says new entities start paused, so it
        cannot spend until a human enables it. Broad match is refused on
        manual-CPC campaigns. Pass the bare keyword text - no brackets or
        quotes.
        """
        customer_id = str(customer_id).strip()
        ad_group_id = str(ad_group_id).strip()
        match_type = str(match_type).strip().upper()
        keyword_text = str(keyword_text).strip()
        arguments = {
            "customer_id": customer_id,
            "ad_group_id": ad_group_id,
            "keyword_text": keyword_text,
            "match_type": match_type,
        }

        caller, _ = await _authorise("add_keyword", customer_id, arguments)
        ad_group = await _ad_group_or_fail(customer_id, ad_group_id)

        def validate(policy: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_numeric_id(ad_group_id, field_name="ad_group_id"))
            result.extend(validate_keyword_text(keyword_text))
            result.extend(validate_match_type(match_type))
            return result

        def evaluate(policy: Policy, tier: Any, spend_today: Decimal) -> Any:
            # Broad match under manual CPC is the classic way to burn money.
            return evaluate_match_type_against_bidding(
                policy,
                match_type=match_type,
                bidding_strategy=ad_group.bidding_strategy_type,
            )

        decision = await _decide(
            "add_keyword", customer_id, arguments, validate=validate, evaluate=evaluate
        )
        policy = decision.policy or policy_store.current()
        status = "PAUSED" if policy.rules.new_entities_start_paused else "ENABLED"

        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"Add keyword to ad group {ad_group.name!r} (id {ad_group.ad_group_id})",
                f"  campaign   : {ad_group.campaign_name}",
                f"  keyword    : {keyword_text}",
                f"  match type : {match_type}",
                f"  created as : {status}",
                (
                    "  It cannot spend until someone enables it."
                    if status == "PAUSED"
                    else "  WARNING: this keyword will be live immediately."
                ),
            ]
        )

        return _park(
            caller=caller,
            tool="add_keyword",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            metadata={
                "status": status,
                "operation": "add_keyword",
                "payload": {
                    "ad_group_id": ad_group_id,
                    "keyword_text": keyword_text,
                    "match_type": match_type,
                    "status": status,
                },
            },
            extra={"created_status": status},
        )

    # ------------------------------------------------------------------
    # bids
    # ------------------------------------------------------------------

    @mcp.tool
    async def update_ad_group_bid(
        customer_id: str, ad_group_id: str, new_max_cpc: float
    ) -> dict:
        """Draft a change to an ad group's max CPC bid. Does NOT change it.

        `new_max_cpc` is in whole currency units (rupees), never micros.
        Subject to your tier's max CPC and maximum percentage increase.
        """
        customer_id = str(customer_id).strip()
        ad_group_id = str(ad_group_id).strip()
        arguments = {
            "customer_id": customer_id,
            "ad_group_id": ad_group_id,
            "new_max_cpc": str(new_max_cpc),
        }

        caller, _ = await _authorise("update_ad_group_bid", customer_id, arguments)
        ad_group = await _ad_group_or_fail(customer_id, ad_group_id)

        current_units = Decimal(ad_group.cpc_bid_micros) / Decimal(1_000_000)
        try:
            new_units = coerce_units(new_max_cpc, field="new_max_cpc")
        except MoneyError as exc:
            raise ToolError(f"{exc}. Nothing was changed.") from exc

        def validate(policy: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_numeric_id(ad_group_id, field_name="ad_group_id"))
            return result

        def evaluate(policy: Policy, tier: Any, spend_today: Decimal) -> Any:
            return evaluate_bid_change(
                policy, tier=tier, current_units=current_units, new_units=new_units
            )

        decision = await _decide(
            "update_ad_group_bid",
            customer_id,
            arguments,
            validate=validate,
            evaluate=evaluate,
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"Ad group {ad_group.name!r} (id {ad_group.ad_group_id})",
                f"  campaign : {ad_group.campaign_name}",
                f"  max CPC  : {format_micros(ad_group.cpc_bid_micros, code)}"
                f" -> {format_units(new_units, code)}",
                "  This changes what you pay per click, not the daily budget.",
            ]
        )

        return _park(
            caller=caller,
            tool="update_ad_group_bid",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            metadata={
                "current_cpc_bid_micros": ad_group.cpc_bid_micros,
                "operation": "update_ad_group_bid",
                "payload": {
                    "ad_group_id": ad_group_id,
                    "cpc_bid_micros": int(to_micros(new_units)),
                },
            },
            extra={
                "ad_group_id": ad_group.ad_group_id,
                "current_max_cpc": format_micros(ad_group.cpc_bid_micros, code),
                "new_max_cpc": format_units(new_units, code),
            },
        )

    # ------------------------------------------------------------------
    # responsive search ads
    # ------------------------------------------------------------------

    @mcp.tool
    async def create_responsive_search_ad(
        customer_id: str,
        ad_group_id: str,
        headlines: list[str],
        descriptions: list[str],
        final_urls: list[str],
        path1: str | None = None,
        path2: str | None = None,
    ) -> dict:
        """Draft a new responsive search ad. Does NOT create it.

        Needs 3-15 headlines of at most 30 characters and 2-4 descriptions of
        at most 90. Final URLs must be https and on an allowlisted domain.
        Created PAUSED when policy says new entities start paused.
        """
        customer_id = str(customer_id).strip()
        ad_group_id = str(ad_group_id).strip()
        headlines = [str(h) for h in (headlines or [])]
        descriptions = [str(d) for d in (descriptions or [])]
        final_urls = [str(u) for u in (final_urls or [])]
        arguments = {
            "customer_id": customer_id,
            "ad_group_id": ad_group_id,
            "headlines": headlines,
            "descriptions": descriptions,
            "final_urls": final_urls,
            "path1": path1,
            "path2": path2,
        }

        caller, _ = await _authorise(
            "create_responsive_search_ad", customer_id, arguments
        )
        ad_group = await _ad_group_or_fail(customer_id, ad_group_id)

        def validate(policy: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_numeric_id(ad_group_id, field_name="ad_group_id"))
            result.extend(
                validate_rsa(
                    headlines=headlines,
                    descriptions=descriptions,
                    final_urls=final_urls,
                    allowed_domains=policy.rules.allowed_final_url_domains,
                    path1=path1,
                    path2=path2,
                )
            )
            return result

        decision = await _decide(
            "create_responsive_search_ad", customer_id, arguments, validate=validate
        )
        policy = decision.policy or policy_store.current()
        status = "PAUSED" if policy.rules.new_entities_start_paused else "ENABLED"

        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"New responsive search ad in ad group {ad_group.name!r} "
                f"(id {ad_group.ad_group_id})",
                f"  campaign   : {ad_group.campaign_name}",
                f"  created as : {status}",
                f"  final URLs : {', '.join(final_urls)}",
                f"  headlines ({len(headlines)}):",
                *[f"      - {h}" for h in headlines],
                f"  descriptions ({len(descriptions)}):",
                *[f"      - {d}" for d in descriptions],
                *([f"  paths      : /{path1}" + (f"/{path2}" if path2 else "")] if path1 else []),
            ]
        )

        return _park(
            caller=caller,
            tool="create_responsive_search_ad",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            metadata={
                "status": status,
                "operation": "create_responsive_search_ad",
                "payload": {
                    "ad_group_id": ad_group_id,
                    "headlines": headlines,
                    "descriptions": descriptions,
                    "final_urls": final_urls,
                    "path1": path1,
                    "path2": path2,
                    "status": status,
                },
            },
            extra={"created_status": status, "ad_group_id": ad_group.ad_group_id},
        )

    @mcp.tool
    async def pause_campaign(customer_id: str, campaign_id: str) -> dict:
        """Draft a change that pauses one campaign. Does NOT pause it.

        Returns a preview and a plan_id. The campaign is only paused when
        confirm_and_apply is called with that plan_id, which is a separate
        step on purpose. Pausing is reversible with enable_campaign.
        """
        return await _draft_status_change("pause_campaign", customer_id, campaign_id)

    @mcp.tool
    async def enable_campaign(customer_id: str, campaign_id: str) -> dict:
        """Draft a change that enables one campaign. Does NOT enable it.

        Returns a preview and a plan_id; nothing happens until
        confirm_and_apply is called. Note that enabling a campaign lets it
        start spending its daily budget again.
        """
        return await _draft_status_change("enable_campaign", customer_id, campaign_id)


def _preview(
    *,
    customer_id: str,
    campaign: Any,
    target_status: str,
    currency_code: str,
) -> str:
    """The text a human approves. Plain, specific, and about one thing."""
    budget = format_micros(campaign.daily_budget_micros, currency_code)
    lines = [
        f"Account {customer_id}",
        f"Campaign {campaign.name!r} (id {campaign.campaign_id})",
        f"  channel      : {campaign.channel_type}",
        f"  daily budget : {budget}",
        f"  status       : {campaign.status} -> {target_status}",
    ]
    if target_status == "ENABLED":
        lines.append(
            f"  NOTE: enabling this campaign lets it spend up to {budget} per day."
        )
    else:
        lines.append("  This is reversible with enable_campaign.")
    return "\n".join(lines)


__all__ = [
    "register_write_tools",
    "validate_campaign_status_args",
    "REVALIDATORS",
    "TOOL_TARGET_STATUS",
]
