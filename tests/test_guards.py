"""The gate. Every check, its order, and what it writes to the audit log."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gads_write.auth.tiers import Tier, TierLookupError
from gads_write.safety.audit import AuditLog
from gads_write.safety.guards import (
    VERDICT_ALLOWED,
    VERDICT_DENIED,
    VERDICT_LOOKUP_FAILED,
    Guard,
    GuardDenied,
)
from gads_write.safety.accounts import AccountLookupError, ManagedAccount
from gads_write.safety.policy import (
    PolicyStore,
    evaluate_bid_change,
    evaluate_budget_change,
)
from gads_write.safety.spend import DailySpendLedger
from gads_write.safety.validators import ValidationResult
from gads_write.tools.registry import ToolSpec, register, reset_for_tests

NOW = datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc)  # 09:30 Kolkata
ACCOUNT = "1234567890"
OTHER_ACCOUNT = "9999999999"


@dataclass(frozen=True)
class FakeCaller:
    email: str
    name: str = "Test User"


class FakeTierResolver:
    """Returns a tier per account, or raises to simulate an outage."""

    def __init__(self, tiers: dict[str, Tier] | Tier, *, raises: bool = False) -> None:
        self._tiers = tiers
        self._raises = raises
        self.resolve_calls = 0

    async def resolve(self, caller, customer_id: str) -> Tier:
        self.resolve_calls += 1
        if self._raises:
            raise TierLookupError("Google Ads API timed out")
        if isinstance(self._tiers, Tier):
            return self._tiers
        return self._tiers.get(customer_id, Tier.NONE)

    async def visible_tier(self, caller) -> Tier:
        if self._raises:
            raise TierLookupError("Google Ads API timed out")
        if isinstance(self._tiers, Tier):
            return self._tiers
        return max(self._tiers.values(), key=lambda t: list(Tier).index(t), default=Tier.NONE)

    @property
    def source(self) -> str:
        return "fake"


class FakeAccounts:
    """The managed-account set, without Google.

    Mirrors ManagedAccountStore: `None` means "not ours", an exception means
    "we could not find out".
    """

    def __init__(self, accounts=None, *, raises: bool = False) -> None:
        if accounts is None:
            accounts = {ACCOUNT: ("INR", "Asia/Kolkata")}
        self._accounts = accounts
        self._raises = raises
        self.calls = 0

    async def get(self, customer_id: str):
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
            descriptive_name="Test Account",
            is_manager=False,
        )

    async def all(self):
        return tuple(
            ManagedAccount(
                customer_id=cid,
                currency_code=cur,
                timezone=tz,
                descriptive_name="Test Account",
                is_manager=False,
            )
            for cid, (cur, tz) in sorted(self._accounts.items())
        )


@pytest.fixture(autouse=True)
def _tools():
    """A tool set to gate. Restored after every test."""
    reset_for_tests()
    # update_campaign_budget is a real registry builtin from Phase 5, so it
    # is not re-registered here; these tests run against the production spec.
    register(ToolSpec(name="get_campaigns", required_tier=Tier.READONLY, writes=False))
    register(
        ToolSpec(
            name="remove_campaign",
            required_tier=Tier.LEAD,
            writes=True,
            operation="remove_campaign",  # on the policy blocklist
        )
    )
    yield
    reset_for_tests()


def _guard(
    tmp_path: Path,
    *,
    tier=Tier.OPERATOR,
    write_enabled: bool = True,
    raises: bool = False,
    ledger=None,
    accounts=None,
) -> tuple[Guard, AuditLog, PolicyStore]:
    from gads_write.settings import Settings

    settings = Settings(
        env="test",
        host="127.0.0.1",
        port=8081,
        base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com",
        oauth_client_secret="s",
        jwt_signing_key="k",
        developer_token="d",
        login_customer_id=ACCOUNT,
        write_enabled=write_enabled,
        roles_path=tmp_path / "roles.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    store = PolicyStore(settings)
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = Guard(
        settings=settings,
        policy_store=store,
        tier_resolver=FakeTierResolver(tier, raises=raises),
        audit_log=audit,
        spend_ledger=ledger if ledger is not None else DailySpendLedger(audit),
        managed_accounts=accounts if accounts is not None else FakeAccounts(),
        now=lambda: NOW,
    )
    return guard, audit, store


class CountingLedger:
    """The real ledger, counting how often the gate consults it.

    `spend_ledger` is a constructor argument of Guard, so this is a stand-in
    at a declared seam rather than a reach into internals.
    """

    def __init__(self, inner: DailySpendLedger) -> None:
        self._inner = inner
        self.reads = 0

    def total_increase_units(self, **kwargs):
        self.reads += 1
        return self._inner.total_increase_units(**kwargs)

    def snapshot(self, **kwargs):
        return self._inner.snapshot(**kwargs)


def _budget_evaluator(current: object, new: object):
    def evaluate(policy, tier, spend_today):
        # `spend_today` is still handed to every evaluator and still lands on
        # the decision, but it is no longer a limit - the draft tools put it
        # on the preview as information. See safety/policy.py.
        return evaluate_budget_change(
            policy, tier=tier, current_units=current, new_units=new
        )

    return evaluate


def _lines(audit: AuditLog) -> list[dict]:
    return list(audit.iter_records())


# ---------------------------------------------------------------------------
# 1. kill switch
# ---------------------------------------------------------------------------

async def test_kill_switch_blocks_every_write(tmp_path) -> None:
    guard, audit, _ = _guard(tmp_path, tier=Tier.LEAD, write_enabled=False)

    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )

    assert not decision.allowed
    assert decision.failed_check == "kill_switch"
    assert "GADS_WRITE_ENABLED" in decision.reason_text


async def test_kill_switch_does_not_block_reads(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path, tier=Tier.READONLY, write_enabled=False)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    assert decision.allowed


async def test_kill_switch_beats_even_a_lead(tmp_path) -> None:
    # It is absolute and tier-independent. That is the point of a kill switch.
    guard, _, _ = _guard(tmp_path, tier=Tier.LEAD, write_enabled=False)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert not decision.allowed


async def test_kill_switch_is_checked_before_the_tier_lookup(tmp_path) -> None:
    # Cheapest first: no reason to hit Google to learn the server is off.
    guard, _, _ = _guard(
        tmp_path, tier=Tier.LEAD, write_enabled=False, raises=True
    )
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "kill_switch"  # not tier_lookup


# ---------------------------------------------------------------------------
# 2. identity
# ---------------------------------------------------------------------------

async def test_no_caller_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path)
    decision = await guard.check(tool="get_campaigns", caller=None, customer_id=ACCOUNT)
    assert not decision.allowed
    assert decision.failed_check == "identity"


async def test_caller_without_an_email_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller(""), customer_id=ACCOUNT
    )
    assert decision.failed_check == "identity"


# ---------------------------------------------------------------------------
# 3. tier
# ---------------------------------------------------------------------------

async def test_tier_below_the_tool_requirement_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path, tier=Tier.READONLY)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    assert not decision.allowed
    assert decision.failed_check == "tier"
    assert "requires tier operator" in decision.reason_text


async def test_tier_is_resolved_for_the_specific_account(tmp_path) -> None:
    """Per-account, not global. The decision you approved."""
    from gads_write.settings import Settings

    audit = AuditLog(tmp_path / "audit.jsonl")
    resolver = FakeTierResolver({ACCOUNT: Tier.OPERATOR, OTHER_ACCOUNT: Tier.READONLY})
    settings = Settings(
        env="test", host="h", port=1, base_url="https://x",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id=ACCOUNT,
        write_enabled=True, roles_path=tmp_path / "r",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    store = PolicyStore(settings)
    guard = Guard(
        settings=settings, policy_store=store, tier_resolver=resolver,
        audit_log=audit, spend_ledger=DailySpendLedger(audit),
        managed_accounts=FakeAccounts(
            {ACCOUNT: ("INR", "Asia/Kolkata"), OTHER_ACCOUNT: ("INR", "Asia/Kolkata")}
        ),
        now=lambda: NOW,
    )
    caller = FakeCaller("a@x.com")

    ok = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=ACCOUNT
    )
    assert ok.allowed

    denied = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=OTHER_ACCOUNT
    )
    assert not denied.allowed
    assert "9999999999" in denied.reason_text


async def test_an_indeterminate_lookup_fails_closed(tmp_path) -> None:
    """A Google outage must never become an escalation path."""
    guard, audit, _ = _guard(tmp_path, raises=True)

    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )

    assert not decision.allowed
    assert decision.tier is Tier.NONE
    # A distinct verdict, so an outage is visible as an outage rather than
    # hiding inside a wave of ordinary refusals.
    assert decision.verdict == VERDICT_LOOKUP_FAILED
    assert _lines(audit)[0]["verdict"] == VERDICT_LOOKUP_FAILED


# ---------------------------------------------------------------------------
# 4 and 5. allowlist, blocked operations
# ---------------------------------------------------------------------------

async def test_an_account_outside_the_manager_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=OTHER_ACCOUNT
    )
    assert decision.failed_check == "managed_account"
    assert OTHER_ACCOUNT in decision.reason_text


async def test_membership_is_checked_before_validation(tmp_path) -> None:
    # An unmanaged account must not have its contents inspected at all.
    guard, _, _ = _guard(tmp_path)

    def validate(policy):
        raise AssertionError("validation must not run for an unmanaged account")

    decision = await guard.check(
        tool="get_campaigns",
        caller=FakeCaller("a@x.com"),
        customer_id=OTHER_ACCOUNT,
        validate=validate,
    )
    assert decision.failed_check == "managed_account"


async def test_an_indeterminate_account_lookup_fails_closed(tmp_path) -> None:
    """Distinct from "not ours", exactly as a failed tier lookup is.

    If a Google outage produced an ordinary denial, the audit log could not
    tell an outage apart from a wave of correct refusals.
    """
    guard, audit, _ = _guard(
        tmp_path, accounts=FakeAccounts(raises=True)
    )
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    assert not decision.allowed
    assert decision.failed_check == "managed_account_lookup"
    assert decision.verdict == VERDICT_LOOKUP_FAILED


async def test_the_account_currency_reaches_the_policy_message(
    tmp_path
) -> None:
    """Currency is per account now, not one value for the whole server."""
    guard, _, _ = _guard(
        tmp_path,
        accounts=FakeAccounts({ACCOUNT: ("USD", "America/New_York")}),
    )

    seen: dict = {}

    def evaluate(policy, tier, spend_today):
        seen["currency"] = policy.currency_code
        return evaluate_budget_change(
            policy, tier=tier, current_units=100, new_units=100_000
        )

    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        evaluate=evaluate,
    )
    assert seen["currency"] == "USD"
    assert "USD" in decision.reason_text


async def test_the_audit_date_uses_the_account_timezone(tmp_path) -> None:
    """The daily ceiling groups by this date, so it must follow the account.

    NOW is 04:00 UTC on the 15th - already the 15th in Kolkata, still the
    14th in New York.
    """
    guard_kolkata, _, _ = _guard(
        tmp_path, accounts=FakeAccounts({ACCOUNT: ("INR", "Asia/Kolkata")})
    )
    guard_ny, _, _ = _guard(
        tmp_path,
        accounts=FakeAccounts({ACCOUNT: ("USD", "America/New_York")}),
    )

    kolkata = await guard_kolkata.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    new_york = await guard_ny.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )

    assert kolkata.local_date == "2026-01-15"
    assert new_york.local_date == "2026-01-14"


async def test_blocked_operation_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path, tier=Tier.LEAD)
    decision = await guard.check(
        tool="remove_campaign", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "blocked_operation"


async def test_an_unregistered_tool_is_refused(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path, tier=Tier.LEAD)
    decision = await guard.check(
        tool="whatever_someone_forgot", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "registry"


# ---------------------------------------------------------------------------
# 6 and 7. validation, policy, ceiling
# ---------------------------------------------------------------------------

async def test_validation_failure_is_refused_with_every_problem(
    tmp_path
) -> None:
    guard, _, _ = _guard(tmp_path)

    def validate(policy):
        result = ValidationResult()
        result.add("headline", "too long")
        result.add("final_url", "not https")
        return result

    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        validate=validate,
    )
    assert decision.failed_check == "validation"
    assert len(decision.reasons) == 2


async def test_policy_limit_is_enforced_at_the_gate(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        # Past the typo backstop: 1,000 -> 50,000 is a fiftyfold jump.
        evaluate=_budget_evaluator(1000, 50000),
    )
    assert decision.failed_check == "policy"


async def test_a_change_within_every_limit_is_allowed(tmp_path) -> None:
    guard, audit, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
        spend_delta_units=Decimal(200),
    )
    assert decision.allowed
    assert decision.tier is Tier.OPERATOR


# The two daily-ceiling tests that stood here are gone with the rule. It
# refused past a percentage of the account's total, which blocked ordinary
# work - raising five campaigns for a seasonal push stopped after the second.
# The running total is still computed here and rides out on the decision;
# tools/writes.py puts it on the preview instead of refusing.


async def test_the_running_total_still_reaches_the_decision(tmp_path) -> None:
    """Not a limit any more, but the draft preview shows it, so it has to be
    on the decision for the tool to read."""
    guard, _, _ = _guard(tmp_path)
    caller = FakeCaller("a@x.com")

    first = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        first, arguments={}, plan_id=None, resource_names=[],
        spend_delta_units=Decimal(200),
    )

    second = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1200, 1400),
    )

    assert second.allowed
    assert second.spend_today_units == Decimal(200)


async def test_another_users_spending_does_not_count_against_you(
    tmp_path
) -> None:
    guard, _, _ = _guard(tmp_path)

    other = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("other@x.com"),
        customer_id=ACCOUNT, evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        other, arguments={}, plan_id=None, resource_names=[],
        spend_delta_units=Decimal(2900),
    )

    mine = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("me@x.com"),
        customer_id=ACCOUNT, evaluate=_budget_evaluator(1000, 1200),
    )
    assert mine.allowed


# ---------------------------------------------------------------------------
# the confirm-time re-evaluation
# ---------------------------------------------------------------------------

# NOTE: two tests were removed here with the policy file itself.
#
#   test_a_plan_drafted_under_old_limits_fails_after_a_tightening
#   test_a_broken_policy_edit_does_not_open_the_gate
#
# Both exercised policy.yaml hot-reload: a lead tightening a limit between
# draft and confirm, and a mistyped edit not relaxing the gate. Neither is
# reachable now - the limits are relative, fixed in code, and there is no
# file to edit or to mistype.
#
# The property they were really protecting - that confirm RE-EVALUATES rather
# than trusting the plan - is still pinned, by the demotion and re-read
# regression tests in tests/test_phase5_tools.py.


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

async def test_every_decision_writes_exactly_one_line(tmp_path) -> None:
    guard, audit, _ = _guard(tmp_path)
    await guard.check(tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT)
    await guard.check(tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=OTHER_ACCOUNT)
    assert len(_lines(audit)) == 2


async def test_refusals_are_logged_not_just_successes(tmp_path) -> None:
    # How you notice someone repeatedly pushing at a cap, or a limit set too
    # tight for the team to work.
    guard, audit, _ = _guard(tmp_path, tier=Tier.READONLY)
    await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    line = _lines(audit)[0]
    assert line["verdict"] == VERDICT_DENIED
    assert line["applied"] is False
    assert line["denied_reasons"]


async def test_the_spend_ledger_is_not_read_when_no_rule_needs_it(
    tmp_path
) -> None:
    """A read must not pay for the daily spend ceiling.

    The ceiling is rebuilt from the audit log, so consulting it on every
    single gate check made every tool call - including reads, which have no
    spend to check - slower every day the log grew.
    """
    audit = AuditLog(tmp_path / "audit.jsonl")
    ledger = CountingLedger(DailySpendLedger(audit))
    guard, _, _ = _guard(tmp_path, ledger=ledger)

    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )

    assert decision.allowed
    assert ledger.reads == 0


async def test_the_spend_ledger_is_read_when_a_rule_does_need_it(
    tmp_path
) -> None:
    """The other half: a budget change still gets its running daily total."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    ledger = CountingLedger(DailySpendLedger(audit))
    guard, _, _ = _guard(tmp_path, ledger=ledger)

    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        evaluate=_budget_evaluator(100, 110),
    )

    assert decision.allowed
    assert ledger.reads == 1


async def test_audit_arguments_are_redacted_by_the_gate(tmp_path) -> None:
    guard, audit, _ = _guard(tmp_path)
    await guard.check(
        tool="get_campaigns",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        arguments={"access_token": "ya29.live", "campaign_id": "111"},
    )
    records = _lines(audit)
    # Re-serialise what was actually persisted: if the token survived
    # anywhere in the record, it shows up here.
    assert "ya29.live" not in json.dumps(records)
    assert records[0]["arguments"]["campaign_id"] == "111"


async def test_a_denied_decision_raises_when_asked(tmp_path) -> None:
    guard, _, _ = _guard(tmp_path, tier=Tier.READONLY)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    with pytest.raises(GuardDenied):
        decision.raise_if_denied()


async def test_record_application_marks_the_line_applied(tmp_path) -> None:
    guard, audit, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        decision,
        arguments={"new_daily_budget": 1200},
        plan_id="plan-1",
        resource_names=["customers/1234567890/campaignBudgets/9"],
        spend_delta_units=Decimal(200),
    )

    applied = [line for line in _lines(audit) if line["applied"]]
    assert len(applied) == 1
    assert applied[0]["verdict"] == VERDICT_ALLOWED
    assert applied[0]["spend_delta_units"] == "200"
    assert applied[0]["resource_names"] == ["customers/1234567890/campaignBudgets/9"]


async def test_a_failed_application_is_not_counted_as_applied(
    tmp_path
) -> None:
    guard, audit, _ = _guard(tmp_path)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        decision, arguments={}, plan_id="p", resource_names=[],
        spend_delta_units=Decimal(200), error="RESOURCE_EXHAUSTED",
    )
    assert not any(line["applied"] for line in _lines(audit))


# NOTE: four tests stood here covering the daily-ceiling base - that it was
# fetched only for rules needing it, and that a failed read refused the
# change. The ceiling is no longer a limit (it blocked ordinary work; the
# running total is shown on the preview instead), so there is no base to
# fetch and nothing to gate on. See safety/policy.py.
