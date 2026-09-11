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

from ..ads.executor import POSITIVE_GEO_TARGET_TYPE
from ..ads.reads import AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..safety.plans import PlanError, PlanStore
from ..safety.policy import (
    broad_match_warning,
    Policy,
    PolicyStore,
)
from ..safety.units import MoneyError, coerce_units, format_micros, format_units, to_micros
from ..safety.validators import ValidationResult
from .operations import (
    OPERATIONS,
    ad_group_campaign_verdict,
    budget_spend_delta,
    location_target_verdict,
    recheck_bid,
    recheck_budget,
    recheck_create_ad_group,
    recheck_location_target,
    shared_budget_verdict,
    units_from_micros,
    validate_campaign_status_args,
)

from .registry import annotations_for

logger = logging.getLogger(__name__)

# tool name -> the status it will set. Mirrors
# ads/executor.py:CAMPAIGN_STATUS_OPERATIONS and AD_GROUP_STATUS_OPERATIONS,
# which are the enforcing copies.
#
# The reason this is a TABLE rather than a parameter, stated once for all four
# tools: CampaignStatus and AdGroupStatus both have a REMOVED member, removal
# is terminal in Google Ads, and this server has no remove tool by design. A
# status that arrives as input is one typo away from being one. Here it cannot
# arrive as input at all.
TOOL_TARGET_STATUS: dict[str, str] = {
    "pause_campaign": "PAUSED",
    "enable_campaign": "ENABLED",
    "pause_ad_group": "PAUSED",
    "enable_ad_group": "ENABLED",
}


# Every validator and every policy recheck now lives in tools/operations.py,
# so that the checks this module runs at draft time and the checks
# tools/confirm.py re-runs at confirm time are literally the same functions.
# They used to be two sets, and the confirm-time set was the weaker one.


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
            validate=lambda policy: validate_campaign_status_args(policy, arguments),
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

    async def _draft_ad_group_status_change(
        tool: str, customer_id: str, ad_group_id: str
    ) -> dict:
        """The ad group twin of `_draft_status_change`.

        Deliberately a parallel function rather than a generalisation of it.
        The two share a shape but not a preview: a campaign preview shows a
        daily budget, an ad group preview shows its campaign's status and its
        max CPC. Folding them together would mean a table of formatting
        strategies, which is more indirection than two short functions.

        One gate pass, like the campaign pair. A status change has no relative
        rule to evaluate, so there is no second pass - but it still READS,
        because "ad group 449283710 -> PAUSED" is not something a human can
        approve.
        """
        target_status = TOOL_TARGET_STATUS[tool]
        customer_id = str(customer_id).strip()
        ad_group_id = str(ad_group_id).strip()
        arguments = {"customer_id": customer_id, "ad_group_id": ad_group_id}

        caller = caller_provider()
        decision = await guard.check(
            tool=tool,
            caller=caller,
            customer_id=customer_id,
            arguments=arguments,
            validate=lambda policy: OPERATIONS[tool].validate(policy, arguments),
        )
        if not decision.allowed:
            raise ToolError(f"{decision.reason_text}. Nothing was changed.")

        ad_group = await _ad_group_or_fail(customer_id, ad_group_id)

        if ad_group.status == "REMOVED":
            raise ToolError(
                f"Ad group {ad_group.name!r} ({ad_group_id}) is REMOVED. Removal "
                "is permanent in Google Ads and cannot be undone by this "
                "server. Nothing was changed."
            )

        if ad_group.status == target_status:
            # Not an error, and deliberately not a plan - the pause_campaign
            # rule. A confirmed, audited change that changed nothing makes the
            # audit log harder to read.
            return {
                "ok": True,
                "no_change_needed": True,
                "ad_group_id": ad_group.ad_group_id,
                "ad_group_name": ad_group.name,
                "campaign_name": ad_group.campaign_name,
                "status": ad_group.status,
                "message": (
                    f"Ad group {ad_group.name!r} is already {target_status}. "
                    "Nothing to do."
                ),
            }

        policy: Policy = decision.policy or policy_store.current()
        preview = _ad_group_preview(
            customer_id=customer_id,
            ad_group=ad_group,
            target_status=target_status,
            currency_code=policy.currency_code,
        )

        return _park(
            caller=caller,
            tool=tool,
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            # No spend delta. Pausing and enabling an ad group move no daily
            # budget - the campaign's budget is unchanged either way - so
            # there is no honest number to charge against the running total
            # the budget previews report.
            spend_delta_units=None,
            metadata={
                "current_status": ad_group.status,
                "target_status": target_status,
                "ad_group_name": ad_group.name,
                "campaign_name": ad_group.campaign_name,
                "operation": tool,
                "payload": {"ad_group_id": ad_group_id},
            },
            extra={
                "ad_group_id": ad_group.ad_group_id,
                "ad_group_name": ad_group.name,
                "campaign_name": ad_group.campaign_name,
                "current_status": ad_group.status,
                "target_status": target_status,
            },
        )

    # ------------------------------------------------------------------
    # shared gate helpers for tools that need current state
    # ------------------------------------------------------------------
    # Budgets, bids and keywords cannot have their policy checked until we
    # know the CURRENT value, and we must not read an account before the gate
    # has authorised it. So these tools gate twice:
    #
    #   1. authorise  tier, kill switch, managed account, argument shape.
    #                 Marked dry_run so the audit line reads as "looked",
    #                 not "changed".
    #   2. decide     the same chain again, now with the numbers, running
    #                 the policy evaluation and the daily spend ceiling.
    #
    # Two audit lines per draft is deliberate. They record two genuinely
    # different checkpoints, and the alternative - reading the account before
    # the account has been checked - is the thing that check exists to
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

    # These two adapt the shared checks in tools/operations.py to the shapes
    # Guard.check wants. They exist so that a draft cannot accidentally run a
    # different check from the one confirm will re-run: there is one
    # definition per tool, and both steps go through it.

    def _validator(tool: str, arguments: dict[str, Any]) -> Callable[[Policy], ValidationResult]:
        validate = OPERATIONS[tool].validate
        return lambda policy: validate(policy, arguments)

    def _evaluator(
        recheck: Any,
        arguments: dict[str, Any],
        *,
        current: Any,
        payload: dict[str, Any] | None = None,
    ) -> Callable[[Policy, Any, Decimal], Any]:
        return lambda policy, tier, spend_today: recheck(
            policy,
            tier=tier,
            arguments=arguments,
            current=current,
            payload=dict(payload or {}),
            spend_today=spend_today,
        )

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

    @mcp.tool(annotations=annotations_for('update_campaign_budget'))
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

        # Refused here as well as inside the recheck, so whoever is drafting
        # gets the explanation before a plan_id exists rather than a bare
        # policy denial. Same function confirm runs, so the two cannot drift.
        shared = shared_budget_verdict(campaign)
        if not shared.allowed:
            raise ToolError(
                f"Campaign {campaign.name!r}: {' '.join(shared.reasons)} "
                "Nothing was changed."
            )
        if not campaign.budget_resource_name:
            raise ToolError(
                f"Campaign {campaign.name!r} has no readable budget resource. "
                "Nothing was changed."
            )

        current_units = units_from_micros(campaign.daily_budget_micros)
        try:
            new_units = coerce_units(new_daily_budget, field="new_daily_budget")
        except MoneyError as exc:
            raise ToolError(f"{exc}. Nothing was changed.") from exc

        delta = new_units - current_units
        # Built BEFORE the gate, because the recheck has to compare the
        # resource we would change against the one the campaign actually
        # uses. Confirm rebuilds neither: it re-reads the campaign and runs
        # the same comparison against the payload stored on the plan.
        payload = {
            "budget_resource_name": campaign.budget_resource_name,
            "amount_micros": int(to_micros(new_units)),
        }
        spend_delta = budget_spend_delta(arguments, campaign)

        # The same functions tools/confirm.py will re-run, against the
        # campaign as it is right now. `campaign` is what makes the percentage
        # limits meaningful; confirm re-reads it rather than trusting this one.
        decision = await _decide(
            "update_campaign_budget",
            customer_id,
            arguments,
            validate=_validator("update_campaign_budget", arguments),
            evaluate=_evaluator(
                recheck_budget, arguments, current=campaign, payload=payload
            ),
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
                # Information, not a limit. This used to REFUSE past a
                # percentage of the account's total, which blocked ordinary
                # work - raising five campaigns for a seasonal push stopped
                # after the second. The number is still worth seeing, so the
                # person approving gets it and decides, which is more than
                # the Google Ads UI gives them.
                f"  your budget increases today would total "
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
                # The same object the gate just judged, not a second one built
                # from the same variables. Two derivations can drift.
                "payload": payload,
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

    @mcp.tool(annotations=annotations_for('add_negative_keyword'))
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

        decision = await _decide(
            tool, customer_id, arguments, validate=_validator(tool, arguments)
        )
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

    @mcp.tool(annotations=annotations_for('add_keyword'))
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

        # Broad match under manual CPC used to be refused outright. The Google
        # Ads UI allows it, so this server does too - the risk is spelled out
        # on the preview and the person approving decides. See
        # safety/policy.py for why refusing it stopped being the right call.
        decision = await _decide(
            "add_keyword",
            customer_id,
            arguments,
            validate=_validator("add_keyword", arguments),
        )
        policy = decision.policy or policy_store.current()
        status = "PAUSED" if policy.rules.new_entities_start_paused else "ENABLED"

        lines = [
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
        warning = broad_match_warning(
            match_type=match_type,
            bidding_strategy=getattr(ad_group, "bidding_strategy_type", ""),
        )
        if warning:
            lines.append(f"  {warning}")
        preview = "\n".join(lines)

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
    # create_campaign
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("create_campaign"))
    async def create_campaign(
        customer_id: str,
        name: str,
        daily_budget: float,
        bidding_strategy: str,
    ) -> dict:
        """Draft a new Search campaign. Does NOT create it.

        Every value is required and nothing is assumed - ASK THE PERSON for
        the name, the budget and the bidding strategy rather than choosing
        any of them yourself.

        `bidding_strategy` is MANUAL_CPC (you set bids yourself) or
        MAXIMIZE_CLICKS (Google spends the budget on clicks).
        `daily_budget` is in whole currency units, never micros.

        The campaign is created PAUSED, on Google Search only, with its own
        budget. It will have no ad groups, keywords or ads, so it cannot
        spend until someone builds it out. Locations, languages and schedules
        are set afterwards in the Google Ads UI.

        Returns a preview and a plan_id; nothing is created until
        confirm_and_apply.
        """
        customer_id = str(customer_id).strip()
        name = str(name).strip()
        bidding_strategy = str(bidding_strategy).strip().upper()

        arguments = {
            "customer_id": customer_id,
            "name": name,
            "daily_budget": str(daily_budget),
            "bidding_strategy": bidding_strategy,
        }

        caller, _ = await _authorise("create_campaign", customer_id, arguments)

        try:
            budget_units = coerce_units(daily_budget, field="daily_budget")
        except MoneyError as exc:
            raise ToolError(f"{exc}. Nothing was created.") from exc

        decision = await _decide(
            "create_campaign",
            customer_id,
            arguments,
            validate=_validator("create_campaign", arguments),
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        strategy_label = {
            "MANUAL_CPC": "Manual CPC - you set the bids",
            "MAXIMIZE_CLICKS": "Maximize Clicks - Google spends the budget on clicks",
        }.get(bidding_strategy, bidding_strategy)

        # Everything this creates, and everything it deliberately does not.
        # A preview that listed only the three chosen values would be lying by
        # omission: the fixed settings are decisions too, and the reader has
        # no way to know them otherwise.
        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"CREATE a new campaign {name!r}",
                f"  daily budget     : {format_units(budget_units, code)}",
                f"  bidding          : {strategy_label}",
                f"  status           : PAUSED",
                f"  type             : Search, Google Search only",
                f"                     (search partners, Display and YouTube off)",
                f"  budget           : its own, not shared with any campaign",
                f"  EU political ads : declared as NOT political advertising",
                "",
                "  It will have no ad groups, keywords or ads, so it cannot",
                "  spend anything. Add those, and set locations, languages and",
                "  schedules, in the Google Ads UI.",
            ]
        )

        return _park(
            caller=caller,
            tool="create_campaign",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            metadata={
                "operation": "create_campaign",
                "payload": {
                    "name": name,
                    "budget_micros": to_micros(budget_units),
                    "bidding_strategy": bidding_strategy,
                },
            },
            extra={"created_status": "PAUSED"},
        )

    # ------------------------------------------------------------------
    # create_ad_group
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("create_ad_group"))
    async def create_ad_group(
        customer_id: str,
        campaign_id: str,
        name: str,
        max_cpc: float | None = None,
    ) -> dict:
        """Draft a new ad group in an existing Search campaign. Does NOT create it.

        The ad group `add_keyword` and `create_responsive_search_ad` will
        target. `create_campaign` makes a campaign with none, so this is the
        step between the two.

        `max_cpc` is the ad group's default max CPC bid, in whole currency
        units, never micros. Whether to pass it is decided by the CAMPAIGN,
        not by you:

          Manual CPC campaign     max_cpc is REQUIRED. Without a default bid
                                  the ad group has nothing to bid with, and
                                  the Google Ads UI demands one here too.
          automated bidding       max_cpc must be OMITTED. Google sets the
                                  bids and ignores this field entirely.

        The ad group is created PAUSED and of type SEARCH_STANDARD. That type
        is immutable in Google Ads and cannot be changed afterwards, which is
        why this refuses any campaign that is not a Search campaign.

        Returns a preview and a plan_id; nothing is created until
        confirm_and_apply.
        """
        customer_id = str(customer_id).strip()
        campaign_id = str(campaign_id).strip()
        name = str(name).strip()
        arguments = {
            "customer_id": customer_id,
            "campaign_id": campaign_id,
            "name": name,
            # Plans are stored as JSON, so the amount travels as a string the
            # same way every other money argument here does.
            "max_cpc": None if max_cpc is None else str(max_cpc),
        }

        caller, _ = await _authorise("create_ad_group", customer_id, arguments)
        campaign = await _campaign_or_fail(customer_id, campaign_id)

        bid_units: Decimal | None = None
        if max_cpc is not None:
            try:
                bid_units = coerce_units(max_cpc, field="max_cpc")
            except MoneyError as exc:
                raise ToolError(f"{exc}. Nothing was created.") from exc

        # Read from the policy snapshot rather than the gate's decision
        # because the payload has to exist BEFORE the gate runs - the bid
        # check compares the payload against the campaign. `for_account` only
        # rebinds currency and timezone, so `rules` is the same either way.
        status = (
            "PAUSED"
            if policy_store.current().rules.new_entities_start_paused
            else "ENABLED"
        )
        # One payload object, built once, judged by the gate and then stored
        # on the plan. Two derivations from the same variables can drift.
        payload: dict[str, Any] = {
            "campaign_id": campaign_id,
            "name": name,
            "status": status,
        }
        if bid_units is not None:
            payload["cpc_bid_micros"] = int(to_micros(bid_units))

        # Refused here as well as inside the recheck, so whoever is drafting
        # gets the explanation before a plan_id exists rather than a bare
        # policy denial. Same function confirm re-runs, so the two cannot
        # drift.
        suitable = ad_group_campaign_verdict(campaign, payload)
        if not suitable.allowed:
            raise ToolError(
                f"Campaign {campaign.name!r} (id {campaign.campaign_id}): "
                f"{' '.join(suitable.reasons)} Nothing was created."
            )

        decision = await _decide(
            "create_ad_group",
            customer_id,
            arguments,
            validate=_validator("create_ad_group", arguments),
            evaluate=_evaluator(
                recheck_create_ad_group, arguments, current=campaign, payload=payload
            ),
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        bid_text = (
            format_units(bid_units, code)
            if bid_units is not None
            else f"none - {campaign.bidding_strategy_type} sets the bids for you"
        )
        preview = "\n".join(
            [
                f"Account {customer_id}",
                f"CREATE an ad group {name!r}",
                f"  campaign        : {campaign.name!r} (id {campaign.campaign_id})",
                f"  bidding         : {campaign.bidding_strategy_type}",
                f"  default max CPC : {bid_text}",
                f"  type            : SEARCH_STANDARD",
                f"  status          : {status}",
                "",
                "  An ad group's type is IMMUTABLE in Google Ads and there is no",
                "  tool here to remove one, so SEARCH_STANDARD is permanent.",
                "  It will have no keywords and no ads, so it cannot spend until",
                "  someone adds them.",
            ]
        )

        return _park(
            caller=caller,
            tool="create_ad_group",
            customer_id=customer_id,
            arguments=arguments,
            preview=preview,
            policy=policy,
            # No spend delta. A paused ad group with no keywords and no ads
            # spends nothing, and a max CPC is a price per click rather than a
            # daily amount - there is no honest number to charge against a
            # ceiling derived from daily budget increases.
            spend_delta_units=None,
            metadata={
                "campaign_name": campaign.name,
                "status": status,
                "operation": "create_ad_group",
                "payload": payload,
            },
            extra={
                "campaign_id": campaign.campaign_id,
                "ad_group_name": name,
                "created_status": status,
            },
        )

    # ------------------------------------------------------------------
    # location targeting
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("add_location_target"))
    async def add_location_target(
        customer_id: str, campaign_id: str, location_ids: list[str]
    ) -> dict:
        """Draft location targeting for a campaign. Does NOT apply it.

        A Search campaign with no location criteria serves EVERYWHERE, so this
        is usually the change that restricts a campaign rather than widening
        it - but adding a location to a campaign that already has some does
        widen it, and the preview lists both.

        `location_ids` are geo target constant ids from find_locations, never
        names. "Delhi" matches a city, a state and a union territory, so a
        name is a question; this tool will not answer it on your behalf.

        It also sets the campaign's location option to PRESENCE - people IN or
        regularly in the targeted places. Google's default additionally serves
        to anyone in the world merely searching ABOUT them, which would make
        "targeting India" untrue. That setting is campaign-level, so it is
        shown on the preview with its previous value.

        Returns a preview naming every location and a plan_id; nothing changes
        until confirm_and_apply.
        """
        customer_id = str(customer_id).strip()
        campaign_id = str(campaign_id).strip()
        wanted = [str(value).strip() for value in (location_ids or [])]
        arguments = {
            "customer_id": customer_id,
            "campaign_id": campaign_id,
            "location_ids": wanted,
        }

        caller, _ = await _authorise("add_location_target", customer_id, arguments)

        # Refused here rather than falling through to the "nothing to do"
        # branch below. An empty list is a malformed call, not a change that
        # happens to be unnecessary, and reporting it as the latter would tell
        # somebody their locations were already targeted when none were named.
        if not wanted:
            raise ToolError(
                "give at least one location id. Use find_locations to look one "
                "up - it returns the id to pass here. Nothing was changed."
            )

        campaign = await _campaign_or_fail(customer_id, campaign_id)

        # What the campaign already targets. Read before anything is drafted,
        # for two reasons: the mutate is all-or-nothing, so one duplicate
        # would fail the whole batch; and a preview offering to add a place
        # the campaign already targets is a preview that lies.
        try:
            existing = await reader.campaign_locations(
                customer_id=customer_id, campaign_id=campaign_id
            )
        except AdsReadError as exc:
            raise ToolError(
                f"could not read the locations campaign {campaign_id} already "
                f"targets: {exc}. Nothing was changed."
            ) from exc

        try:
            found = await reader.geo_targets_by_id(
                customer_id=customer_id, geo_target_ids=wanted
            )
        except AdsReadError as exc:
            raise ToolError(f"{exc}. Nothing was changed.") from exc

        by_id = {row.geo_target_id: row for row in found}
        missing = [value for value in wanted if value not in by_id]
        if missing:
            raise ToolError(
                f"Google has no enabled place with id(s) {missing}. Look them "
                "up with find_locations - it returns the id to use. Nothing "
                "was changed."
            )

        targeted = {row.geo_target_id for row in existing if not row.negative}
        excluded = {row.geo_target_id for row in existing if row.negative}

        clashing = [value for value in wanted if value in excluded]
        if clashing:
            # Adding a location positively while it is excluded is a
            # contradiction, and there is no tool here to remove the
            # exclusion, so this cannot be resolved from this server.
            names = ", ".join(by_id[value].describe() for value in clashing)
            raise ToolError(
                f"Campaign {campaign.name!r} currently EXCLUDES {names}. "
                "Targeting and excluding the same place is a contradiction, "
                "and this server has no tool to remove an exclusion - do that "
                "in the Google Ads UI first. Nothing was changed."
            )

        to_add = [value for value in wanted if value not in targeted]
        already = [value for value in wanted if value in targeted]

        if not to_add:
            # Not an error, and deliberately not a plan. The pause_campaign
            # rule: a confirmed, audited change that changed nothing makes the
            # audit log harder to read.
            names = ", ".join(by_id[value].describe() for value in already)
            return {
                "ok": True,
                "no_change_needed": True,
                "campaign_id": campaign.campaign_id,
                "campaign_name": campaign.name,
                "already_targeted": names,
                "message": (
                    f"Campaign {campaign.name!r} already targets {names}. "
                    "Nothing to do."
                ),
            }

        current_geo_type = campaign.positive_geo_target_type or ""
        # None means "leave the campaign record alone" - it is already
        # presence-only, so there is nothing to write and the mutate carries
        # criterion creates only.
        set_geo_type = (
            None if current_geo_type == POSITIVE_GEO_TARGET_TYPE
            else POSITIVE_GEO_TARGET_TYPE
        )
        payload: dict[str, Any] = {
            "campaign_id": campaign_id,
            "geo_target_constant_ids": to_add,
            "positive_geo_target_type": set_geo_type,
            # What the preview was built against. Confirm compares this to the
            # campaign as it is then: if somebody switched the setting back in
            # the Google Ads UI, the preview's promise no longer holds.
            "expected_positive_geo_target_type": current_geo_type,
        }

        suitable = location_target_verdict(campaign, payload)
        if not suitable.allowed:
            raise ToolError(
                f"Campaign {campaign.name!r} (id {campaign.campaign_id}): "
                f"{' '.join(suitable.reasons)} Nothing was changed."
            )

        decision = await _decide(
            "add_location_target",
            customer_id,
            arguments,
            validate=_validator("add_location_target", arguments),
            evaluate=_evaluator(
                recheck_location_target, arguments, current=campaign, payload=payload
            ),
        )
        policy = decision.policy or policy_store.current()

        # Every location named in full. A preview that said "3 locations"
        # would be asking someone to approve a number, not a decision.
        lines = [
            f"Account {customer_id}",
            f"Campaign {campaign.name!r} (id {campaign.campaign_id})",
            f"  ADD {len(to_add)} location target(s):",
            *[f"      + {by_id[value].describe()}" for value in to_add],
        ]
        if already:
            lines.append("  already targeted, not added again:")
            lines.extend(f"      = {by_id[value].describe()}" for value in already)

        others = sorted(
            row.display_name or row.geo_target_constant
            for row in existing
            if not row.negative and row.geo_target_id not in set(wanted)
        )
        if others:
            lines.append("  other locations this campaign already targets:")
            lines.extend(f"      . {name}" for name in others)
        elif not already:
            lines.append(
                "  This campaign had NO location targeting, so it was serving"
            )
            lines.append("  everywhere. After this it serves only where listed.")

        lines.append("")
        if set_geo_type is None:
            lines.append(
                "  who sees it : people IN or regularly in these locations"
            )
            lines.append("                (already set that way, unchanged)")
        else:
            lines.append(
                "  who sees it : people IN or regularly in these locations"
            )
            lines.append(
                f"                changed from {current_geo_type or 'unset'}, "
                "which also"
            )
            lines.append(
                "                served people elsewhere searching ABOUT them"
            )
        if excluded:
            lines.append(
                f"  NOTE: this campaign also excludes {len(excluded)} location(s), "
                "which this change does not touch."
            )

        return _park(
            caller=caller,
            tool="add_location_target",
            customer_id=customer_id,
            arguments=arguments,
            preview="\n".join(lines),
            policy=policy,
            # No spend delta. Location targeting moves no daily budget: it
            # changes WHERE the existing budget is spent, not how much.
            spend_delta_units=None,
            metadata={
                "campaign_name": campaign.name,
                "operation": "add_location_target",
                "payload": payload,
            },
            extra={
                "campaign_id": campaign.campaign_id,
                "locations_added": [by_id[value].describe() for value in to_add],
                "already_targeted": [by_id[value].describe() for value in already],
                "positive_geo_target_type": POSITIVE_GEO_TARGET_TYPE,
            },
        )

    # ------------------------------------------------------------------
    # bids
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for('update_ad_group_bid'))
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

        current_units = units_from_micros(ad_group.cpc_bid_micros)
        try:
            new_units = coerce_units(new_max_cpc, field="new_max_cpc")
        except MoneyError as exc:
            raise ToolError(f"{exc}. Nothing was changed.") from exc

        decision = await _decide(
            "update_ad_group_bid",
            customer_id,
            arguments,
            validate=_validator("update_ad_group_bid", arguments),
            evaluate=_evaluator(recheck_bid, arguments, current=ad_group),
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

    @mcp.tool(annotations=annotations_for('create_responsive_search_ad'))
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

        decision = await _decide(
            "create_responsive_search_ad",
            customer_id,
            arguments,
            validate=_validator("create_responsive_search_ad", arguments),
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

    @mcp.tool(annotations=annotations_for('pause_campaign'))
    async def pause_campaign(customer_id: str, campaign_id: str) -> dict:
        """Draft a change that pauses one campaign. Does NOT pause it.

        Returns a preview and a plan_id. The campaign is only paused when
        confirm_and_apply is called with that plan_id, which is a separate
        step on purpose. Pausing is reversible with enable_campaign.
        """
        return await _draft_status_change("pause_campaign", customer_id, campaign_id)

    @mcp.tool(annotations=annotations_for('enable_campaign'))
    async def enable_campaign(customer_id: str, campaign_id: str) -> dict:
        """Draft a change that enables one campaign. Does NOT enable it.

        Returns a preview and a plan_id; nothing happens until
        confirm_and_apply is called. Note that enabling a campaign lets it
        start spending its daily budget again.
        """
        return await _draft_status_change("enable_campaign", customer_id, campaign_id)

    @mcp.tool(annotations=annotations_for("pause_ad_group"))
    async def pause_ad_group(customer_id: str, ad_group_id: str) -> dict:
        """Draft a change that pauses one ad group. Does NOT pause it.

        Pausing an ad group stops all of its keywords and ads serving in one
        step, without touching any of them individually. Reversible with
        enable_ad_group.

        Returns a preview and a plan_id; nothing changes until
        confirm_and_apply. Use list_ad_groups to find an ad_group_id.
        """
        return await _draft_ad_group_status_change(
            "pause_ad_group", customer_id, ad_group_id
        )

    @mcp.tool(annotations=annotations_for("enable_ad_group"))
    async def enable_ad_group(customer_id: str, ad_group_id: str) -> dict:
        """Draft a change that enables one ad group. Does NOT enable it.

        Lets the ad group's keywords and ads serve again, spending the
        CAMPAIGN's budget - this does not create a budget of its own. If the
        campaign is paused the preview says so, because an enabled ad group in
        a paused campaign still does not serve.

        Returns a preview and a plan_id; nothing changes until
        confirm_and_apply.
        """
        return await _draft_ad_group_status_change(
            "enable_ad_group", customer_id, ad_group_id
        )


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


def _ad_group_preview(
    *,
    customer_id: str,
    ad_group: Any,
    target_status: str,
    currency_code: str,
) -> str:
    """The text a human approves for an ad group status change.

    Names the ad group, its campaign and its bid, because an id and an arrow
    is not something anybody can sensibly approve.

    The campaign's own status is on here for a specific reason: an ENABLED ad
    group inside a PAUSED campaign does not serve. Without that line the tool
    would report success on a change with no visible effect, and the person
    would be left looking for the fault somewhere else.
    """
    campaign_status = getattr(ad_group, "campaign_status", "") or "unknown"
    lines = [
        f"Account {customer_id}",
        f"Ad group {ad_group.name!r} (id {ad_group.ad_group_id})",
        f"  campaign : {ad_group.campaign_name!r} ({campaign_status})",
        f"  max CPC  : {format_micros(ad_group.cpc_bid_micros, currency_code)}",
        f"  status   : {ad_group.status} -> {target_status}",
    ]
    if target_status == "ENABLED":
        if campaign_status == "ENABLED":
            lines.append(
                "  NOTE: this lets its keywords and ads spend the CAMPAIGN's "
                "daily budget again."
            )
        else:
            lines.append(
                f"  NOTE: campaign {ad_group.campaign_name!r} is {campaign_status}, "
                "so this ad group"
            )
            lines.append(
                "        still will not serve until that campaign is enabled too."
            )
    else:
        lines.append("  This stops every keyword and ad in it serving.")
        lines.append("  Reversible with enable_ad_group.")
    return "\n".join(lines)


__all__ = [
    "register_write_tools",
    "TOOL_TARGET_STATUS",
]
