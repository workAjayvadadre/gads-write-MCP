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
from typing import Any

from fastmcp.exceptions import ToolError

from ..ads.reads import AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..safety.plans import PlanError, PlanStore
from ..safety.policy import Policy, PolicyStore
from ..safety.units import format_micros
from ..safety.validators import (
    ValidationResult,
    validate_customer_id,
    validate_numeric_id,
)

logger = logging.getLogger(__name__)

# tool name -> the status it will set. Mirrors
# ads/executor.py:CAMPAIGN_STATUS_OPERATIONS, which is the enforcing copy.
TOOL_TARGET_STATUS: dict[str, str] = {
    "pause_campaign": "PAUSED",
    "enable_campaign": "ENABLED",
}


def validate_campaign_status_args(arguments: dict[str, Any]) -> ValidationResult:
    """Validate the arguments of a campaign status change.

    Lives at module scope, and is deliberately shared with tools/confirm.py,
    so that the checks run at draft time and the checks re-run at confirm
    time cannot drift apart.
    """
    result = ValidationResult()
    result.extend(validate_customer_id(arguments.get("customer_id", "")))
    result.extend(
        validate_numeric_id(arguments.get("campaign_id", ""), field_name="campaign_id")
    )
    return result


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


__all__ = ["register_write_tools", "validate_campaign_status_args", "TOOL_TARGET_STATUS"]
