"""Shared fixtures.

There is no policy file any more, so there is no policy fixture. The spending
rules come from Settings and code constants; tests that care about a limit set
it on the Settings object they build.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

def _deep_update(base: dict, patch: dict) -> dict:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_update(out[key], value)
        else:
            out[key] = value
    return out


# Break-glass overrides. Normally empty in production; the fixture carries a
# couple so tests can exercise the override path.
BASE_ROLES: dict = {
    "users": {"lead@example.com": "lead", "op@example.com": "operator"},
}


@pytest.fixture
def write_roles(tmp_path: Path):
    """Write a roles file, optionally patched. Returns its path."""

    def _write(patch: dict | None = None, *, name: str = "roles.yaml") -> Path:
        patch = dict(patch or {})
        # `users` is REPLACED wholesale, not merged. Deep-merging it would
        # silently keep the base fixture's lead in a test that is trying to
        # prove what happens when there is no lead.
        users = patch.pop("users", None)
        data = _deep_update(BASE_ROLES, patch)
        if users is not None:
            data["users"] = users
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return path

    return _write


class FakeClock:
    """A monotonic clock the test drives by hand, instead of sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# managed accounts
# ---------------------------------------------------------------------------
# The set of accounts under the MCC used to be `allowed_customer_ids` in
# policy.yaml. It is now derived from Google, so tests inject this stand-in
# at the same seam the server injects ManagedAccountStore.

DEFAULT_TEST_ACCOUNTS: dict = {"1234567890": ("INR", "Asia/Kolkata")}


class FakeManagedAccounts:
    """Mirrors ManagedAccountStore: None means "not ours", raising means
    "we could not find out"."""

    def __init__(self, accounts: dict | None = None, *, raises: bool = False) -> None:
        self._accounts = (
            DEFAULT_TEST_ACCOUNTS if accounts is None else dict(accounts)
        )
        self._raises = raises
        self.calls = 0

    async def get(self, customer_id: str):
        from gads_write.safety.accounts import AccountLookupError, ManagedAccount

        self.calls += 1
        if self._raises:
            raise AccountLookupError("Google Ads API timed out")
        entry = self._accounts.get(str(customer_id).strip())
        if entry is None:
            return None
        currency, tz = entry
        return ManagedAccount(
            customer_id=str(customer_id).strip(),
            currency_code=currency,
            timezone=tz,
            descriptive_name=f"Account {customer_id}",
            is_manager=False,
        )

    async def all(self):
        from gads_write.safety.accounts import ManagedAccount

        if self._raises:
            from gads_write.safety.accounts import AccountLookupError

            raise AccountLookupError("Google Ads API timed out")
        return tuple(
            ManagedAccount(
                customer_id=cid,
                currency_code=cur,
                timezone=tz,
                descriptive_name=f"Account {cid}",
                is_manager=False,
            )
            for cid, (cur, tz) in sorted(self._accounts.items())
        )


@pytest.fixture
def managed_accounts() -> FakeManagedAccounts:
    return FakeManagedAccounts()
