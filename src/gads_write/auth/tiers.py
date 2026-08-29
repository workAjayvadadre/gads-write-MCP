"""Permission tiers and the interface that resolves them.

This module defines WHAT a tier is and HOW it is asked for. It deliberately
knows nothing about where the answer comes from.

Why that separation exists: in production a tier must come from the user's
own Google Ads access level, so that adding someone in the Google Ads UI
grants them access here with no developer involved. That resolver needs the
`google-ads` library, which does not arrive until Phase 3. Rather than let
guards, middleware and tests harden around the temporary file-based answer,
they all depend on this interface and nothing else.

  Phase 2   FileTierResolver      reads config/roles.yaml
  Phase 3   GoogleAdsTierResolver reads customer_user_access, becomes default

Two questions, deliberately separate:

  resolve(caller, customer_id)  What may you do TO THIS ACCOUNT?
                                Authoritative. Used by the gate.

  visible_tier(caller)          What might you be able to do at all?
                                Used only to decide which tools appear in
                                `tools/list`, which carries no customer_id.
                                It is a menu, never a permission.

Python notes for a TypeScript reader:
  - `Protocol` is structural typing, the same idea as a TS `interface`. A
    class satisfies it by having the right methods; there is no `implements`
    keyword and no inheritance.
  - Both methods are `async` because the Phase 3 implementation does network
    I/O. Making them async now means Phase 3 is not a breaking change.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    # Import only for type checking. At runtime this module must stay free of
    # FastMCP so that the safety core, which imports Tier, remains testable
    # with no server, no credentials and no network.
    from .identity import Caller


class Tier(str, Enum):
    """Permission tiers, lowest to highest.

    Mapped from Google Ads access roles in Phase 3:
        ADMIN       -> lead
        STANDARD    -> operator
        READ_ONLY   -> readonly
        EMAIL_ONLY  -> none      (a notification recipient, not an operator)
        UNKNOWN     -> none      (a value from a newer API we have not reviewed)
        UNSPECIFIED -> none
        no access   -> none
    """

    NONE = "none"
    READONLY = "readonly"
    OPERATOR = "operator"
    LEAD = "lead"


# Explicit ranking. Do not rely on declaration order: reordering the enum
# must never silently change who can spend money.
_RANK: dict[Tier, int] = {
    Tier.NONE: 0,
    Tier.READONLY: 1,
    Tier.OPERATOR: 2,
    Tier.LEAD: 3,
}


def tier_at_least(actual: Tier, required: Tier) -> bool:
    """True if `actual` meets or exceeds `required`."""
    return _RANK[actual] >= _RANK[required]


def highest(tiers: list[Tier]) -> Tier:
    """The most permissive tier in a list, or NONE if empty."""
    return max(tiers, key=lambda t: _RANK[t], default=Tier.NONE)


class TierLookupError(RuntimeError):
    """The tier could not be determined. NOT the same as 'no access'.

    This distinction is the whole point of having a dedicated exception:

      Tier.NONE          A definitive answer. We asked, and this person has
                         no access. Refuse them, confidently.

      TierLookupError    An indeterminate result: timeout, 5xx, quota
                         exhausted, or the caller cannot read their own
                         access row. We do not know.

    Callers must fail closed on this. An outage must never be an escalation
    path, because anyone who can cause an outage could then grant themselves
    permissions. See safety/guards.py, which refuses with a distinct audit
    verdict so the rate of these is visible rather than silent.
    """


@runtime_checkable
class TierResolver(Protocol):
    """The one interface through which a permission level is obtained."""

    async def resolve(self, caller: "Caller", customer_id: str) -> Tier:
        """This caller's tier ON THIS ACCOUNT. The authoritative answer.

        Raises TierLookupError if the answer cannot be determined. Returning
        Tier.NONE means 'definitely nothing', which is a different claim.
        """
        ...

    async def visible_tier(self, caller: "Caller") -> Tier:
        """The highest tier this caller holds on any permitted account.

        Used ONLY for tool listing. `tools/list` asks "what can I do?" with
        no account in the question, so it cannot be answered per-account.
        Never use this to authorize an actual change.
        """
        ...

    @property
    def source(self) -> str:
        """Short human-readable description, for health_check and audit."""
        ...
