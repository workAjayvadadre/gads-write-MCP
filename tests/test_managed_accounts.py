"""The managed-account set, derived from the MCC instead of a hand-written list.

What these tests pin down:

  - Membership is derived, not configured. An account under the manager is
    managed; anything else is not, however good the caller's Google access.
  - Currency and timezone come with membership, per account, in the same
    lookup. That is what removes them from policy.yaml.
  - A failed derivation is NOT an empty set. `AccountLookupError`, never
    "no accounts", for exactly the reason TierLookupError exists.
  - Failures are never cached. An outage must not become sticky.
"""

from __future__ import annotations

import pytest

from gads_write.ads.reads import AccountSummary, AdsReadError
from gads_write.safety.accounts import (
    AccountLookupError,
    ManagedAccountStore,
)

MCC = "9999999999"
CHILD_A = "1234567890"
CHILD_B = "2222222222"
OUTSIDER = "8888888888"


def _summary(
    customer_id: str,
    *,
    currency: str = "INR",
    timezone: str = "Asia/Kolkata",
    manager: bool = False,
    status: str = "ENABLED",
) -> AccountSummary:
    return AccountSummary(
        customer_id=customer_id,
        descriptive_name=f"account {customer_id}",
        currency_code=currency,
        time_zone=timezone,
        is_manager=manager,
        is_test_account=False,
        status=status,
    )


class FakeReader:
    """Stands in for GoogleAdsReader.managed_accounts."""

    def __init__(self, accounts=None, *, error: Exception | None = None) -> None:
        self._accounts = tuple(accounts or ())
        self._error = error
        self.calls = 0

    async def managed_accounts(self, *, manager_customer_id: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._accounts

    def set_accounts(self, accounts) -> None:
        self._accounts = tuple(accounts)

    def set_error(self, error: Exception | None) -> None:
        self._error = error


def _store(reader, *, ttl_seconds: int = 300) -> ManagedAccountStore:
    return ManagedAccountStore(
        reader=reader, login_customer_id=MCC, ttl_seconds=ttl_seconds
    )


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_account_under_the_manager_is_managed() -> None:
    store = _store(FakeReader([_summary(CHILD_A)]))
    account = await store.get(CHILD_A)
    assert account is not None
    assert account.customer_id == CHILD_A


@pytest.mark.asyncio
async def test_an_account_outside_the_manager_is_not_managed() -> None:
    """The whole point of deriving from the MCC rather than allowing anything.

    The caller may well have Google access to this account. It is still not
    ours, so our developer token must never be used against it.
    """
    store = _store(FakeReader([_summary(CHILD_A)]))
    assert await store.get(OUTSIDER) is None


@pytest.mark.asyncio
async def test_the_manager_account_itself_is_managed() -> None:
    """It is returned by the query at level 0 and must not be special-cased out."""
    store = _store(FakeReader([_summary(MCC, manager=True), _summary(CHILD_A)]))
    assert await store.get(MCC) is not None


@pytest.mark.asyncio
async def test_ids_are_compared_after_stripping() -> None:
    store = _store(FakeReader([_summary(CHILD_A)]))
    assert await store.get(f"  {CHILD_A} ") is not None


# ---------------------------------------------------------------------------
# the metadata that replaces policy.yaml
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_currency_and_timezone_come_from_the_account() -> None:
    """This is what lets currency_code and timezone leave the config file."""
    store = _store(
        FakeReader(
            [
                _summary(CHILD_A, currency="INR", timezone="Asia/Kolkata"),
                _summary(CHILD_B, currency="USD", timezone="America/New_York"),
            ]
        )
    )

    a = await store.get(CHILD_A)
    b = await store.get(CHILD_B)

    assert (a.currency_code, a.timezone) == ("INR", "Asia/Kolkata")
    assert (b.currency_code, b.timezone) == ("USD", "America/New_York")


@pytest.mark.asyncio
async def test_all_returns_every_managed_account() -> None:
    store = _store(FakeReader([_summary(CHILD_A), _summary(CHILD_B)]))
    ids = {a.customer_id for a in await store.all()}
    assert ids == {CHILD_A, CHILD_B}


# ---------------------------------------------------------------------------
# failing closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_failure_raises_rather_than_reporting_no_accounts() -> None:
    """`None` means "asked, not ours". An error means "we do not know".

    Conflating them would turn a Google outage into a silent, total denial
    that looks exactly like a correct refusal in the audit log.
    """
    store = _store(FakeReader(error=AdsReadError("boom")))
    with pytest.raises(AccountLookupError):
        await store.get(CHILD_A)


@pytest.mark.asyncio
async def test_a_failure_is_never_cached() -> None:
    reader = FakeReader(error=AdsReadError("transient"))
    store = _store(reader)

    with pytest.raises(AccountLookupError):
        await store.get(CHILD_A)

    reader.set_error(None)
    reader.set_accounts([_summary(CHILD_A)])

    assert await store.get(CHILD_A) is not None


@pytest.mark.asyncio
async def test_an_empty_manager_is_not_an_error_but_permits_nothing() -> None:
    """A real, readable, empty MCC is a legitimate answer: nothing is managed."""
    store = _store(FakeReader([]))
    assert await store.get(CHILD_A) is None
    assert await store.all() == ()


# ---------------------------------------------------------------------------
# caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_lookups_hit_google_once_within_the_ttl() -> None:
    reader = FakeReader([_summary(CHILD_A)])
    store = _store(reader)

    await store.get(CHILD_A)
    await store.get(CHILD_A)
    await store.get(CHILD_B)

    assert reader.calls == 1


@pytest.mark.asyncio
async def test_the_set_is_refreshed_once_the_ttl_expires() -> None:
    reader = FakeReader([_summary(CHILD_A)])
    clock = {"now": 1000.0}
    store = ManagedAccountStore(
        reader=reader,
        login_customer_id=MCC,
        ttl_seconds=60,
        clock=lambda: clock["now"],
    )

    assert await store.get(CHILD_B) is None

    reader.set_accounts([_summary(CHILD_A), _summary(CHILD_B)])
    clock["now"] += 61

    assert await store.get(CHILD_B) is not None
    assert reader.calls == 2


@pytest.mark.asyncio
async def test_a_zero_ttl_disables_caching() -> None:
    reader = FakeReader([_summary(CHILD_A)])
    store = _store(reader, ttl_seconds=0)

    await store.get(CHILD_A)
    await store.get(CHILD_A)

    assert reader.calls == 2


@pytest.mark.asyncio
async def test_the_set_is_shared_across_callers_rather_than_per_user() -> None:
    """Which accounts the MCC contains is a property of the MCC, not the caller.

    Deriving it per-caller would lock out anyone whose own credential cannot
    read the manager account - which is most of the team, since they hold
    access on a child. The per-user decision is the tier check, not this.
    """
    reader = FakeReader([_summary(CHILD_A)])
    store = _store(reader)

    await store.get(CHILD_A)
    reader.set_error(AdsReadError("this caller cannot read the MCC"))

    assert await store.get(CHILD_A) is not None


# ---------------------------------------------------------------------------
# surviving a caller who cannot read the manager account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_last_known_set() -> None:
    """The bug this fixes, seen in production.

    The set is derived by querying the MCC, which needs access to the MCC. A
    user holding only a direct grant on a sub-account has none, so when their
    request is the one whose turn it is to refresh an expired cache, the
    lookup fails - and failing closed threw away an answer we already had,
    locking them out of an account they legitimately hold access to.

    Serving the stale set grants nobody anything. It says only which accounts
    exist under the manager; the per-user decision is still the tier check,
    run on every call with the caller's own credential.
    """
    reader = FakeReader([_summary(CHILD_A)])
    clock = {"now": 1000.0}
    store = ManagedAccountStore(
        reader=reader, login_customer_id=MCC, ttl_seconds=60,
        clock=lambda: clock["now"],
    )

    assert await store.get(CHILD_A) is not None      # establishes the set

    clock["now"] += 61                                # cache expires
    reader.set_error(AdsReadError("USER_PERMISSION_DENIED"))

    assert await store.get(CHILD_A) is not None, (
        "a caller who cannot read the MCC must still see the accounts a "
        "previous successful lookup established"
    )


@pytest.mark.asyncio
async def test_a_failed_refresh_with_no_previous_set_still_raises() -> None:
    """Falling back is only possible when there is something to fall back to.
    A cold start that cannot reach the MCC is still `we do not know`, never an
    empty set that would read as a correct refusal."""
    store = _store(FakeReader(error=AdsReadError("USER_PERMISSION_DENIED")))
    with pytest.raises(AccountLookupError):
        await store.get(CHILD_A)


@pytest.mark.asyncio
async def test_a_later_success_replaces_the_stale_set() -> None:
    """Stale is a fallback, not a resting state - the next caller who CAN read
    the manager refreshes it for everyone."""
    reader = FakeReader([_summary(CHILD_A)])
    clock = {"now": 1000.0}
    store = ManagedAccountStore(
        reader=reader, login_customer_id=MCC, ttl_seconds=60,
        clock=lambda: clock["now"],
    )
    await store.get(CHILD_A)

    clock["now"] += 61
    reader.set_error(AdsReadError("USER_PERMISSION_DENIED"))
    assert await store.get(CHILD_B) is None          # serving the stale set

    clock["now"] += 61
    reader.set_error(None)
    reader.set_accounts([_summary(CHILD_A), _summary(CHILD_B)])
    assert await store.get(CHILD_B) is not None      # refreshed
