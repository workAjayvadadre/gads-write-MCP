"""A human must actually see the change before it is applied.

The hole this closes: `pause_campaign` and `confirm_and_apply` are two
ordinary tool calls over one endpoint. Nothing at the protocol level stopped
a model making both in the same turn, in a few hundred milliseconds, so the
person could be told "done" having never seen the preview. Everything the
plan store guarantees - owner-bound, expiring, single-use, policy re-checked
- controls WHICH plan is applied, never WHETHER a person looked.

Elicitation closes it, because the server stops and waits for the client to
return an answer. No answer, no mutation. That makes human approval a
property of the server rather than a habit of the client.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult

from gads_write.ads.executor import MutationResult
from gads_write.ads.reads import CampaignSummary
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
CAMPAIGN = "55"


@dataclass(frozen=True)
class FakeCaller:
    email: str = "op@example.com"
    name: str | None = "Operator"


class FixedTier:
    def __init__(self, tier: Tier = Tier.OPERATOR) -> None:
        self.tier = tier

    async def resolve(self, caller, customer_id):
        return self.tier

    async def visible_tier(self, caller):
        return self.tier

    @property
    def source(self) -> str:
        return "fixed"


class FakeReader:
    async def campaign_by_id(self, *, customer_id, campaign_id):
        return CampaignSummary(
            campaign_id=campaign_id, name="Brand - Exact", status="ENABLED",
            channel_type="SEARCH", daily_budget_micros=100_000_000,
            budget_resource_name=f"customers/{ACCOUNT}/campaignBudgets/777",
            budget_id="777", budget_is_shared=False,
            bidding_strategy_type="MANUAL_CPC",
        )

    async def ad_group_by_id(self, **kw): return None
    async def accessible_customer_ids(self): return ()
    async def account_summary(self, customer_id): return None
    async def access_role(self, **kw): return None
    async def campaign_performance(self, **kw): return ()
    async def search_terms(self, **kw): return ()


class FakeExecutor:
    def __init__(self) -> None:
        self.applied: list = []

    async def apply(self, request):
        self.applied.append(request)
        return MutationResult(
            success=True, resource_names=("customers/x/campaigns/55",),
            details={"operation": request.operation},
        )


@dataclass
class Harness:
    mcp: FastMCP
    executor: FakeExecutor
    plans: PlanStore
    seen: list


@pytest.fixture
def harness(tmp_path, write_policy):
    def _build(*, require_confirmation: bool = True) -> Harness:
        policy_path = write_policy()
        settings = Settings(
            env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
            oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
            jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
            write_enabled=True,
            policy_path=policy_path,
            roles_path=tmp_path / "roles.yaml",
            audit_log_path=tmp_path / "audit.jsonl",
            require_human_confirmation=require_confirmation,
        )
        policy_store = PolicyStore(policy_path)
        audit_log = AuditLog(settings.audit_log_path)
        tiers = FixedTier()
        guard = Guard(
            settings=settings, policy_store=policy_store, tier_resolver=tiers,
            audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log),
        )
        plans = PlanStore()
        executor = FakeExecutor()
        reader = FakeReader()

        mcp = FastMCP(name="test")
        mcp.add_middleware(
            TierMiddleware(
                tier_resolver=tiers, settings=settings, caller_provider=FakeCaller
            )
        )
        register_write_tools(
            mcp, guard=guard, reader=reader, policy_store=policy_store,
            plan_store=plans, caller_provider=FakeCaller,
        )
        register_confirm_tool(
            mcp, guard=guard, executor=executor, plan_store=plans,
            reader=reader, settings=settings, caller_provider=FakeCaller,
        )
        return Harness(mcp=mcp, executor=executor, plans=plans, seen=[])

    return _build


def _accepting(seen: list):
    async def handler(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="accept", content=None)

    return handler


def _declining(seen: list):
    async def handler(message, response_type, params, context):
        seen.append(message)
        return ElicitResult(action="decline")

    return handler


async def _draft(h: Harness) -> str:
    async with Client(h.mcp, elicitation_handler=_accepting(h.seen)) as client:
        result = await client.call_tool(
            "pause_campaign", {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}
        )
    return json.loads(result.content[0].text)["plan_id"]


# ---------------------------------------------------------------------------

async def test_a_declined_confirmation_changes_nothing(harness) -> None:
    """The scenario: the person reads the preview and says no."""
    h = harness()
    plan_id = await _draft(h)

    async with Client(h.mcp, elicitation_handler=_declining(h.seen)) as client:
        with pytest.raises(Exception) as caught:
            await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert h.executor.applied == []
    assert "declined" in str(caught.value).lower()


async def test_an_accepted_confirmation_applies_the_change(harness) -> None:
    h = harness()
    plan_id = await _draft(h)

    async with Client(h.mcp, elicitation_handler=_accepting(h.seen)) as client:
        await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert len(h.executor.applied) == 1


async def test_the_human_is_shown_the_actual_change(harness) -> None:
    """Approving a blank prompt would be worthless. The preview must reach
    the person, with the before and after in it."""
    h = harness()
    plan_id = await _draft(h)
    h.seen.clear()

    async with Client(h.mcp, elicitation_handler=_accepting(h.seen)) as client:
        await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert h.seen, "the human was never asked"
    shown = h.seen[-1]
    assert "Brand - Exact" in shown
    assert "ENABLED -> PAUSED" in shown


async def test_a_client_that_cannot_ask_a_human_is_refused(harness) -> None:
    """The one that makes this a server guarantee.

    A client with no elicitation support cannot obtain approval, so it must
    not be able to apply anything. Failing open here would leave exactly the
    hole this feature exists to close.
    """
    h = harness()
    plan_id = await _draft(h)

    async with Client(h.mcp) as client:  # no elicitation_handler
        with pytest.raises(Exception):
            await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert h.executor.applied == []


async def test_a_declined_plan_can_still_be_confirmed_later(harness) -> None:
    """Saying no must not burn the plan - the person may want to think and
    then approve the same change."""
    h = harness()
    plan_id = await _draft(h)

    async with Client(h.mcp, elicitation_handler=_declining(h.seen)) as client:
        with pytest.raises(Exception):
            await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    async with Client(h.mcp, elicitation_handler=_accepting(h.seen)) as client:
        await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert len(h.executor.applied) == 1


async def test_confirmation_can_be_switched_off_deliberately(harness) -> None:
    """An escape hatch for a client that genuinely cannot elicit - but it is
    an explicit, deliberate setting, not a silent fallback."""
    h = harness(require_confirmation=False)
    plan_id = await _draft(h)

    async with Client(h.mcp) as client:
        await client.call_tool("confirm_and_apply", {"plan_id": plan_id})

    assert len(h.executor.applied) == 1
