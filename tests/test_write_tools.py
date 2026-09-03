"""Draft and confirm, end to end, through a real FastMCP server.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy file, the registry, the middleware and the audit log.

The properties being proved are the ones that make a two-step confirm worth
having. A plan must be intent and never authority - so it is re-checked, it
expires, it belongs to one person, and it can be spent exactly once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import FakeBudgetReader, FakeManagedAccounts
import yaml
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult

from gads_write.ads.executor import MutationResult
from gads_write.ads.reads import AdsReadError, CampaignSummary
from gads_write.auth.tiers import Tier
from gads_write.mcp_middleware import TierMiddleware
from gads_write.safety.audit import AuditLog
from gads_write.safety.guards import Guard
from gads_write.safety.plans import PlanStore
from gads_write.safety.policy import PolicyStore
from gads_write.safety.spend import DailySpendLedger
from gads_write.settings import Settings
from gads_write.tools.confirm import register_confirm_tool
from gads_write.tools.writes import register_write_tools

ACCOUNT = "1234567890"
UNMANAGED = "5555555555"
CAMPAIGN = "55"


@dataclass(frozen=True)
class FakeCaller:
    email: str = "op@example.com"
    name: str | None = "Operator"


class CallerBox:
    """A caller the test can swap mid-session, like a second person calling."""

    def __init__(self, email: str = "op@example.com") -> None:
        self.email = email

    def __call__(self) -> FakeCaller:
        return FakeCaller(self.email)


class MutableTier:
    def __init__(self, tier: Tier) -> None:
        self.tier = tier

    async def resolve(self, caller, customer_id):
        return self.tier

    async def visible_tier(self, caller):
        return self.tier

    @property
    def source(self) -> str:
        return "mutable"


class FakeReader:
    def __init__(self, status: str = "ENABLED") -> None:
        self.status = status
        self.missing = False
        self.explode = False

    async def campaign_by_id(self, *, customer_id, campaign_id):
        if self.explode:
            raise AdsReadError("DEADLINE_EXCEEDED")
        if self.missing:
            return None
        return CampaignSummary(
            campaign_id=campaign_id,
            name="Brand - Exact",
            status=self.status,
            channel_type="SEARCH",
            daily_budget_micros=12_000_000_000,
        )

    # unused by the write path
    async def accessible_customer_ids(self): return ()
    async def account_summary(self, customer_id): return None
    async def access_role(self, **kw): return None
    async def campaign_performance(self, **kw): return ()
    async def search_terms(self, **kw): return ()


class FakeExecutor:
    def __init__(self) -> None:
        self.applied: list = []
        self.explode: Exception | None = None
        self.fail_result = False

    async def apply(self, request):
        self.applied.append(request)
        if self.explode:
            raise self.explode
        if self.fail_result:
            return MutationResult(success=False, error="INVALID_CAMPAIGN")
        return MutationResult(
            success=True,
            resource_names=(f"customers/{request.customer_id}/campaigns/{CAMPAIGN}",),
            details={"operation": request.operation},
        )


class StepClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Harness:
    mcp: FastMCP
    reader: FakeReader
    executor: FakeExecutor
    tiers: MutableTier
    clock: StepClock
    audit_path: Path
    settings: Settings
    caller: CallerBox


@pytest.fixture
def harness(tmp_path):
    def _build(
        tier: Tier = Tier.OPERATOR,
        *,
        write_enabled: bool = True,
        status: str = "ENABLED",
    ) -> Harness:
        settings = Settings(
            env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
            oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
            jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
            write_enabled=write_enabled,
            roles_path=tmp_path / "roles.yaml",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        policy_store = PolicyStore(settings)
        audit_log = AuditLog(settings.audit_log_path)
        tiers = MutableTier(tier)
        guard = Guard(
            settings=settings,
            policy_store=policy_store,
            tier_resolver=tiers,
            audit_log=audit_log,
            spend_ledger=DailySpendLedger(audit_log),
            managed_accounts=FakeManagedAccounts(),
        reader=FakeBudgetReader(),
        )
        clock = StepClock()
        plan_store = PlanStore(clock=clock)
        reader = FakeReader(status=status)
        executor = FakeExecutor()

        caller = CallerBox()
        mcp = FastMCP(name="test")
        mcp.add_middleware(
            TierMiddleware(
                tier_resolver=tiers, settings=settings, caller_provider=caller
            )
        )
        register_write_tools(
            mcp, guard=guard, reader=reader, policy_store=policy_store,
            plan_store=plan_store, caller_provider=caller,
        )
        register_confirm_tool(
            mcp, guard=guard, executor=executor, plan_store=plan_store,
            settings=settings,
            reader=reader, caller_provider=caller,
        )
        return Harness(
            mcp=mcp, reader=reader, executor=executor, tiers=tiers, clock=clock, audit_path=settings.audit_log_path,
            settings=settings, caller=caller,
        )

    return _build


async def _approve(message, response_type, params, context):
    """Stand in for a person clicking Apply.

    These tests run with the production default (human confirmation
    required), so confirm_and_apply blocks on elicitation. Approving here
    keeps each test about the thing it is actually testing; whether approval
    is enforced at all is covered in test_human_confirmation.py.
    """
    return ElicitResult(action="accept", content=None)


async def _call(mcp, tool, args) -> dict:
    async with Client(mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


def _audit(path: Path) -> list[dict]:
    """Read through the log's own interface, not the file layout."""
    return list(AuditLog(path).iter_records())


# ---------------------------------------------------------------------------
# drafting changes nothing
# ---------------------------------------------------------------------------

async def test_drafting_returns_a_plan_and_applies_nothing(harness) -> None:
    """The core promise of the whole design."""
    h = harness()
    payload = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )

    assert payload["plan_id"]
    assert payload["current_status"] == "ENABLED"
    assert payload["target_status"] == "PAUSED"
    assert h.executor.applied == []


async def test_the_preview_shows_what_changes_from_and_to(harness) -> None:
    h = harness()
    payload = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    preview = payload["preview"]

    assert "Brand - Exact" in preview
    assert "ENABLED -> PAUSED" in preview
    assert "12,000" in preview  # daily budget, converted from micros


async def test_enabling_warns_that_spending_resumes(harness) -> None:
    h = harness(status="PAUSED")
    payload = await _call(
        h.mcp, "enable_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    assert "lets it spend up to" in payload["preview"]


async def test_a_no_op_does_not_create_a_plan(harness) -> None:
    """Confirming a change that changes nothing would pollute the audit log."""
    h = harness(status="PAUSED")
    payload = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    assert payload["no_change_needed"] is True
    assert "plan_id" not in payload


async def test_a_removed_campaign_cannot_be_enabled(harness) -> None:
    """Removal is terminal in Google Ads. Say so clearly rather than letting
    the API reject it after someone has already approved a plan."""
    h = harness(status="REMOVED")
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "enable_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
        )
    assert "REMOVED" in str(caught.value)
    assert "permanent" in str(caught.value)
    assert h.executor.applied == []


async def test_a_missing_campaign_is_caught_before_a_plan_exists(harness) -> None:
    h = harness()
    h.reader.missing = True
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": "999"}
        )
    assert "No campaign 999" in str(caught.value)


async def test_a_failed_read_does_not_produce_a_plan(harness) -> None:
    h = harness()
    h.reader.explode = True
    with pytest.raises(Exception):
        await _call(
            h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
        )


# ---------------------------------------------------------------------------
# confirming applies
# ---------------------------------------------------------------------------

async def test_confirm_applies_the_change(harness) -> None:
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert applied["applied"] is True
    assert applied["resource_names"] == [f"customers/{ACCOUNT}/campaigns/{CAMPAIGN}"]
    assert len(h.executor.applied) == 1
    request = h.executor.applied[0]
    assert request.operation == "pause_campaign"
    assert request.customer_id == ACCOUNT
    assert request.payload["campaign_id"] == CAMPAIGN
    assert request.validate_only is False


async def test_a_plan_can_only_be_applied_once(harness) -> None:
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "single-use" in str(caught.value) or "already applied" in str(caught.value)
    assert len(h.executor.applied) == 1


async def test_someone_else_cannot_confirm_your_plan(harness) -> None:
    """A plan_id pasted into a shared channel is inert to everyone else.

    Same server, same plan store, same tier - only the authenticated identity
    differs, which is exactly the situation a leaked plan_id creates.
    """
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )

    h.caller.email = "someone.else@example.com"

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "drafted by someone else" in str(caught.value)
    assert h.executor.applied == []

    # And it still works for the person who drafted it.
    h.caller.email = "op@example.com"
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_an_expired_plan_is_refused(harness) -> None:
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    h.clock.advance(601)  # policy ttl is 600s

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "expired" in str(caught.value)
    assert h.executor.applied == []


async def test_an_unknown_plan_id_is_refused(harness) -> None:
    h = harness()
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": "not-a-real-plan"})
    assert h.executor.applied == []


# ---------------------------------------------------------------------------
# re-evaluation at confirm time
# ---------------------------------------------------------------------------

async def test_a_demotion_between_draft_and_confirm_refuses_the_apply(harness) -> None:
    """A plan carries intent, never authority."""
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )

    h.tiers.tier = Tier.READONLY  # removed from the account in Google Ads

    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


# test_tightening_policy_between_draft_and_confirm_refuses_the_apply stood
# here. It added pause_campaign to blocked_operations mid-flight and proved
# the confirm re-read the file rather than remembering the draft's verdict.
# The blocked list is fixed in code now, so there is nothing to edit.
#
# The property it proved - policy is RE-EVALUATED at confirm, never carried in
# the plan - is still covered end to end by the demotion test above, by the
# ceiling-tightening tests in test_phase5_tools.py, and by the re-read tests
# that refuse a plan once the current budget has moved.


async def test_a_refused_confirm_does_not_burn_the_plan(harness) -> None:
    """Refused by policy is not the same as used up.

    If the demotion is reversed a minute later, the same plan must still work.
    """
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )

    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    h.tiers.tier = Tier.OPERATOR  # restored
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


# ---------------------------------------------------------------------------
# tier and kill switch
# ---------------------------------------------------------------------------

async def test_readonly_cannot_draft_a_write(harness) -> None:
    h = harness(Tier.READONLY)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
        )
    assert "requires tier operator" in str(caught.value)


async def test_the_kill_switch_hides_and_refuses_every_write(harness) -> None:
    h = harness(write_enabled=False)
    async with Client(h.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "pause_campaign" not in names
    assert "confirm_and_apply" not in names

    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
        )
    assert "GADS_WRITE_ENABLED" in str(caught.value)


async def test_an_unmanaged_account_cannot_be_drafted_against(harness) -> None:
    h = harness()
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "pause_campaign", {"customer_id": UNMANAGED, "campaign_id": CAMPAIGN}
        )
    assert UNMANAGED in str(caught.value)


async def test_a_malformed_campaign_id_is_refused(harness) -> None:
    h = harness()
    with pytest.raises(Exception):
        await _call(
            h.mcp,
            "pause_campaign",
            {"customer_id": ACCOUNT, "campaign_id": "55 OR 1=1"},
        )


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

async def test_an_applied_change_is_audited_with_its_resource_names(harness) -> None:
    h = harness()
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    lines = _audit(h.audit_path)
    applied = [l for l in lines if l.get("applied") is True]
    assert len(applied) == 1
    assert applied[0]["tool"] == "pause_campaign"
    assert applied[0]["plan_id"] == draft["plan_id"]
    assert applied[0]["resource_names"] == [
        f"customers/{ACCOUNT}/campaigns/{CAMPAIGN}"
    ]
    assert applied[0]["user_email"] == "op@example.com"


async def test_a_failed_apply_is_audited_and_the_plan_is_burnt(harness) -> None:
    """The safe failure mode: we cannot know whether it reached Google, so
    nobody gets to retry blindly."""
    h = harness()
    h.executor.explode = RuntimeError("connection reset")
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "connection reset" in str(caught.value)

    errors = [l for l in _audit(h.audit_path) if l.get("error")]
    assert len(errors) == 1
    assert "connection reset" in errors[0]["error"]

    # burnt: a second attempt is refused as already used
    h.executor.explode = None
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})


async def test_a_mutation_reporting_failure_is_not_reported_as_success(
    harness,
) -> None:
    h = harness()
    h.executor.fail_result = True
    draft = await _call(
        h.mcp, "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
    )
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "INVALID_CAMPAIGN" in str(caught.value)
