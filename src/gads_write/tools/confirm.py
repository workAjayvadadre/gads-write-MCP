"""THE ONLY TOOL THAT APPLIES ANYTHING.

Every write in this server ends here. A draft tool parks intent as a plan;
this takes a plan_id and turns it into a mutation. Nothing else calls
`Executor.apply`.

The order of operations is the point, and it is not arbitrary:

  1. identity          who is confirming, per Google
  2. peek the plan     ownership, expiry and single-use, WITHOUT consuming it
  3. authorise         tier, kill switch and account check, before any read
  4. re-read state     what the account looks like NOW, not at draft time
  5. re-run the gate   the full chain again, against the ORIGINAL tool
  6. consume the plan  marked used BEFORE anything is applied
  7. apply             ads/executor.py
  8. audit             record what actually happened, or the failure

Why 5 exists at all, given the draft already passed the gate: because time
passed. Between draft and confirm, someone may have been demoted in Google
Ads, the account may have been unlinked from the MCC, the account's own
budgets may have moved under the daily ceiling, or the kill switch may have
been thrown. A plan carries
intent, never authority. Re-checking is what makes that true rather than
merely stated.

Note that step 5 checks the ORIGINAL tool's tier, not this tool's. Otherwise
a `lead`-only change could be drafted by a lead and applied by an operator,
and the tier on the drafting tool would be decorative.

Why 3 and 4 exist: the gate cannot judge a budget or a bid without the
CURRENT value, because every one of those rules is relative - a percentage
increase needs something to be a percentage OF. A plan stores an absolute
target, so trusting the value the preview was built from would let an
approved "100 -> 120" become a 1100% increase if someone lowered the budget
to 10 in the Google Ads UI first. Step 3 comes before step 4 for the same
reason tools/writes.py authorises before reading: the managed-account check must
hold before this server touches an account at all. That costs a second audit
line on the tools that need state, which is the same deliberate trade
drafting already makes.

Why 6 comes before 7: if the apply crashes halfway, the plan is already
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

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.elicitation import AcceptedElicitation

from ..ads.executor import Executor, MutationRequest
from ..ads.reads import AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..safety.plans import PlanError, PlanStore
from .operations import OPERATIONS, ReadKind, read_current

logger = logging.getLogger(__name__)


async def _require_human_approval(ctx: Context, plan: Any) -> None:
    """Show the preview to a person and refuse unless they accept.

    This is what turns human approval from a client-side habit into a server
    guarantee. `pause_campaign` and `confirm_and_apply` are two ordinary tool
    calls, and nothing at the protocol level stopped a model making both in
    the same turn - so the person could be told "done" having never seen what
    changed. Elicitation blocks here until the client returns an answer.

    A client that cannot elicit gets refused rather than waved through.
    Failing open would leave exactly the hole this exists to close, and a
    connector that cannot show an approval prompt has no business applying
    changes that spend money.
    """
    message = (
        "Approve this change to your Google Ads account?\n\n"
        f"{plan.preview}\n\n"
        "Nothing has been changed yet. This applies only if you accept."
    )

    try:
        answer = await ctx.elicit(message, response_type=None)
    except Exception as exc:  # noqa: BLE001 - any failure to ask is a refusal
        logger.warning("could not ask for human approval: %s", exc)
        raise ToolError(
            "This change needs a person to approve it, and this client cannot "
            "show an approval prompt. Nothing was changed. Use a client that "
            "supports MCP elicitation, or have an administrator set "
            "GADS_REQUIRE_HUMAN_CONFIRMATION=false if that is genuinely "
            "intended."
        ) from exc

    if not isinstance(answer, AcceptedElicitation):
        raise ToolError(
            "The change was declined by the person asked to approve it. "
            "Nothing was changed, and the plan is still open if they change "
            "their mind."
        )


def register_confirm_tool(
    mcp: Any,
    *,
    guard: Any,
    executor: Executor,
    plan_store: PlanStore,
    reader: AdsReader,
    settings: Any,
    caller_provider: Callable[[], Any] = current_caller,
) -> None:
    """Define confirm_and_apply on `mcp`."""

    @mcp.tool
    async def confirm_and_apply(plan_id: str, ctx: Context) -> dict:
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

        # Fail closed: a tool with no entry in the checks table cannot be
        # confirmed at all, rather than being confirmed without re-checking.
        checks = OPERATIONS.get(plan.tool)
        if checks is None:
            raise ToolError(
                f"No re-validation is defined for {plan.tool!r}, so this plan "
                "cannot be applied. Nothing was changed."
            )

        # A budget increase has to reach the audit log, or the per-user daily
        # ceiling - which is derived from that log - would never accumulate.
        # The plan's figure is only a fallback: anything whose delta depends
        # on current state is recomputed below, once that state has been
        # re-read. Recording the draft-time figure when the two disagree
        # writes a number nobody approved into the ledger the ceiling is
        # built from, which is a permanent hole in the ceiling.
        spend_delta = _decimal_or_none(plan.spend_delta_units)

        # --- 3 & 4. authorise, then re-read the CURRENT state ----------
        # Only for tools whose rules are relative. A negative keyword or an
        # RSA is judged on its own arguments, so it pays for neither the
        # extra audit line nor the round trip to Google.
        current: Any = None
        if checks.reads is not ReadKind.NONE:
            authorised = await guard.check(
                tool=plan.tool,
                caller=caller,
                customer_id=plan.customer_id,
                arguments=plan.arguments,
                plan_id=plan.plan_id,
                dry_run=True,
            )
            if not authorised.allowed:
                raise ToolError(
                    f"This plan can no longer be applied: "
                    f"{authorised.reason_text}. Nothing was changed."
                )
            try:
                current = await read_current(
                    reader,
                    checks,
                    customer_id=plan.customer_id,
                    arguments=plan.arguments,
                )
            except AdsReadError as exc:
                # Refused, not consumed. We could not establish what we would
                # be changing, which is a reason to stop - not a reason to
                # burn the plan.
                raise ToolError(
                    f"could not re-read the current state in account "
                    f"{plan.customer_id} to re-check this plan: {exc}. "
                    "Nothing was changed; try again in a moment."
                ) from exc

        # Recomputed from the state just re-read, so the ledger records the
        # increase that was actually approved rather than the one the preview
        # happened to be built from.
        if checks.spend_delta is not None:
            spend_delta = checks.spend_delta(plan.arguments, current)

        def recheck(policy: Any, tier: Any, spend_today: Decimal) -> Any:
            # Same function the draft ran, with the state as it is NOW.
            return checks.recheck(
                policy,
                tier=tier,
                arguments=plan.arguments,
                current=current,
                payload=dict(payload),
                spend_today=spend_today,
            )

        # --- 5. the full gate again, against the ORIGINAL tool ---------
        decision = await guard.check(
            tool=plan.tool,
            caller=caller,
            customer_id=plan.customer_id,
            arguments=plan.arguments,
            validate=lambda policy: checks.validate(policy, plan.arguments),
            evaluate=recheck if checks.recheck is not None else None,
            needs_account_total=checks.needs_account_total,
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

        # --- 6. ask a person, and stop unless they say yes -------------
        # Deliberately AFTER the gate, so nobody is asked to approve a change
        # that policy would refuse anyway, and BEFORE the plan is consumed,
        # so declining leaves the plan open rather than burning it.
        if getattr(settings, "require_human_confirmation", True):
            await _require_human_approval(ctx, plan)

        # --- 7. claim it, before anything is applied -------------------
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

        # --- 7. apply --------------------------------------------------
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

        # --- 8. audit the success --------------------------------------
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
