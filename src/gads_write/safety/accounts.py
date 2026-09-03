"""Which accounts this server may touch, derived from the MCC.

This replaces the hand-written `allowed_customer_ids` list in policy.yaml.

Why it is derived rather than configured. The list was never a per-user
permission - that has always been the tier check, which asks Google. What
the list actually bought was one thing: our developer token is only ever
used against accounts that belong to us. A caller's own Google login may
well reach a former employer's account or another client's; Google would
say yes, and every one of those calls would be attributed to OUR developer
token. The manager account already describes exactly the set we mean, so
asking it is both zero-maintenance and more accurate than a list somebody
has to remember to update.

The set is a property of the MCC, NOT of the caller, so it is cached once
and shared. That is deliberate: most of the team holds access on a child
account and cannot read the manager, and deriving per-caller would lock
them out of everything. Sharing it grants nobody anything - the per-user
decision is `auth/google_ads_roles.py`, which still runs on every call with
the caller's own credential.

Membership comes back with the account's currency and timezone, which is
what lets `currency_code` and `timezone` leave the config file too. One
lookup answers "is this ours?" and "in what currency?" together, because
the guard needs both at the same moment.

Failing closed, and the distinction that matters:

    None                  we asked, and this account is not ours
    AccountLookupError    we could not find out

Conflating those would turn a Google outage into a silent, total refusal
that is indistinguishable from a correct one in the audit log. Same
reasoning as `TierLookupError` in auth/tiers.py; see that module.

Python notes for a TypeScript reader:
  - `time.monotonic()` only moves forward and ignores system clock changes,
    which is what you want for a TTL. `datetime.now()` is not.
  - The `clock` parameter is injected so tests can advance time without
    sleeping.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..ads.reads import AccountSummary, AdsReader, AdsReadError

logger = logging.getLogger(__name__)

# Same default as the tier cache, for the same reason: a change made in the
# Google Ads UI should bite in seconds, not on the next restart. Linking a
# new sub-account is rarer than a permission change, so this could be longer;
# it is kept short because the cost of a miss is one query.
DEFAULT_TTL_SECONDS = 300


class AccountLookupError(RuntimeError):
    """Raised when the managed-account set could not be determined.

    Never raised to mean "this account is not managed" - that is `None`.
    """


@dataclass(frozen=True)
class ManagedAccount:
    """One account under the manager, with what the guard needs to judge it."""

    customer_id: str
    currency_code: str
    timezone: str
    descriptive_name: str
    is_manager: bool


def _from_summary(summary: AccountSummary) -> ManagedAccount:
    return ManagedAccount(
        customer_id=str(summary.customer_id).strip(),
        currency_code=(summary.currency_code or "").strip(),
        timezone=(summary.time_zone or "").strip(),
        descriptive_name=(summary.descriptive_name or "").strip(),
        is_manager=bool(summary.is_manager),
    )


class ManagedAccountStore:
    """The set of accounts under the MCC, cached with a TTL.

    Thread-safe via an asyncio lock: FastMCP serves concurrent requests, and
    a cold cache under load should produce one Google query rather than one
    per in-flight request.
    """

    def __init__(
        self,
        *,
        reader: AdsReader,
        login_customer_id: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reader = reader
        self._manager_id = str(login_customer_id).strip()
        self._ttl = max(0, int(ttl_seconds))
        self._clock = clock
        self._lock = asyncio.Lock()
        self._accounts: dict[str, ManagedAccount] | None = None
        self._expires_at = 0.0

    @property
    def login_customer_id(self) -> str:
        return self._manager_id

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def get(self, customer_id: str) -> ManagedAccount | None:
        """The account, or None if it is not under our manager account."""
        accounts = await self._current()
        return accounts.get(str(customer_id).strip())

    async def all(self) -> tuple[ManagedAccount, ...]:
        """Every managed account, ordered by customer ID for a stable listing."""
        accounts = await self._current()
        return tuple(sorted(accounts.values(), key=lambda a: a.customer_id))

    async def _current(self) -> dict[str, ManagedAccount]:
        cached = self._fresh_cache()
        if cached is not None:
            return cached

        async with self._lock:
            # Re-check inside the lock: another request may have just loaded it.
            cached = self._fresh_cache()
            if cached is not None:
                return cached

            try:
                summaries = await self._reader.managed_accounts(
                    manager_customer_id=self._manager_id
                )
            except AdsReadError as exc:
                # Deliberately NOT cached, and deliberately not an empty set.
                # An outage must not become sticky, and it must not read as a
                # correct refusal.
                raise AccountLookupError(
                    f"could not list the accounts under manager account "
                    f"{self._manager_id}: {exc}"
                ) from exc

            accounts = {
                account.customer_id: account
                for account in (_from_summary(s) for s in summaries)
                if account.customer_id
            }

            if self._ttl > 0:
                self._accounts = accounts
                self._expires_at = self._clock() + self._ttl
            else:
                self._accounts = None

            logger.info(
                "managed accounts refreshed from manager %s: %d account(s)",
                self._manager_id,
                len(accounts),
            )
            return accounts

    def _fresh_cache(self) -> dict[str, ManagedAccount] | None:
        if self._accounts is None or self._ttl <= 0:
            return None
        if self._clock() >= self._expires_at:
            return None
        return self._accounts
