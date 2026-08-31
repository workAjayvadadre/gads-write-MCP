"""The gate. Every tool calls this first, and confirm calls it again.

One module, one order, one place to read. Eight checks scattered across
eight tools would be eight chances to forget one, and the one you forget is
the one that costs money.

The order is deliberate, cheapest and most absolute first:

  1  kill switch        an if on a boolean already in memory
  2  identity           who is asking, per Google, not per arguments
  3  tier               what they may do TO THIS ACCOUNT
  4  account allowlist  refuse unmanaged accounts before inspecting anything
  5  blocked operation  irreversible operations, refused outright
  6  field validation   shape of the input
  7  policy limits      per-tier caps on the actual numbers
  8  daily ceiling      this user's running total for the day

Why that order and not another:

  - The kill switch reads a boolean. There is no reason to resolve a tier or
    touch an API to discover the server is switched off.
  - Identity precedes tier because tier resolution needs an email.
  - The allowlist precedes validation so that a request naming an account we
    do not manage never has its contents inspected, expanded into an API
    call, or written to the audit log in detail.
  - The daily ceiling is last because it is the only check that reads a
    file. Everything cheaper has already had its chance to refuse.

Every decision is audited, including refusals. A refused change is often the
more interesting record: it is how you notice someone repeatedly pushing at
a cap, or a limit set too tight for the team to do their job.

Python notes for a TypeScript reader:
  - `Guard` is a class holding its dependencies, constructed once at startup.
    Think constructor injection in a Nest service rather than module globals;
    it is what lets tests build one with fakes.
  - `validate` and `evaluate` are callbacks. The gate owns the ORDER and the
    audit; the calling tool supplies the two operation-specific steps, since
    only it knows whether this is a budget or a bid.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..auth.identity import AuthError
from ..auth.tiers import Tier, TierLookupError, TierResolver, tier_at_least
from ..settings import Settings
from ..tools.registry import ToolSpec, is_registered, spec_for
from .audit import AuditLog, AuditRecord, local_date_for
from .policy import Policy, PolicyStore, PolicyVerdict, evaluate_customer, evaluate_operation
from .spend import DailySpendLedger
from .validators import ValidationResult

logger = logging.getLogger(__name__)


# Verdict strings written to the audit log. Distinct values on purpose: the
# rate of `lookup_failed` is an operational signal, and folding it into
# `denied` would hide a Google outage as a wave of ordinary refusals.
VERDICT_ALLOWED = "allowed"
VERDICT_DENIED = "denied"
VERDICT_LOOKUP_FAILED = "lookup_failed"


@dataclass(frozen=True)
class GuardDecision:
    """The gate's verdict, and everything the caller and the log need."""

    allowed: bool
    tool: str
    verdict: str
    tier: Tier
    caller_email: str | None
    customer_id: str | None
    reasons: tuple[str, ...] = ()
    failed_check: str | None = None
    policy: Policy | None = None
    local_date: str = ""
    spend_today_units: Decimal = Decimal(0)
    spec: ToolSpec | None = None

    @property
    def reason_text(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "allowed"

    def raise_if_denied(self) -> None:
        if not self.allowed:
            raise GuardDenied(self)


class GuardDenied(RuntimeError):
    """Raised when the gate refuses. Carries the decision for the caller."""

    def __init__(self, decision: GuardDecision) -> None:
        super().__init__(decision.reason_text)
        self.decision = decision


class Guard:
    """The single gate. Construct once at startup; call on every tool."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy_store: PolicyStore,
        tier_resolver: TierResolver,
        audit_log: AuditLog,
        spend_ledger: DailySpendLedger,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._settings = settings
        self._policy_store = policy_store
        self._tiers = tier_resolver
        self._audit = audit_log
        self._spend = spend_ledger
        self._now = now

    async def check(
        self,
        *,
        tool: str,
        caller: Any,
        customer_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        validate: Callable[[Policy], ValidationResult] | None = None,
        evaluate: Callable[[Policy, Tier, Decimal], PolicyVerdict] | None = None,
        spend_delta_units: Decimal | None = None,
        plan_id: str | None = None,
        dry_run: bool = False,
    ) -> GuardDecision:
        """Run every check in order. Always writes exactly one audit line."""
        arguments = arguments or {}
        spec = spec_for(tool)

        # Read the policy snapshot ONCE, up front, and use this same object
        # for the whole evaluation. This is not a check; it is what stops a
        # policy.yaml saved mid-evaluation producing a half-old, half-new
        # verdict. It also gives us the account timezone for the audit line.
        policy = self._policy_store.current()
        moment = self._now()
        local_date = local_date_for(moment, policy.timezone)

        def deny(check: str, *reasons: str, verdict: str = VERDICT_DENIED) -> GuardDecision:
            decision = GuardDecision(
                allowed=False,
                tool=tool,
                verdict=verdict,
                tier=Tier.NONE,
                caller_email=getattr(caller, "email", None),
                customer_id=customer_id,
                reasons=tuple(reasons),
                failed_check=check,
                policy=policy,
                local_date=local_date,
                spec=spec,
            )
            self._write_audit(decision, arguments, plan_id, dry_run, moment, policy)
            return decision

        # --- 0. the tool must be registered ---------------------------------
        # spec_for() already fails closed to lead+writes, but an unregistered
        # tool is a programming error rather than a permission question, and
        # it deserves a distinct message rather than a confusing tier refusal.
        if not is_registered(tool):
            return deny(
                "registry",
                f"tool {tool!r} is not in the tool registry. Register it in "
                "tools/registry.py with the tier it requires; it is refused "
                "until then.",
            )

        # --- 1. kill switch --------------------------------------------------
        # Absolute and tier-independent. One env change plus a restart makes
        # the whole server read-only.
        if spec.writes and not self._settings.write_enabled:
            return deny(
                "kill_switch",
                "writes are disabled on this server (GADS_WRITE_ENABLED=false). "
                "Nothing can be changed until an administrator turns them on.",
            )

        # --- 2. identity -----------------------------------------------------
        email = getattr(caller, "email", None)
        if caller is None or not email:
            return deny(
                "identity",
                "could not establish who is calling; refusing to act without a "
                "verified identity",
            )

        # --- 3. tier, for THIS account ---------------------------------------
        # An indeterminate lookup is not the same as "no access" and must not
        # be treated as one. Failing open here would mean anyone able to cause
        # a Google outage could grant themselves permissions.
        try:
            tier = (
                await self._tiers.resolve(caller, customer_id)
                if customer_id
                else await self._tiers.visible_tier(caller)
            )
        except TierLookupError as exc:
            return deny(
                "tier_lookup",
                f"could not verify your Google Ads access level: {exc}. "
                "Nothing was changed. Try again in a moment.",
                verdict=VERDICT_LOOKUP_FAILED,
            )

        if not tier_at_least(tier, spec.required_tier):
            return deny(
                "tier",
                f"{tool} requires tier {spec.required_tier.value}; "
                f"your access level here is {tier.value}"
                + (f" on account {customer_id}" if customer_id else ""),
            )

        # Everything past this point knows the real tier, so rebuild `deny`
        # to carry it into the audit line.
        def deny_with_tier(check: str, *reasons: str) -> GuardDecision:
            decision = GuardDecision(
                allowed=False,
                tool=tool,
                verdict=VERDICT_DENIED,
                tier=tier,
                caller_email=email,
                customer_id=customer_id,
                reasons=tuple(reasons),
                failed_check=check,
                policy=policy,
                local_date=local_date,
                spec=spec,
            )
            self._write_audit(decision, arguments, plan_id, dry_run, moment, policy)
            return decision

        # --- 4. account allowlist --------------------------------------------
        if customer_id is not None:
            verdict = evaluate_customer(policy, customer_id)
            if not verdict.allowed:
                return deny_with_tier("account_allowlist", *verdict.reasons)

        # --- 5. blocked operation --------------------------------------------
        if spec.operation:
            verdict = evaluate_operation(policy, spec.operation)
            if not verdict.allowed:
                return deny_with_tier("blocked_operation", *verdict.reasons)

        # --- 6. field validation ---------------------------------------------
        if validate is not None:
            result = validate(policy)
            if not result.ok:
                return deny_with_tier("validation", *result.as_messages())

        # --- 7 & 8. policy limits, including this user's daily ceiling -------
        # The ledger read happens here, last, because it is the only check
        # that touches the filesystem.
        spend_today = self._spend.total_increase_units(
            user_email=email, local_date=local_date
        )
        if evaluate is not None:
            verdict = evaluate(policy, tier, spend_today)
            if not verdict.allowed:
                return deny_with_tier("policy", *verdict.reasons)

        decision = GuardDecision(
            allowed=True,
            tool=tool,
            verdict=VERDICT_ALLOWED,
            tier=tier,
            caller_email=email,
            customer_id=customer_id,
            policy=policy,
            local_date=local_date,
            spend_today_units=spend_today,
            spec=spec,
        )
        self._write_audit(
            decision, arguments, plan_id, dry_run, moment, policy,
            spend_delta_units=spend_delta_units,
        )
        return decision

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    def _write_audit(
        self,
        decision: GuardDecision,
        arguments: dict[str, Any],
        plan_id: str | None,
        dry_run: bool,
        moment: datetime,
        policy: Policy,
        *,
        spend_delta_units: Decimal | None = None,
    ) -> None:
        """One line per decision. Never raises into the caller's request.

        A guard decision is not itself a mutation, so a failure to log one
        must not break the request. The apply path is different: see
        `record_application`, which lets the failure through on purpose.
        """
        record = AuditRecord.build(
            user_email=decision.caller_email or "<unauthenticated>",
            tool=decision.tool,
            verdict=decision.verdict,
            account_timezone=policy.timezone,
            now=moment,
            arguments=arguments,
            customer_id=decision.customer_id,
            plan_id=plan_id,
            dry_run=dry_run,
            applied=False,
            denied_reasons=list(decision.reasons),
            spend_delta_units=(
                str(spend_delta_units) if spend_delta_units is not None else None
            ),
        )
        try:
            self._audit.append(record)
        except OSError:
            logger.exception(
                "failed to write audit line for %s by %s",
                decision.tool,
                decision.caller_email,
            )

    def record_application(
        self,
        decision: GuardDecision,
        *,
        arguments: dict[str, Any],
        plan_id: str | None,
        resource_names: list[str],
        spend_delta_units: Decimal | None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        dry_run: bool = False,
    ) -> None:
        """Record that a mutation was actually applied. Raises on failure.

        This one deliberately does NOT swallow errors. If we cannot record
        what we just did, the caller must know, because an unlogged mutation
        is worse than a refused one: nobody can find it afterwards.
        """
        policy_timezone = decision.policy.timezone if decision.policy else "UTC"
        record = AuditRecord.build(
            user_email=decision.caller_email or "<unauthenticated>",
            tool=decision.tool,
            verdict=VERDICT_ALLOWED if error is None else "error",
            account_timezone=policy_timezone,
            now=self._now(),
            arguments=arguments,
            customer_id=decision.customer_id,
            plan_id=plan_id,
            dry_run=dry_run,
            applied=error is None and not dry_run,
            resource_names=list(resource_names),
            spend_delta_units=(
                str(spend_delta_units) if spend_delta_units is not None else None
            ),
            result=result,
            error=error,
        )
        self._audit.append(record)
