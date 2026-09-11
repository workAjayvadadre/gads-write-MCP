"""create_ad_group - the step that makes a campaign buildable end to end.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy snapshot, the registry, the middleware and the audit log.

Two properties carry most of the weight here.

  the type is not a parameter    `AdGroup.type_` is IMMUTABLE in the Google
                                 Ads API and there is no tool here to remove
                                 an ad group, so a wrong type could never be
                                 corrected through this server.

  the bid follows the CAMPAIGN   Manual CPC needs a default max CPC, exactly
                                 as the Google Ads UI demands on that screen.
                                 Automated bidding ignores the field, so
                                 accepting one would put a figure on the
                                 preview that does nothing.

The second is re-checked at confirm time, because a campaign can be moved
onto a different bidding strategy in the Google Ads UI between the two steps.
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
from gads_write.ads.reads import AdGroupSummary, AdsReadError, CampaignSummary
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
        self.bidding_strategy = "MANUAL_CPC"
        self.channel_type = "SEARCH"
        self.campaign_status = "ENABLED"
        self.campaign_missing = False
        self.read_fails = False

    async def campaign_by_id(self, *, customer_id, campaign_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        if self.campaign_missing:
            return None
        return CampaignSummary(
            campaign_id=campaign_id,
            name="Brand - Exact",
            status=self.campaign_status,
            channel_type=self.channel_type,
            daily_budget_micros=100_000_000,
            budget_resource_name=f"customers/{ACCOUNT}/campaignBudgets/777",
            budget_id="777",
            budget_reference_count=1,
            bidding_strategy_type=self.bidding_strategy,
        )

    async def ad_group_by_id(self, *, customer_id, ad_group_id):
        return AdGroupSummary(
            ad_group_id=ad_group_id,
            name="Core Terms",
            status="ENABLED",
            campaign_id=CAMPAIGN,
            campaign_name="Brand - Exact",
            cpc_bid_micros=50_000_000,
            bidding_strategy_type=self.bidding_strategy,
        )

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
            resource_names=(f"customers/{request.customer_id}/adGroups/9",),
            details={"operation": request.operation},
        )


@dataclass
class Harness:
    mcp: FastMCP
    reader: FakeReader
    executor: FakeExecutor
    tiers: MutableTier
    caller: CallerBox
    audit_path: Path


@pytest.fixture
def linked(tmp_path):
    """Draft tools and confirm sharing ONE plan store, as in production."""

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
            caller=caller, audit_path=settings.audit_log_path,
        )

    return _build


async def _approve(message, response_type, params, context):
    return ElicitResult(action="accept", content=None)


async def _call(mcp, tool, args) -> dict:
    async with Client(mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


def _audit(path: Path) -> list[dict]:
    return list(AuditLog(path).iter_records())


def _draft_args(**overrides) -> dict:
    args = {
        "customer_id": ACCOUNT,
        "campaign_id": CAMPAIGN,
        "name": "Core Terms",
        "max_cpc": 25,
    }
    args.update(overrides)
    return args


# ===========================================================================
# drafting
# ===========================================================================

async def test_a_draft_previews_every_setting_including_the_fixed_ones(linked) -> None:
    """A preview listing only the name would be lying by omission. Paused,
    SEARCH_STANDARD and the immutability of the type are decisions too, and
    the reader has no other way to know them."""
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args())
    preview = draft["preview"]

    assert draft["plan_id"]
    assert "Core Terms" in preview
    assert "Brand - Exact" in preview
    assert "SEARCH_STANDARD" in preview
    assert "IMMUTABLE" in preview
    assert "PAUSED" in preview
    assert "INR 25.00" in preview
    assert h.executor.applied == []          # drafting creates nothing


async def test_a_new_ad_group_is_created_paused(linked) -> None:
    """rules.new_entities_start_paused. It cannot spend before a human looks
    at it, which is the point of creating things paused at all."""
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args())
    assert draft["created_status"] == "PAUSED"
    assert "cannot spend" in draft["preview"]


async def test_an_operator_can_create_an_ad_group(linked) -> None:
    """Parity: a STANDARD user creates ad groups in the Google Ads UI every
    day, so requiring Admin here would be a restriction the UI does not have."""
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "create_ad_group", _draft_args())
    assert draft["plan_id"]


async def test_a_readonly_user_cannot_draft_one(linked) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(h.mcp, "create_ad_group", _draft_args())
    assert h.executor.applied == []


# ===========================================================================
# the bid follows the campaign's bidding strategy
# ===========================================================================

async def test_manual_cpc_requires_a_default_bid(linked) -> None:
    """The Google Ads UI demands one on this screen too, so this is parity
    rather than an extra rule. Without it the ad group has nothing to bid."""
    h = linked()
    h.reader.bidding_strategy = "MANUAL_CPC"
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=None))
    assert "max_cpc" in str(caught.value)
    assert h.executor.applied == []


async def test_enhanced_cpc_also_requires_a_default_bid(linked) -> None:
    """The legacy manual strategy: Google adjusts the bid, but the ad group's
    own max CPC is still the base it adjusts."""
    h = linked()
    h.reader.bidding_strategy = "ENHANCED_CPC"
    with pytest.raises(Exception):
        await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=None))


async def test_automated_bidding_refuses_a_bid(linked) -> None:
    """Google ignores cpc_bid_micros under an automated strategy, so accepting
    one would put a number on the preview that does nothing - and a preview
    that lies is the one thing this server refuses on principle."""
    h = linked()
    h.reader.bidding_strategy = "MAXIMIZE_CLICKS"
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=25))
    assert "ignores" in str(caught.value)
    assert h.executor.applied == []


async def test_automated_bidding_drafts_without_a_bid(linked) -> None:
    h = linked()
    h.reader.bidding_strategy = "MAXIMIZE_CLICKS"
    draft = await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=None))
    assert draft["plan_id"]
    assert "MAXIMIZE_CLICKS sets the bids for you" in draft["preview"]


async def test_an_unreadable_bidding_strategy_is_refused_not_guessed(linked) -> None:
    h = linked()
    h.reader.bidding_strategy = ""
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args())
    assert "rather than guessing" in str(caught.value)


@pytest.mark.parametrize("bad", [0, -5])
async def test_a_bid_that_is_not_a_usable_positive_amount_is_refused(linked, bad) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=bad))
    assert h.executor.applied == []


# ===========================================================================
# the campaign has to be able to hold a SEARCH_STANDARD ad group
# ===========================================================================

@pytest.mark.parametrize("channel", ["DISPLAY", "VIDEO", "SHOPPING", "PERFORMANCE_MAX"])
async def test_a_non_search_campaign_is_refused(linked, channel) -> None:
    """`type_` is immutable and there is no remove tool, so a SEARCH_STANDARD
    ad group in a Display campaign could never be corrected here."""
    h = linked()
    h.reader.channel_type = channel
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args())
    assert "immutable" in str(caught.value).lower()
    assert h.executor.applied == []


async def test_a_removed_campaign_is_refused_at_draft_time(linked) -> None:
    """Removal is terminal in Google Ads, so an ad group added to one could
    never serve. Catching it before a plan exists beats an opaque API
    rejection after a human has approved something."""
    h = linked()
    h.reader.campaign_status = "REMOVED"
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args())
    assert "REMOVED" in str(caught.value)


async def test_a_missing_campaign_is_refused(linked) -> None:
    h = linked()
    h.reader.campaign_missing = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "create_ad_group", _draft_args())
    assert "No campaign" in str(caught.value)


async def test_an_unmanaged_account_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "create_ad_group", _draft_args(customer_id="5555555555"))
    assert h.executor.applied == []


# ===========================================================================
# names
# ===========================================================================

@pytest.mark.parametrize("bad_name", ["", "   ", "a" * 256, "Core" + chr(0) + "Terms"])
async def test_an_unusable_name_is_refused(linked, bad_name) -> None:
    """Empty, over Google's limit, or carrying a control character - which
    Google accepts and then renders as nothing, producing an ad group nobody
    can find by name."""
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "create_ad_group", _draft_args(name=bad_name))
    assert h.executor.applied == []


async def test_a_name_exactly_at_the_limit_is_accepted(linked) -> None:
    """The control for the test above: 255 goes through, 256 does not."""
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args(name="a" * 255))
    assert draft["plan_id"]


# ===========================================================================
# confirming
# ===========================================================================

async def test_confirming_sends_micros_not_units(linked) -> None:
    """The 1,000,000x rule. 25 rupees must reach Google as 25000000."""
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=25))
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    request = h.executor.applied[0]
    assert request.operation == "create_ad_group"
    assert request.payload["cpc_bid_micros"] == 25_000_000


async def test_confirming_carries_the_whole_payload(linked) -> None:
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args())
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    payload = h.executor.applied[0].payload
    assert payload["campaign_id"] == CAMPAIGN
    assert payload["name"] == "Core Terms"
    assert payload["status"] == "PAUSED"


async def test_an_automated_bidding_ad_group_carries_no_bid(linked) -> None:
    """Omitted, not zero. On a create there is no update mask, so an absent
    field is simply not written - whereas an explicit zero is a real value
    Google would store, and a zero bid is an ad group that cannot win."""
    h = linked()
    h.reader.bidding_strategy = "MAXIMIZE_CLICKS"
    draft = await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=None))
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "cpc_bid_micros" not in h.executor.applied[0].payload


async def test_a_campaign_moved_to_automated_bidding_is_refused_at_confirm(
    linked,
) -> None:
    """The reason this tool pays for a second gate pass and a re-read.

    The human approved 'default max CPC INR 25.00'. If the campaign is moved
    onto Maximize Clicks in the Google Ads UI first, that bid becomes a number
    Google ignores - so the change that would be applied is not the one that
    was previewed.
    """
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=25))

    h.reader.bidding_strategy = "MAXIMIZE_CLICKS"   # changed in the Ads UI

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "ignores" in str(caught.value)
    assert h.executor.applied == []


async def test_a_campaign_moved_to_manual_bidding_is_refused_at_confirm(
    linked,
) -> None:
    """The other direction. An ad group drafted with no bid against an
    automated campaign has nothing to bid with once that campaign is manual."""
    h = linked()
    h.reader.bidding_strategy = "MAXIMIZE_CLICKS"
    draft = await _call(h.mcp, "create_ad_group", _draft_args(max_cpc=None))

    h.reader.bidding_strategy = "MANUAL_CPC"

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "max_cpc" in str(caught.value)
    assert h.executor.applied == []


async def test_a_read_failure_at_confirm_refuses_without_burning_the_plan(
    linked,
) -> None:
    """We could not establish what we would be creating it in. That is a
    reason to stop, not a reason to consume the plan."""
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args())

    h.reader.read_fails = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "Nothing was changed" in str(caught.value)
    assert h.executor.applied == []

    h.reader.read_fails = False
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_demoted_user_cannot_confirm_their_own_draft(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "create_ad_group", _draft_args())

    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


async def test_applying_writes_an_audit_line(linked) -> None:
    h = linked()
    draft = await _call(h.mcp, "create_ad_group", _draft_args())
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [line for line in _audit(h.audit_path) if line.get("applied") is True]
    assert len(applied) == 1
    assert applied[0]["tool"] == "create_ad_group"
    # Creating an ad group moves no daily budget, so it charges nothing
    # against the running total the budget preview reports.
    assert applied[0].get("spend_delta_units") in (None, "")


async def test_the_kill_switch_hides_the_tool(tmp_path) -> None:
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
        names = {t.name for t in await client.list_tools()}

    assert "create_ad_group" not in names
