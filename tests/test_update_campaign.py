"""update_campaign - name and run dates, and nothing else.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy snapshot, the registry, the middleware and the audit log.

The API detail this phase turns on, and the reason it is worth its own file:
Google Ads API v25 has NO `campaign.start_date` or `campaign.end_date`. The
fields are `start_date_time` and `end_date_time`, strings in the CUSTOMER'S
timezone in "yyyy-MM-dd HH:mm:ss" form. Writing the fields that "obviously"
exist would have produced code that type-checks, reads correctly, and is
rejected by Google at the only moment that matters.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from conftest import FakeManagedAccounts
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
CAMPAIGN = "55"

BASE_CAMPAIGN = CampaignSummary(
    campaign_id=CAMPAIGN,
    name="Brand - Exact",
    status="ENABLED",
    channel_type="SEARCH",
    daily_budget_micros=100_000_000,
    budget_resource_name=f"customers/{ACCOUNT}/campaignBudgets/777",
    budget_id="777",
    budget_reference_count=1,
    bidding_strategy_type="MANUAL_CPC",
    positive_geo_target_type="PRESENCE",
    start_date_time="2026-01-01 00:00:00",
    end_date_time="",
)


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
        self.campaign: CampaignSummary | None = BASE_CAMPAIGN
        self.read_fails = False

    async def campaign_by_id(self, *, customer_id, campaign_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return self.campaign

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
            success=True,
            resource_names=(f"customers/{request.customer_id}/campaigns/55",),
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


BASE_ARGS = {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN}


# ===========================================================================
# the v25 field names
# ===========================================================================

async def test_dates_are_sent_as_date_TIME_strings(linked) -> None:
    """v25 has no start_date / end_date. The proto documents the time
    components this uses: 00:00:00 to begin a day, 23:59:59 to end one."""
    h = linked()
    draft = await _call(
        h.mcp, "update_campaign",
        {**BASE_ARGS, "start_date": "2026-03-01", "end_date": "2026-12-31"},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    payload = h.executor.applied[0].payload
    assert payload["start_date_time"] == "2026-03-01 00:00:00"
    assert payload["end_date_time"] == "2026-12-31 23:59:59"
    assert "start_date" not in payload
    assert "end_date" not in payload


# ===========================================================================
# only what was given is changed
# ===========================================================================

async def test_renaming_carries_no_dates(linked) -> None:
    """An omitted field must not reach the payload at all. The mask is derived
    from set fields, and a mask naming an unset field BLANKS it - which is how
    a rename could silently erase a campaign's end date."""
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    payload = h.executor.applied[0].payload
    assert payload["name"] == "Brand - New"
    assert "start_date_time" not in payload
    assert "end_date_time" not in payload


async def test_setting_an_end_date_carries_no_name(linked) -> None:
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": "2026-12-31"})
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    payload = h.executor.applied[0].payload
    assert "name" not in payload
    assert payload["end_date_time"] == "2026-12-31 23:59:59"


async def test_changing_nothing_is_refused(linked) -> None:
    """A mutation that changes nothing would still produce an audit line for a
    change nobody made - the same reason the status tools return "nothing to
    do" instead of drafting a plan."""
    h = linked()
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "update_campaign", BASE_ARGS)
    assert "at least one" in str(caught.value)
    assert h.executor.applied == []


# ===========================================================================
# the preview
# ===========================================================================

async def test_the_preview_shows_each_change_with_its_previous_value(linked) -> None:
    h = linked()
    draft = await _call(
        h.mcp, "update_campaign",
        {**BASE_ARGS, "name": "Brand - New", "end_date": "2026-12-31"},
    )
    preview = draft["preview"]

    assert "'Brand - Exact' -> 'Brand - New'" in preview
    assert "2026-12-31" in preview
    # An absent end date must read as something, not as blank.
    assert "none (runs indefinitely)" in preview


async def test_the_preview_names_the_timezone_the_dates_mean(linked) -> None:
    """Google keeps these in the ACCOUNT's timezone, not the reader's. A date
    with no timezone attached is a date that is wrong for part of every day."""
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": "2026-12-31"})
    assert "Asia/Kolkata" in draft["preview"]


async def test_the_preview_says_what_it_does_not_touch(linked) -> None:
    """Status, budget, bidding and locations each have their own tool with
    their own preview."""
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    assert "NOT changed" in draft["preview"]


# ===========================================================================
# dates that could not work
# ===========================================================================

async def test_an_end_before_the_given_start_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_campaign",
            {**BASE_ARGS, "start_date": "2026-06-01", "end_date": "2026-03-01"},
        )
    assert "before the start date" in str(caught.value)
    assert h.executor.applied == []


async def test_an_end_before_the_campaigns_CURRENT_start_is_refused(linked) -> None:
    """Setting only an end date is ordinary, and whether it works depends on a
    value only the account holds - which is why this check needs the campaign
    rather than just the arguments."""
    h = linked()
    h.reader.campaign = replace(BASE_CAMPAIGN, start_date_time="2026-06-01 00:00:00")

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": "2026-03-01"})
    assert "before the start date" in str(caught.value)
    assert h.executor.applied == []


@pytest.mark.parametrize(
    "bad", ["2026-13-01", "2026-02-31", "01-01-2026", "2026/01/01", "next tuesday"]
)
async def test_an_unusable_date_is_refused(linked, bad) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": bad})
    assert h.executor.applied == []


async def test_an_unusable_name_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "a" * 256})
    assert h.executor.applied == []


async def test_a_removed_campaign_is_refused(linked) -> None:
    h = linked()
    h.reader.campaign = replace(BASE_CAMPAIGN, status="REMOVED")
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    assert "REMOVED" in str(caught.value)


async def test_a_missing_campaign_is_refused(linked) -> None:
    h = linked()
    h.reader.campaign = None
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    assert "No campaign" in str(caught.value)


# ===========================================================================
# the gate, and confirming
# ===========================================================================

async def test_an_operator_can_rename_a_campaign(linked) -> None:
    """Parity: a STANDARD user renames campaigns in the Google Ads UI."""
    h = linked(Tier.OPERATOR)
    assert (await _call(
        h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"}
    ))["plan_id"]


async def test_a_readonly_user_cannot(linked) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    assert h.executor.applied == []


async def test_an_unmanaged_account_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(
            h.mcp, "update_campaign",
            {"customer_id": "5555555555", "campaign_id": CAMPAIGN, "name": "X"},
        )
    assert h.executor.applied == []


async def test_a_start_moved_after_drafting_refuses_the_plan(linked) -> None:
    """The reason this tool re-reads at confirm. The human approved an end
    date that sat after the campaign's start; if the start is moved past it in
    the Google Ads UI first, the approved plan would create a campaign that
    can never run."""
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": "2026-03-01"})

    h.reader.campaign = replace(BASE_CAMPAIGN, start_date_time="2026-06-01 00:00:00")

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "before the start date" in str(caught.value)
    assert h.executor.applied == []


async def test_a_read_failure_at_confirm_refuses_without_burning_the_plan(
    linked,
) -> None:
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})

    h.reader.read_fails = True
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []

    h.reader.read_fails = False
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_demoted_user_cannot_confirm_their_own_draft(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})

    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


async def test_applying_charges_nothing_against_the_daily_total(linked) -> None:
    """A name moves no money, and a date change alters when a budget is spent
    rather than how much per day."""
    h = linked()
    draft = await _call(h.mcp, "update_campaign", {**BASE_ARGS, "name": "Brand - New"})
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [line for line in AuditLog(h.audit_path).iter_records()
               if line.get("applied") is True]
    assert len(applied) == 1
    assert applied[0]["tool"] == "update_campaign"
    assert applied[0].get("spend_delta_units") in (None, "")


async def test_the_tool_cannot_clear_an_end_date(linked) -> None:
    """A real limitation, recorded rather than hidden.

    Clearing a field means naming it in an update mask while leaving it unset -
    which is exactly the mechanism `_seal_mask` exists to make impossible,
    because it is how a status change silently erases a campaign name. So
    "run indefinitely" is not expressible here, and an empty string is treated
    as "not supplied" rather than as "clear it".
    """
    h = linked()
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "update_campaign", {**BASE_ARGS, "end_date": ""})
    assert "at least one" in str(caught.value)
    assert h.executor.applied == []
