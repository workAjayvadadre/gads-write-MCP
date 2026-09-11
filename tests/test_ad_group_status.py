"""pause_ad_group and enable_ad_group.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy snapshot, the registry, the middleware and the audit log.

These are the campaign status pair one level down, and they carry the same
three properties, plus one of their own.

  the status is never an argument       AdGroupStatus includes REMOVED, which
                                        is terminal. It is hardcoded per tool
                                        and per executor operation, so no
                                        input reaches it.

  the draft READS                       "ad group 449283710 -> PAUSED" is not
                                        something a human can approve.

  already in that state -> no plan      A confirmed, audited change that
                                        changed nothing makes the audit log
                                        harder to read.

  the CAMPAIGN's status is on the       An ENABLED ad group inside a PAUSED
  preview                               campaign still does not serve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import FakeManagedAccounts
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult

from gads_write.ads.executor import MutationResult
from gads_write.ads.reads import AdGroupSummary, AdsReadError
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
AD_GROUP = "66"
CAMPAIGN = "55"


@dataclass(frozen=True)
class FakeCaller:
    email: str = "op@example.com"
    name: str | None = "Operator"


class CallerBox:
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
    def __init__(self) -> None:
        self.ad_group_status = "ENABLED"
        self.campaign_status = "ENABLED"
        self.ad_group_missing = False
        self.read_fails = False

    async def ad_group_by_id(self, *, customer_id, ad_group_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        if self.ad_group_missing:
            return None
        return AdGroupSummary(
            ad_group_id=ad_group_id,
            name="Core Terms",
            status=self.ad_group_status,
            campaign_id=CAMPAIGN,
            campaign_name="Brand - Exact",
            cpc_bid_micros=50_000_000,
            bidding_strategy_type="MANUAL_CPC",
            campaign_status=self.campaign_status,
        )

    async def campaign_by_id(self, **kw): return None
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
            success=True,
            resource_names=(f"customers/{request.customer_id}/adGroups/66",),
            details={"operation": request.operation},
        )


@dataclass
class Harness:
    mcp: FastMCP
    reader: FakeReader
    executor: FakeExecutor
    tiers: MutableTier
    audit_path: Path


@pytest.fixture
def linked(tmp_path):
    def _build(tier: Tier = Tier.OPERATOR) -> Harness:
        settings = Settings(
            env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
            oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
            jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
            write_enabled=True,
            roles_path=tmp_path / "roles.yaml",
            audit_log_path=tmp_path / "audit.jsonl",
            allowed_url_domains=frozenset({"indiraivf.com"}),
        )
        policy_store = PolicyStore(settings)
        audit_log = AuditLog(settings.audit_log_path)
        tiers = MutableTier(tier)
        guard = Guard(
            settings=settings, policy_store=policy_store, tier_resolver=tiers,
            audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log),
            managed_accounts=FakeManagedAccounts(),
        )
        caller = CallerBox()
        reader = FakeReader()
        executor = FakeExecutor()
        plans = PlanStore()

        mcp = FastMCP(name="test")
        mcp.add_middleware(
            TierMiddleware(tier_resolver=tiers, settings=settings, caller_provider=caller)
        )
        register_write_tools(
            mcp, guard=guard, reader=reader, policy_store=policy_store,
            plan_store=plans, caller_provider=caller,
        )
        register_confirm_tool(
            mcp, guard=guard, executor=executor, plan_store=plans,
            settings=settings, reader=reader, caller_provider=caller,
        )
        return Harness(
            mcp=mcp, reader=reader, executor=executor, tiers=tiers,
            audit_path=settings.audit_log_path,
        )

    return _build


async def _approve(message, response_type, params, context):
    return ElicitResult(action="accept", content=None)


async def _call(mcp, tool, args) -> dict:
    async with Client(mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


ARGS = {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP}


# ===========================================================================
# the status is never an argument
# ===========================================================================

@pytest.mark.parametrize(
    ("tool", "expected"), [("pause_ad_group", "PAUSED"), ("enable_ad_group", "ENABLED")]
)
async def test_each_tool_sets_exactly_one_status(linked, tool, expected) -> None:
    h = linked()
    h.reader.ad_group_status = "PAUSED" if expected == "ENABLED" else "ENABLED"
    draft = await _call(h.mcp, tool, ARGS)
    assert draft["target_status"] == expected


async def test_neither_tool_accepts_a_status(linked) -> None:
    """The structural guarantee behind "there are no delete tools".

    AdGroupStatus has a REMOVED member. If a status could be passed in, one
    typo turns pause_ad_group into a delete tool - so the schema must not have
    the parameter at all.
    """
    h = linked()
    async with Client(h.mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    for name in ("pause_ad_group", "enable_ad_group"):
        properties = tools[name].inputSchema.get("properties", {})
        assert set(properties) == {"customer_id", "ad_group_id"}, name
        assert "status" not in properties, name


# ===========================================================================
# the draft reads, for the preview
# ===========================================================================

async def test_the_preview_names_the_ad_group_and_its_campaign(linked) -> None:
    """An id and an arrow is not something anybody can approve."""
    h = linked()
    h.reader.ad_group_status = "ENABLED"
    draft = await _call(h.mcp, "pause_ad_group", ARGS)
    preview = draft["preview"]

    assert "Core Terms" in preview
    assert "Brand - Exact" in preview
    assert "INR 50.00" in preview
    assert "ENABLED -> PAUSED" in preview
    assert h.executor.applied == []


async def test_pausing_says_it_is_reversible(linked) -> None:
    h = linked()
    draft = await _call(h.mcp, "pause_ad_group", ARGS)
    assert "Reversible with enable_ad_group" in draft["preview"]
    assert "every keyword and ad" in draft["preview"]


async def test_enabling_inside_an_enabled_campaign_warns_about_spend(linked) -> None:
    h = linked()
    h.reader.ad_group_status = "PAUSED"
    h.reader.campaign_status = "ENABLED"
    draft = await _call(h.mcp, "enable_ad_group", ARGS)
    assert "spend the CAMPAIGN's daily budget" in draft["preview"]


async def test_enabling_inside_a_paused_campaign_says_it_still_will_not_serve(
    linked,
) -> None:
    """Without this the tool reports success on a change with no visible
    effect, and the person goes looking for the fault somewhere else."""
    h = linked()
    h.reader.ad_group_status = "PAUSED"
    h.reader.campaign_status = "PAUSED"
    draft = await _call(h.mcp, "enable_ad_group", ARGS)
    preview = draft["preview"]

    assert "is PAUSED" in preview
    assert "still will not serve" in preview
    # And it does not also claim the budget is about to be spent.
    assert "spend the CAMPAIGN's daily budget" not in preview


# ===========================================================================
# nothing to do, and nothing terminal
# ===========================================================================

@pytest.mark.parametrize(
    ("tool", "status"), [("pause_ad_group", "PAUSED"), ("enable_ad_group", "ENABLED")]
)
async def test_an_ad_group_already_in_that_state_drafts_no_plan(
    linked, tool, status
) -> None:
    h = linked()
    h.reader.ad_group_status = status
    payload = await _call(h.mcp, tool, ARGS)

    assert payload["no_change_needed"] is True
    assert "plan_id" not in payload
    assert h.executor.applied == []


@pytest.mark.parametrize("tool", ["pause_ad_group", "enable_ad_group"])
async def test_a_removed_ad_group_is_refused_at_draft_time(linked, tool) -> None:
    """Removal is terminal in Google Ads. Catching it before a plan exists
    beats an opaque API rejection after a human has approved something."""
    h = linked()
    h.reader.ad_group_status = "REMOVED"
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, tool, ARGS)
    assert "REMOVED" in str(caught.value)
    assert h.executor.applied == []


async def test_a_missing_ad_group_is_refused(linked) -> None:
    h = linked()
    h.reader.ad_group_missing = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "pause_ad_group", ARGS)
    assert "No ad group" in str(caught.value)


async def test_a_failed_read_refuses_rather_than_drafting_blind(linked) -> None:
    h = linked()
    h.reader.read_fails = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "pause_ad_group", ARGS)
    assert "Nothing was changed" in str(caught.value)


# ===========================================================================
# the gate
# ===========================================================================

async def test_an_operator_can_pause_and_enable(linked) -> None:
    """Parity: a STANDARD user does this in the Google Ads UI constantly."""
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "pause_ad_group", ARGS)
    assert draft["plan_id"]


@pytest.mark.parametrize("tool", ["pause_ad_group", "enable_ad_group"])
async def test_a_readonly_user_cannot_draft_one(linked, tool) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(h.mcp, tool, ARGS)
    assert h.executor.applied == []


async def test_an_unmanaged_account_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(
            h.mcp, "pause_ad_group",
            {"customer_id": "5555555555", "ad_group_id": AD_GROUP},
        )
    assert h.executor.applied == []


async def test_a_non_numeric_ad_group_id_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(
            h.mcp, "pause_ad_group",
            {"customer_id": ACCOUNT, "ad_group_id": "66 OR 1=1"},
        )
    assert h.executor.applied == []


# ===========================================================================
# confirming
# ===========================================================================

@pytest.mark.parametrize("tool", ["pause_ad_group", "enable_ad_group"])
async def test_confirming_sends_the_right_operation(linked, tool) -> None:
    h = linked()
    h.reader.ad_group_status = "ENABLED" if tool == "pause_ad_group" else "PAUSED"
    draft = await _call(h.mcp, tool, ARGS)
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert applied["applied"] is True
    request = h.executor.applied[0]
    assert request.operation == tool
    assert request.payload == {"ad_group_id": AD_GROUP}
    # No status in the payload. The executor decides it from the operation.
    assert "status" not in request.payload


async def test_confirming_does_not_re_read_the_ad_group(linked) -> None:
    """What a status change skips is the SECOND gate pass, not the draft's
    read. There is no relative rule here - no percentage, no amount - so
    there is nothing to re-establish, and confirm pays for neither the extra
    audit line nor the round trip.
    """
    h = linked()
    draft = await _call(h.mcp, "pause_ad_group", ARGS)

    # Even a total read failure does not stop the confirm, because confirm
    # never reads for this tool.
    h.reader.read_fails = True
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_demoted_user_cannot_confirm_their_own_draft(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "pause_ad_group", ARGS)

    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


async def test_applying_writes_an_audit_line_charging_no_spend(linked) -> None:
    """Pausing or enabling an ad group moves no daily budget - the campaign's
    budget is unchanged either way."""
    h = linked()
    draft = await _call(h.mcp, "pause_ad_group", ARGS)
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [line for line in AuditLog(h.audit_path).iter_records()
               if line.get("applied") is True]
    assert len(applied) == 1
    assert applied[0]["tool"] == "pause_ad_group"
    assert applied[0].get("spend_delta_units") in (None, "")


async def test_a_plan_is_single_use(linked) -> None:
    h = linked()
    draft = await _call(h.mcp, "pause_ad_group", ARGS)
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert len(h.executor.applied) == 1


async def test_the_kill_switch_hides_both_tools(tmp_path) -> None:
    settings = Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
        write_enabled=False, roles_path=tmp_path / "r.yaml",
        audit_log_path=tmp_path / "a.jsonl",
    )
    policy_store = PolicyStore(settings)
    audit_log = AuditLog(settings.audit_log_path)
    tiers = MutableTier(Tier.LEAD)
    guard = Guard(settings=settings, policy_store=policy_store, tier_resolver=tiers,
                  audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log),
                  managed_accounts=FakeManagedAccounts())
    caller = CallerBox()
    mcp = FastMCP(name="test")
    mcp.add_middleware(
        TierMiddleware(tier_resolver=tiers, settings=settings, caller_provider=caller)
    )
    register_write_tools(mcp, guard=guard, reader=FakeReader(),
                         policy_store=policy_store, plan_store=PlanStore(),
                         caller_provider=caller)

    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    assert "pause_ad_group" not in names
    assert "enable_ad_group" not in names
