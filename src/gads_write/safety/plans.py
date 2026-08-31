"""Draft plans: the mechanism that makes every write two-step.

A write tool does not write. It validates, checks policy, builds a
human-readable preview, and parks the intent here under a `plan_id`. A
separate `confirm_and_apply` call is what actually changes anything, and it
re-runs the full guard chain first.

Four properties, each of which closes a specific hole:

  owner-bound   Only the person who drafted a plan can confirm it. A plan_id
                pasted into a shared channel is inert to everyone else.
  time-limited  A plan expires (default 10 min). Approving something drafted
                an hour ago means approving a preview that may no longer
                describe reality.
  single-use    Consumed on confirm. The same plan_id cannot be applied
                twice, so a retried or replayed call cannot double-apply.
  re-evaluated  The plan stores intent, never a permission. Policy is
                re-checked at confirm time by guards.py, so a plan drafted
                under looser limits fails once they are tightened.

Plans live in process memory. That is why ops/ecosystem.config.js pins
`instances: 1` - a second worker would not see the first worker's plans and
single-use would have a hole in it. It also means a restart drops all open
plans, which is safe: the worst case is someone re-drafting.

Python notes for a TypeScript reader:
  - `time.monotonic()` is a clock that only moves forward and is immune to
    NTP steps and DST. Wall-clock time is stored separately, for display.
  - `threading.Lock` used as a context manager (`with self._lock:`) is the
    same idea as a mutex around a critical section.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# Bound on how many open plans we hold, so a runaway client cannot grow the
# process indefinitely. Expired plans are purged first; only if every plan
# is live and unexpired does drafting start refusing.
MAX_OPEN_PLANS = 500


class PlanError(RuntimeError):
    """Base class for every reason a plan cannot be confirmed."""


class PlanNotFound(PlanError):
    pass


class PlanNotOwned(PlanError):
    pass


class PlanExpired(PlanError):
    pass


class PlanAlreadyUsed(PlanError):
    pass


class TooManyOpenPlans(PlanError):
    pass


@dataclass
class Plan:
    """A drafted, not-yet-applied change.

    `arguments` is the intent, not a permission. Nothing here grants the
    right to do anything; guards.py decides that again at confirm time.
    """

    plan_id: str
    owner_email: str
    tool: str
    customer_id: str
    arguments: dict[str, Any]
    preview: str
    # Monotonic values drive expiry; wall-clock values are for humans.
    created_monotonic: float
    expires_monotonic: float
    created_at: str
    expires_at: str
    ttl_seconds: int
    consumed_at: str | None = None
    # Set by the drafting tool so the audit log and the daily ceiling can
    # account for the change. Currency units as a string, never micros.
    spend_delta_units: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now_monotonic: float) -> bool:
        return now_monotonic >= self.expires_monotonic

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def summary(self) -> dict[str, Any]:
        """What a tool returns to the caller. Never includes anything secret."""
        return {
            "plan_id": self.plan_id,
            "tool": self.tool,
            "customer_id": self.customer_id,
            "preview": self.preview,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "next_step": (
                f"Call confirm_and_apply with plan_id={self.plan_id!r} to apply "
                f"this. Nothing has changed yet."
            ),
        }


class PlanStore:
    """In-memory, thread-safe store of open plans."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16),
        max_open_plans: int = MAX_OPEN_PLANS,
    ) -> None:
        # Clocks and the ID factory are injected so tests can drive expiry
        # deterministically instead of sleeping.
        self._clock = clock
        self._wall_clock = wall_clock
        # token_urlsafe(16) is 128 bits of entropy. A plan_id is effectively
        # a capability, so it must not be guessable or sequential.
        self._id_factory = id_factory
        self._max_open_plans = max_open_plans
        self._plans: dict[str, Plan] = {}
        self._lock = threading.Lock()

    # -- drafting ----------------------------------------------------------

    def draft(
        self,
        *,
        owner_email: str,
        tool: str,
        customer_id: str,
        arguments: dict[str, Any],
        preview: str,
        ttl_seconds: int,
        spend_delta_units: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Plan:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = self._clock()
        wall_now = self._wall_clock()

        with self._lock:
            self._purge_expired_locked(now)

            if len(self._plans) >= self._max_open_plans:
                raise TooManyOpenPlans(
                    f"{len(self._plans)} plans are already open and unexpired. "
                    "Confirm or abandon some before drafting more."
                )

            plan = Plan(
                plan_id=self._id_factory(),
                owner_email=owner_email.strip().lower(),
                tool=tool,
                customer_id=str(customer_id).strip(),
                arguments=dict(arguments),
                preview=preview,
                created_monotonic=now,
                expires_monotonic=now + ttl_seconds,
                created_at=wall_now.isoformat(),
                expires_at=(wall_now + timedelta(seconds=ttl_seconds)).isoformat(),
                ttl_seconds=ttl_seconds,
                spend_delta_units=spend_delta_units,
                metadata=dict(metadata or {}),
            )
            self._plans[plan.plan_id] = plan
            return plan

    # -- reading -----------------------------------------------------------

    def peek(self, plan_id: str, *, caller_email: str) -> Plan:
        """Look at a plan without consuming it. Same checks as consume.

        Used to show someone what they are about to approve.
        """
        with self._lock:
            return self._checked_locked(plan_id, caller_email, self._clock())

    # -- consuming ---------------------------------------------------------

    def consume(self, plan_id: str, *, caller_email: str) -> Plan:
        """Claim a plan for application. It can never be claimed again.

        The plan is marked consumed *before* the caller executes anything.
        If the apply then crashes halfway, the plan is still burnt: the
        operator has to look at the account and draft a fresh change rather
        than blindly retrying, which is the safe failure mode when we cannot
        know whether the mutation landed.
        """
        with self._lock:
            plan = self._checked_locked(plan_id, caller_email, self._clock())
            plan.consumed_at = self._wall_clock().isoformat()
            return plan

    def _checked_locked(self, plan_id: str, caller_email: str, now: float) -> Plan:
        plan = self._plans.get(str(plan_id))
        if plan is None:
            raise PlanNotFound(
                f"No open plan {plan_id!r}. It may have expired, already been "
                "applied, or the server may have restarted. Draft the change again."
            )

        # Ownership is checked before expiry and before use, so someone
        # else's plan_id tells you nothing about its state.
        if plan.owner_email != caller_email.strip().lower():
            raise PlanNotOwned(
                f"Plan {plan_id!r} was drafted by someone else. Only the person "
                "who drafted a change can confirm it."
            )

        if plan.is_consumed:
            raise PlanAlreadyUsed(
                f"Plan {plan_id!r} was already applied at {plan.consumed_at}. "
                "Plans are single-use; draft a new one."
            )

        if plan.is_expired(now):
            raise PlanExpired(
                f"Plan {plan_id!r} expired at {plan.expires_at} "
                f"(plans last {plan.ttl_seconds}s). The preview may no longer "
                "describe the account. Draft the change again."
            )

        return plan

    # -- housekeeping ------------------------------------------------------

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired_locked(self._clock())

    def _purge_expired_locked(self, now: float) -> int:
        stale = [
            plan_id
            for plan_id, plan in self._plans.items()
            if plan.is_expired(now) or plan.is_consumed
        ]
        for plan_id in stale:
            del self._plans[plan_id]
        return len(stale)

    def open_count(self) -> int:
        with self._lock:
            return sum(
                1
                for plan in self._plans.values()
                if not plan.is_consumed and not plan.is_expired(self._clock())
            )
