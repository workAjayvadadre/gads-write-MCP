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
from gads_write.safety.policy import PolicyStore, evaluate_budget_change
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


@pytest.fixture(autouse=True)
def _tools():
    """A tool set to gate. Restored after every test."""
    reset_for_tests()
    register(ToolSpec(name="get_campaigns", required_tier=Tier.READONLY, writes=False))
    register(
        ToolSpec(
            name="update_campaign_budget",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="update_campaign_budget",
        )
    )
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
    write_policy,
    *,
    tier=Tier.OPERATOR,
    write_enabled: bool = True,
    raises: bool = False,
    policy_patch: dict | None = None,
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
        policy_path=tmp_path / "policy.yaml",
        roles_path=tmp_path / "roles.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    store = PolicyStore(write_policy(policy_patch))
    audit = AuditLog(tmp_path / "audit.jsonl")
    guard = Guard(
        settings=settings,
        policy_store=store,
        tier_resolver=FakeTierResolver(tier, raises=raises),
        audit_log=audit,
        spend_ledger=DailySpendLedger(audit),
        now=lambda: NOW,
    )
    return guard, audit, store


def _budget_evaluator(current: object, new: object):
    def evaluate(policy, tier, spend_today):
        return evaluate_budget_change(
            policy,
            tier=tier,
            current_units=current,
            new_units=new,
            already_increased_today_units=spend_today,
        )

    return evaluate


def _lines(audit: AuditLog) -> list[dict]:
    return list(audit.iter_records())


# ---------------------------------------------------------------------------
# 1. kill switch
# ---------------------------------------------------------------------------

async def test_kill_switch_blocks_every_write(tmp_path, write_policy) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy, tier=Tier.LEAD, write_enabled=False)

    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )

    assert not decision.allowed
    assert decision.failed_check == "kill_switch"
    assert "GADS_WRITE_ENABLED" in decision.reason_text


async def test_kill_switch_does_not_block_reads(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.READONLY, write_enabled=False)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    assert decision.allowed


async def test_kill_switch_beats_even_a_lead(tmp_path, write_policy) -> None:
    # It is absolute and tier-independent. That is the point of a kill switch.
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.LEAD, write_enabled=False)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert not decision.allowed


async def test_kill_switch_is_checked_before_the_tier_lookup(tmp_path, write_policy) -> None:
    # Cheapest first: no reason to hit Google to learn the server is off.
    guard, _, _ = _guard(
        tmp_path, write_policy, tier=Tier.LEAD, write_enabled=False, raises=True
    )
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "kill_switch"  # not tier_lookup


# ---------------------------------------------------------------------------
# 2. identity
# ---------------------------------------------------------------------------

async def test_no_caller_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(tool="get_campaigns", caller=None, customer_id=ACCOUNT)
    assert not decision.allowed
    assert decision.failed_check == "identity"


async def test_caller_without_an_email_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller(""), customer_id=ACCOUNT
    )
    assert decision.failed_check == "identity"


# ---------------------------------------------------------------------------
# 3. tier
# ---------------------------------------------------------------------------

async def test_tier_below_the_tool_requirement_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.READONLY)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    assert not decision.allowed
    assert decision.failed_check == "tier"
    assert "requires tier operator" in decision.reason_text


async def test_tier_is_resolved_for_the_specific_account(tmp_path, write_policy) -> None:
    """Per-account, not global. The decision you approved."""
    from gads_write.settings import Settings

    store = PolicyStore(write_policy({"allowed_customer_ids": [ACCOUNT, OTHER_ACCOUNT]}))
    audit = AuditLog(tmp_path / "audit.jsonl")
    resolver = FakeTierResolver({ACCOUNT: Tier.OPERATOR, OTHER_ACCOUNT: Tier.READONLY})
    settings = Settings(
        env="test", host="h", port=1, base_url="https://x",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id=ACCOUNT,
        write_enabled=True, policy_path=tmp_path / "p", roles_path=tmp_path / "r",
        audit_log_path=tmp_path / "audit.jsonl",
    )
    guard = Guard(
        settings=settings, policy_store=store, tier_resolver=resolver,
        audit_log=audit, spend_ledger=DailySpendLedger(audit), now=lambda: NOW,
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


async def test_an_indeterminate_lookup_fails_closed(tmp_path, write_policy) -> None:
    """A Google outage must never become an escalation path."""
    guard, audit, _ = _guard(tmp_path, write_policy, raises=True)

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

async def test_account_not_on_the_allowlist_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(
        tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=OTHER_ACCOUNT
    )
    assert decision.failed_check == "account_allowlist"
    assert "no wildcard" in decision.reason_text


async def test_allowlist_is_checked_before_validation(tmp_path, write_policy) -> None:
    # An unmanaged account must not have its contents inspected at all.
    guard, _, _ = _guard(tmp_path, write_policy)

    def validate(policy):
        raise AssertionError("validation must not run for an unmanaged account")

    decision = await guard.check(
        tool="get_campaigns",
        caller=FakeCaller("a@x.com"),
        customer_id=OTHER_ACCOUNT,
        validate=validate,
    )
    assert decision.failed_check == "account_allowlist"


async def test_blocked_operation_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.LEAD)
    decision = await guard.check(
        tool="remove_campaign", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "blocked_operation"


async def test_an_unregistered_tool_is_refused(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.LEAD)
    decision = await guard.check(
        tool="whatever_someone_forgot", caller=FakeCaller("lead@x.com"), customer_id=ACCOUNT
    )
    assert decision.failed_check == "registry"


# ---------------------------------------------------------------------------
# 6 and 7. validation, policy, ceiling
# ---------------------------------------------------------------------------

async def test_validation_failure_is_refused_with_every_problem(
    tmp_path, write_policy
) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)

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


async def test_policy_limit_is_enforced_at_the_gate(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 5000),  # way over operator's 20%
    )
    assert decision.failed_check == "policy"


async def test_a_change_within_every_limit_is_allowed(tmp_path, write_policy) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(
        tool="update_campaign_budget",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
        spend_delta_units=Decimal(200),
    )
    assert decision.allowed
    assert decision.tier is Tier.OPERATOR


async def test_the_daily_ceiling_reaches_the_gate(tmp_path, write_policy) -> None:
    """Today's applied increases are read from the audit log and enforced."""
    guard, audit, _ = _guard(tmp_path, write_policy)
    caller = FakeCaller("a@x.com")

    # Record 2,900 of the operator's 3,000 daily allowance as already applied.
    decision = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        decision, arguments={}, plan_id=None, resource_names=[],
        spend_delta_units=Decimal(2900),
    )

    blocked = await guard.check(
        tool="update_campaign_budget", caller=caller, customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),  # +200 -> 3100, over the cap
    )
    assert not blocked.allowed
    assert "daily ceiling" in blocked.reason_text


async def test_another_users_spending_does_not_count_against_you(
    tmp_path, write_policy
) -> None:
    guard, _, _ = _guard(tmp_path, write_policy)

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

async def test_a_plan_drafted_under_old_limits_fails_after_a_tightening(
    tmp_path, write_policy
) -> None:
    """End to end through the gate, not just the policy module.

    Drafted while the cap allowed it. The lead then tightens policy.yaml.
    The identical check at confirm time refuses, because the gate reads
    PolicyStore.current() rather than anything captured at draft time.
    """
    guard, _, _ = _guard(tmp_path, write_policy)
    caller = FakeCaller("a@x.com")

    drafted = dict(
        tool="update_campaign_budget",
        caller=caller,
        customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    assert (await guard.check(**drafted)).allowed

    write_policy({"limits": {"defaults": {"budget": {"max_daily": 1000}},
                             "tiers": {"operator": {"budget": {"max_daily": 1000}}}}})

    after = await guard.check(**drafted)
    assert not after.allowed
    assert after.failed_check == "policy"


async def test_a_broken_policy_edit_does_not_open_the_gate(tmp_path, write_policy) -> None:
    guard, _, store = _guard(tmp_path, write_policy)
    store.path.write_text("broken: [\n", encoding="utf-8")

    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 5000),
    )
    assert not decision.allowed  # last good policy still in force
    assert store.last_error is not None


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

async def test_every_decision_writes_exactly_one_line(tmp_path, write_policy) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy)
    await guard.check(tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT)
    await guard.check(tool="get_campaigns", caller=FakeCaller("a@x.com"), customer_id=OTHER_ACCOUNT)
    assert len(_lines(audit)) == 2


async def test_refusals_are_logged_not_just_successes(tmp_path, write_policy) -> None:
    # How you notice someone repeatedly pushing at a cap, or a limit set too
    # tight for the team to work.
    guard, audit, _ = _guard(tmp_path, write_policy, tier=Tier.READONLY)
    await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    line = _lines(audit)[0]
    assert line["verdict"] == VERDICT_DENIED
    assert line["applied"] is False
    assert line["denied_reasons"]


async def test_audit_arguments_are_redacted_by_the_gate(tmp_path, write_policy) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy)
    await guard.check(
        tool="get_campaigns",
        caller=FakeCaller("a@x.com"),
        customer_id=ACCOUNT,
        arguments={"access_token": "ya29.live", "campaign_id": "111"},
    )
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "ya29.live" not in raw
    assert json.loads(raw.splitlines()[0])["arguments"]["campaign_id"] == "111"


async def test_a_denied_decision_raises_when_asked(tmp_path, write_policy) -> None:
    guard, _, _ = _guard(tmp_path, write_policy, tier=Tier.READONLY)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT
    )
    with pytest.raises(GuardDenied):
        decision.raise_if_denied()


async def test_record_application_marks_the_line_applied(tmp_path, write_policy) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy)
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
    tmp_path, write_policy
) -> None:
    guard, audit, _ = _guard(tmp_path, write_policy)
    decision = await guard.check(
        tool="update_campaign_budget", caller=FakeCaller("a@x.com"), customer_id=ACCOUNT,
        evaluate=_budget_evaluator(1000, 1200),
    )
    guard.record_application(
        decision, arguments={}, plan_id="p", resource_names=[],
        spend_delta_units=Decimal(200), error="RESOURCE_EXHAUSTED",
    )
    assert not any(line["applied"] for line in _lines(audit))
