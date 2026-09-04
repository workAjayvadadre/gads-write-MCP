"""Tiers derived from the user's OWN Google Ads access role.

This is the resolver the whole design was waiting for. The operational
requirement it satisfies: a marketing lead adds someone in the Google Ads UI
and they work here, immediately, with no developer involved and no file to
edit. Nobody maintains a second copy of the permission model, so the two
copies cannot disagree.

    ADMIN        -> lead
    STANDARD     -> operator
    READ_ONLY    -> readonly
    EMAIL_ONLY   -> none      a notification recipient, not an operator
    UNKNOWN      -> none      a value from a newer API nobody has reviewed
    UNSPECIFIED  -> none
    no row       -> none

UNKNOWN mapping to `none` is not defensive padding. It is the API's way of
saying "this enum member did not exist when your client was generated". A
future role that we map optimistically is a privilege escalation delivered
by a Google release note.

Three things here are worth understanding before changing anything.

**Inheritance.** Google resolves a user's effective role against the
`login-customer-id` the request is made through, and roles are inherited
down the account hierarchy. A team member usually has one row on the MCC and
no row at all on the child accounts they work in. So a lookup that only ever
asked the child account would see "no row" and lock out the entire team. We
therefore ask the account first and fall back to the manager: a direct grant
on the child is more specific and wins; otherwise the inherited manager role
applies. That degrades safely - the worst case is that someone is offered
less than the Google Ads UI shows them and asks a lead, rather than more.

**A failed lookup is not a zero.** `AdsReadError` becomes `TierLookupError`,
never `Tier.NONE`. See auth/tiers.py for why that distinction is load-bearing:
if an outage produced "no access" it would be indistinguishable from a
genuine refusal in the logs, and if it produced anything permissive it would
make causing an outage an escalation path.

**The open question this phase does not settle.** Google documents
`customer_user_access` as how an *administrator* lists users. Whether a
STANDARD or READ_ONLY user may read their own row with their own token is
not documented either way, and could not be tested here - the available
credential is not attached to the MCC. Rather than guess, this resolver
fails closed and says so loudly: the first non-admin who connects produces a
specific, actionable error naming the fallback, and until then nobody is
over-privileged. `OverridingTierResolver` is the break-glass that keeps the
team working while that is sorted out.

Python notes for a TypeScript reader:
  - `time.monotonic()` is a clock that only moves forward and is immune to
    the system clock being adjusted. Correct choice for measuring a TTL;
    `datetime.now()` would not be.
  - The cache is a plain dict behind a lock, not an LRU library, because the
    eviction policy matters more than the hit rate here and should be
    readable at a glance.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..ads.reads import AdsReader, AdsReadError
from ..safety.accounts import AccountLookupError
from .roles import RoleStore
from .tiers import Tier, TierLookupError, TierResolver, highest

logger = logging.getLogger(__name__)


# The only mapping from Google's vocabulary to ours. Anything absent from
# this table is `none` by construction, which is why there is no `.get(role,
# something_permissive)` anywhere below.
ROLE_TO_TIER: dict[str, Tier] = {
    "ADMIN": Tier.LEAD,
    "STANDARD": Tier.OPERATOR,
    "READ_ONLY": Tier.READONLY,
    "EMAIL_ONLY": Tier.NONE,
    "UNKNOWN": Tier.NONE,
    "UNSPECIFIED": Tier.NONE,
}


def tier_for_access_role(role: str | None) -> Tier:
    """Map a Google Ads access role name to a tier. Unrecognised means none."""
    if role is None:
        return Tier.NONE
    return ROLE_TO_TIER.get(role.strip().upper(), Tier.NONE)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

MAX_CACHE_ENTRIES = 2000


@dataclass(frozen=True)
class _Entry:
    tier: Tier
    expires_at: float


class TierCache:
    """A small TTL cache for resolved tiers.

    This exists because without it every `tools/list` costs one Google Ads
    round trip per managed account, and every tool call costs another. With
    it, a tier is looked up at most once per TTL per (user, account).

    The TTL is a deliberate, bounded weakening of "resolve on every call".
    The requirement that mattered was that a demotion must not wait for the
    user to reconnect - an unbounded session-lifetime cache could keep a
    removed operator working for hours. A short TTL bounds that window to
    seconds and is visible in health_check. Set GADS_TIER_CACHE_SECONDS=0 to
    disable it entirely and pay the round trips.

    Only definite answers are cached. A `TierLookupError` is never stored:
    an outage must not become sticky, and must not be remembered as an
    answer.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], _Entry] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, email: str, customer_id: str | None) -> Tier | None:
        if self._ttl <= 0:
            return None
        key = (email, customer_id or "")
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del self._entries[key]
                return None
            return entry.tier

    def put(self, email: str, customer_id: str | None, tier: Tier) -> None:
        if self._ttl <= 0:
            return
        now = self._clock()
        with self._lock:
            if len(self._entries) >= MAX_CACHE_ENTRIES:
                # Drop everything expired; if that is not enough, drop the
                # lot. Bounded memory matters more than a warm cache, and a
                # cold cache is only ever slower, never wrong.
                self._entries = {
                    key: value
                    for key, value in self._entries.items()
                    if value.expires_at > now
                }
                if len(self._entries) >= MAX_CACHE_ENTRIES:
                    self._entries.clear()
            self._entries[(email, customer_id or "")] = _Entry(
                tier=tier, expires_at=now + self._ttl
            )

    def invalidate(self, email: str | None = None) -> None:
        """Drop cached tiers, for one user or for everyone."""
        with self._lock:
            if email is None:
                self._entries.clear()
            else:
                for key in [k for k in self._entries if k[0] == email]:
                    del self._entries[key]


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


class GoogleAdsTierResolver(TierResolver):
    """Resolves tiers from customer_user_access, using the caller's token."""

    def __init__(
        self,
        *,
        reader: AdsReader,
        login_customer_id: str,
        managed_customer_ids: Callable[[], Awaitable[frozenset[str]]],
        cache: TierCache | None = None,
    ) -> None:
        self._reader = reader
        self._login_customer_id = login_customer_id
        # Async because the managed set is now derived from the manager
        # account rather than read out of a config file. See
        # safety/accounts.py.
        self._managed_customer_ids = managed_customer_ids
        self._cache = cache or TierCache(0)

    # -- interface --------------------------------------------------------

    async def resolve(self, caller, customer_id: str) -> Tier:  # noqa: ANN001
        email = (getattr(caller, "email", "") or "").strip().lower()
        if not email:
            # No identity, no tier. Not an outage: a definite nothing.
            return Tier.NONE

        cached = self._cache.get(email, customer_id)
        if cached is not None:
            return cached

        tier = await self._resolve_uncached(email=email, customer_id=customer_id)
        self._cache.put(email, customer_id, tier)
        return tier

    async def visible_tier(self, caller) -> Tier:  # noqa: ANN001
        """Highest tier this caller holds on any account WE MANAGE.

        Intersecting with the managed set is a real security control, not
        tidiness. Someone may be ADMIN on a personal Google Ads account that
        has nothing to do with this company; without the intersection that
        would light up every `lead` tool in their menu.
        """
        email = (getattr(caller, "email", "") or "").strip().lower()
        if not email:
            return Tier.NONE

        cached = self._cache.get(email, None)
        if cached is not None:
            return cached

        # The managed set is derived from the manager account now, so unlike
        # the config list it replaced, asking for it can FAIL. Everything
        # upstream - the middleware's tools/list hook and the gate's step 3 -
        # catches TierLookupError and only that, so an AccountLookupError
        # escaping raw would crash tool listing and would skip the gate's
        # audit line, leaving an outage with no `lookup_failed` record.
        try:
            accounts = sorted(await self._managed_customer_ids())
        except AccountLookupError as exc:
            raise TierLookupError(
                f"could not determine which accounts this server manages, so "
                f"your access level cannot be established: {exc}"
            ) from exc

        if not accounts:
            # A readable but empty manager account. Nobody can do anything,
            # which is the correct reading of "we manage nothing".
            return Tier.NONE

        found: list[Tier] = []
        failures: list[str] = []
        for account in accounts:
            try:
                found.append(await self.resolve(caller, account))
            except TierLookupError as exc:
                failures.append(f"{account}: {exc}")

        if not found:
            # Every account failed. That is an outage, not an answer.
            raise TierLookupError(
                "could not determine your access level on any managed account: "
                + "; ".join(failures)
            )

        if failures:
            # Partial result. The menu may under-report, which is the safe
            # direction, but it must not do so silently.
            logger.warning(
                "tier lookup failed for %d of %d accounts; the tool list may "
                "show less than it should: %s",
                len(failures),
                len(accounts),
                "; ".join(failures),
            )

        tier = highest(found)
        self._cache.put(email, None, tier)
        return tier

    @property
    def source(self) -> str:
        ttl = self._cache.ttl_seconds
        detail = f"{ttl:g}s cache" if ttl > 0 else "uncached"
        return f"google ads customer_user_access ({detail})"

    # -- internals --------------------------------------------------------

    async def _resolve_uncached(self, *, email: str, customer_id: str) -> Tier:
        role = await self._read_role(customer_id=customer_id, email=email)

        if role is None and customer_id != self._login_customer_id:
            # No direct grant on this account. Fall back to the manager
            # account the request is made through, because that is where a
            # normal team member's single access row actually lives.
            role = await self._read_role(
                customer_id=self._login_customer_id, email=email
            )

        return tier_for_access_role(role)

    async def _read_role(self, *, customer_id: str, email: str) -> str | None:
        try:
            return await self._reader.access_role(
                customer_id=customer_id, email=email
            )
        except AdsReadError as exc:
            # This is the branch that answers the open question in CLAUDE.md.
            # If non-admins cannot read their own access row, every non-admin
            # lands here, and the message says exactly what to do about it.
            raise TierLookupError(
                f"could not read your Google Ads access role on account "
                f"{customer_id}: {exc}. If this affects every non-admin user, "
                "it means customer_user_access is readable only by admins; the "
                "fallback is a read-only service credential used SOLELY for "
                "this role lookup, with every actual read and write still on "
                "the user's own token."
            ) from exc


# ---------------------------------------------------------------------------
# break-glass
# ---------------------------------------------------------------------------


class OverridingTierResolver(TierResolver):
    """Consults roles.yaml first, then the real resolver.

    The override file is normally absent, so this is a pass-through. A name
    in it means: "Google could not tell us this person's role and a lead has
    deliberately overridden it."

    Deliberately a file edit rather than an automatic fallback. It is
    visible, it is in git, it shows up in review, and it is obviously
    temporary - none of which is true of code that quietly grants a tier when
    an API call fails.

    An override can only be *consulted*; it is still subject to every other
    check in the gate, including the managed-account check and the kill switch.
    """

    def __init__(self, *, overrides: RoleStore, primary: TierResolver) -> None:
        self._overrides = overrides
        self._primary = primary

    def _override_for(self, caller) -> Tier | None:  # noqa: ANN001
        email = (getattr(caller, "email", "") or "").strip().lower()
        if not email:
            return None
        table = self._overrides.current()
        tier = table.override_for(email)
        if tier is not None:
            logger.warning(
                "BREAK-GLASS: tier for %s came from %s, not from Google Ads",
                email,
                table.source_path,
            )
        return tier

    async def resolve(self, caller, customer_id: str) -> Tier:  # noqa: ANN001
        override = self._override_for(caller)
        if override is not None:
            return override
        return await self._primary.resolve(caller, customer_id)

    async def visible_tier(self, caller) -> Tier:  # noqa: ANN001
        override = self._override_for(caller)
        if override is not None:
            return override
        return await self._primary.visible_tier(caller)

    @property
    def source(self) -> str:
        count = len(self._overrides.current().users)
        suffix = f", {count} break-glass override(s)" if count else ""
        return f"{self._primary.source}{suffix}"
