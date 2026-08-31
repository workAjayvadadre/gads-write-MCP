"""THE ONLY TOOL THAT APPLIES ANYTHING.

Every write in this server ends here. A draft tool parks intent as a plan;
this takes a plan_id and turns it into a mutation. Nothing else calls
`Executor.apply`.

The order of operations is the point, and it is not arbitrary:

  1. identity          who is confirming, per Google
  2. peek the plan     ownership, expiry and single-use, WITHOUT consuming it
  3. re-run the gate   the full chain again, against the ORIGINAL tool
  4. consume the plan  marked used BEFORE anything is applied
  5. apply             ads/executor.py
  6. audit             record what actually happened, or the failure

Why 3 exists at all, given the draft already passed the gate: because time
passed. Between draft and confirm, someone may have been demoted in Google
Ads, the account may have been dropped from the allowlist, `policy.yaml` may
have been tightened, or the kill switch may have been thrown. A plan carries
intent, never authority. Re-checking is what makes that true rather than
merely stated.

Note that step 3 checks the ORIGINAL tool's tier, not this tool's. Otherwise
a `lead`-only change could be drafted by a lead and applied by an operator,
and the tier on the drafting tool would be decorative.

Why 4 comes before 5: if the apply crashes halfway, the plan is already
burnt. Nobody can retry it blindly. That is the safe failure mode when we
cannot know whether the mutation reached Google - a human has to look at the
account and draft afresh. The alternative, consuming on success, turns a
timeout into a double-apply.

Python note for a TypeScript reader:
  `peek` then `consume` looks redundant but is not. Peek validates so we can
  refuse cheaply and leave the plan usable; consume is the compare-and-swap
  that claims it exactly once.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from fastmcp.exceptions import ToolError

from ..ads.executor import Executor, MutationRequest
from ..auth.identity import current_caller
from ..safety.plans import PlanError, PlanStore
from .writes import REVALIDATORS

logger = logging.getLogger(__name__)


def register_confirm_tool(
    mcp: Any,
    *,
    guard: Any,
    executor: Executor,
    plan_store: PlanStore,
    caller_provider: Callable[[], Any] = current_caller,
) -> None:
    """Define confirm_and_apply on `mcp`."""

    @mcp.tool
    async def confirm_and_apply(plan_id: str) -> dict:
        """Apply a previously drafted change. THIS ONE ACTUALLY CHANGES THINGS.

        Takes the plan_id returned by a draft tool such as pause_campaign.
        Only the person who drafted the plan can confirm it, plans expire,
        and each can be applied exactly once. Policy is re-checked now, so a
        plan may be refused even though drafting it succeeded.
        """
        caller = caller_provider()
        plan_id = str(plan_id).strip()

        # --- 2. peek: ownership, expiry, single-use --------------------
        try:
            plan = plan_store.peek(plan_id, caller_email=caller.email)
        except PlanError as exc:
            raise ToolError(f"{exc} Nothing was changed.") from exc

        # The executor operation and its payload were decided at draft time
        # and stored on the plan, so nothing is re-derived here. One tool can
        # map to several operations - add_negative_keyword is a campaign
        # criterion or an ad group criterion depending on what was targeted.
        operation = str(plan.metadata.get("operation") or "").strip()
        payload = plan.metadata.get("payload")
        if not operation or not isinstance(payload, dict):
            raise ToolError(
                f"Plan {plan_id!r} carries no executor operation, so it cannot be "
                "applied. Draft the change again. Nothing was changed."
            )

        # Fail closed: a tool with no re-validator cannot be confirmed at all,
        # rather than being confirmed without re-validation.
        revalidator = REVALIDATORS.get(plan.tool)
        if revalidator is None:
            raise ToolError(
                f"No re-validation is defined for {plan.tool!r}, so this plan "
                "cannot be applied. Nothing was changed."
            )

        # A budget increase has to reach the audit log, or the per-user daily
        # ceiling - which is derived from that log - would never accumulate.
        spend_delta = _decimal_or_none(plan.spend_delta_units)

        # --- 3. the full gate again, against the ORIGINAL tool ---------
        decision = await guard.check(
            tool=plan.tool,
            caller=caller,
            customer_id=plan.customer_id,
            arguments=plan.arguments,
            validate=lambda policy: revalidator(policy, plan.arguments),
            spend_delta_units=spend_delta,
            plan_id=plan.plan_id,
        )
        if not decision.allowed:
            # The plan is deliberately NOT consumed. It was refused by policy
            # as it stands now, not used up; if a lead widens the limit a
            # minute later the same plan is still confirmable.
            raise ToolError(
                f"This plan can no longer be applied: {decision.reason_text}. "
                "Nothing was changed."
            )

        # --- 4. claim it, before anything is applied -------------------
        try:
            plan = plan_store.consume(plan_id, caller_email=caller.email)
        except PlanError as exc:
            raise ToolError(f"{exc} Nothing was changed.") from exc

        request = MutationRequest(
            customer_id=plan.customer_id,
            operation=operation,
            payload=dict(payload),
            validate_only=False,
        )

        # --- 5. apply --------------------------------------------------
        try:
            result = await executor.apply(request)
        except Exception as exc:  # noqa: BLE001 - audited, then re-raised
            # An unlogged mutation attempt is worse than a refused one:
            # nobody can find it afterwards. record_application raises if it
            # cannot write, and that is intentional.
            guard.record_application(
                decision,
                arguments=plan.arguments,
                plan_id=plan.plan_id,
                resource_names=[],
                spend_delta_units=None,
                error=str(exc),
            )
            raise ToolError(
                f"The change failed and the plan has been used up: {exc}. "
                "Check the account before drafting again - we cannot be certain "
                "whether the change reached Google."
            ) from exc

        if not result.success:
            guard.record_application(
                decision,
                arguments=plan.arguments,
                plan_id=plan.plan_id,
                resource_names=list(result.resource_names),
                spend_delta_units=None,
                error=result.error or "the mutation reported failure",
            )
            raise ToolError(
                f"The change was not applied: {result.error}. The plan has been "
                "used up; draft a new one."
            )

        # --- 6. audit the success --------------------------------------
        guard.record_application(
            decision,
            arguments=plan.arguments,
            plan_id=plan.plan_id,
            resource_names=list(result.resource_names),
            # Only a SUCCESSFUL apply contributes to the daily ceiling. A
            # failed one must not consume headroom it never used.
            spend_delta_units=spend_delta,
            result=dict(result.details),
        )

        logger.info(
            "plan %s applied by %s: %s on %s",
            plan.plan_id,
            caller.email,
            operation,
            plan.customer_id,
        )

        return {
            "ok": True,
            "applied": True,
            "plan_id": plan.plan_id,
            "tool": plan.tool,
            "customer_id": plan.customer_id,
            "resource_names": list(result.resource_names),
            "details": dict(result.details),
            "preview_that_was_approved": plan.preview,
        }


__all__ = ["register_confirm_tool"]


def _decimal_or_none(value: object) -> Decimal | None:
    """Plans store the spend delta as a string; the audit log wants a number."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
