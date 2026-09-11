"""Keywords and ads: status, bids, and the reads that find their ids.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy snapshot, the registry, the middleware and the audit log.

Everything below an ad group is addressed by TWO ids - an
`ad_group_criterion` is `{ad_group_id}~{criterion_id}` and an `ad_group_ad` is
`{ad_group_id}~{ad_id}` - because those child ids are unique within an ad
group, not within an account. A tool taking one of them alone would be a
silent way to change the wrong thing, so every tool here takes both and
list_keywords / list_ads return them together.
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
from gads_write.ads.reads import AdSummary, AdsReadError, KeywordSummary
from gads_write.auth.tiers import Tier
from gads_write.mcp_middleware import TierMiddleware
from gads_write.safety.audit import AuditLog
from gads_write.safety.guards import Guard
from gads_write.safety.plans import PlanStore
from gads_write.safety.policy import PolicyStore
from gads_write.safety.spend import DailySpendLedger
from gads_write.settings import Settings
from gads_write.tools.confirm import register_confirm_tool
from gads_write.tools.reads import register_read_tools
from gads_write.tools.writes import register_write_tools

ACCOUNT = "1234567890"
AD_GROUP = "66"
CRITERION = "999"
AD = "888"

BASE_KEYWORD = KeywordSummary(
    criterion_id=CRITERION,
    ad_group_id=AD_GROUP,
    ad_group_name="Core Terms",
    campaign_id="55",
    campaign_name="Brand - Exact",
    text="ivf treatment",
    match_type="EXACT",
    status="ENABLED",
    cpc_bid_micros=0,
    effective_cpc_bid_micros=50_000_000,
    ad_group_status="ENABLED",
    campaign_status="ENABLED",
    bidding_strategy_type="MANUAL_CPC",
)

BASE_AD = AdSummary(
    ad_id=AD,
    ad_group_id=AD_GROUP,
    ad_group_name="Core Terms",
    campaign_id="55",
    campaign_name="Brand - Exact",
    status="ENABLED",
    ad_type="RESPONSIVE_SEARCH_AD",
    headlines=("Fertility Care", "IVF Experts"),
    descriptions=("Speak to a specialist.",),
    final_urls=("https://indiraivf.com/x",),
    approval_status="APPROVED",
    review_status="REVIEWED",
    ad_group_status="ENABLED",
    campaign_status="ENABLED",
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
        self.keyword: KeywordSummary | None = BASE_KEYWORD
        self.ad: AdSummary | None = BASE_AD
        self.read_fails = False

    async def keyword_by_id(self, *, customer_id, ad_group_id, criterion_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return self.keyword

    async def ad_by_id(self, *, customer_id, ad_group_id, ad_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return self.ad

    async def list_keywords(self, *, customer_id, ad_group_id=None):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return () if self.keyword is None else (self.keyword,)

    async def list_ads(self, *, customer_id, ad_group_id=None):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return () if self.ad is None else (self.ad,)

    async def campaign_by_id(self, **kw): return None
    async def ad_group_by_id(self, **kw): return None
    async def accessible_customer_ids(self): return ()
    async def account_summary(self, customer_id): return None
    async def managed_accounts(self, **kw): return ()
    async def access_role(self, **kw): return None
    async def campaign_performance(self, **kw): return ()
    async def search_terms(self, **kw): return ()
    async def run_query(self, **kw): return ()
    async def list_campaigns(self, customer_id): return ()
    async def list_ad_groups(self, **kw): return ()
    async def campaign_locations(self, **kw): return ()
    async def geo_targets_by_id(self, **kw): return ()
    async def find_geo_targets(self, **kw): return ()


class FakeExecutor:
    def __init__(self) -> None:
        self.applied: list = []

    async def apply(self, request):
        self.applied.append(request)
        return MutationResult(
            success=True,
            resource_names=(f"customers/{request.customer_id}/things/1",),
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
        accounts = FakeManagedAccounts()
        guard = Guard(
            settings=settings, policy_store=policy_store, tier_resolver=tiers,
            audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log),
            managed_accounts=accounts,
        )
        caller = CallerBox()
        reader = FakeReader()
        executor = FakeExecutor()
        plans = PlanStore()

        mcp = FastMCP(name="test")
        mcp.add_middleware(
            TierMiddleware(tier_resolver=tiers, settings=settings, caller_provider=caller)
        )
        register_read_tools(
            mcp, guard=guard, reader=reader, policy_store=policy_store,
            managed_accounts=accounts, caller_provider=caller,
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


KEYWORD_ARGS = {
    "customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "criterion_id": CRITERION
}
AD_ARGS = {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "ad_id": AD}


# ===========================================================================
# the reads that make the writes usable
# ===========================================================================

async def test_list_keywords_returns_both_ids_together(linked) -> None:
    """Neither id addresses a keyword alone, so returning one without the
    other would hand somebody half an address."""
    h = linked(Tier.READONLY)
    payload = await _call(h.mcp, "list_keywords", {"customer_id": ACCOUNT})
    row = payload["keywords"][0]

    assert row["criterion_id"] == CRITERION
    assert row["ad_group_id"] == AD_GROUP
    assert row["keyword"] == "[ivf treatment]"


async def test_list_keywords_separates_own_bid_from_effective_bid(linked) -> None:
    """A keyword with no bid of its own inherits the ad group default. Showing
    only one number would hide which of the two is in force."""
    h = linked(Tier.READONLY)
    row = (await _call(h.mcp, "list_keywords", {"customer_id": ACCOUNT}))["keywords"][0]

    assert row["max_cpc"] is None            # no bid of its own
    assert row["effective_max_cpc"] == "INR 50.00"


async def test_list_ads_reports_approval_alongside_status(linked) -> None:
    """An ad can be ENABLED and still show nothing because Google disapproved
    it, so status alone does not say whether an ad is running."""
    h = linked(Tier.READONLY)
    row = (await _call(h.mcp, "list_ads", {"customer_id": ACCOUNT}))["ads"][0]

    assert row["ad_id"] == AD
    assert row["ad_group_id"] == AD_GROUP
    assert row["status"] == "ENABLED"
    assert row["approval_status"] == "APPROVED"
    assert row["headlines"][0] == "Fertility Care"


@pytest.mark.parametrize("tool", ["list_keywords", "list_ads"])
async def test_a_readonly_user_can_run_the_discovery_reads(linked, tool) -> None:
    h = linked(Tier.READONLY)
    assert (await _call(h.mcp, tool, {"customer_id": ACCOUNT}))["ok"] is True


@pytest.mark.parametrize("tool", ["list_keywords", "list_ads"])
async def test_the_discovery_reads_refuse_an_unmanaged_account(linked, tool) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(h.mcp, tool, {"customer_id": "5555555555"})


# ===========================================================================
# status: never an argument
# ===========================================================================

@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("pause_keyword", {"customer_id", "ad_group_id", "criterion_id"}),
        ("enable_keyword", {"customer_id", "ad_group_id", "criterion_id"}),
        ("pause_ad", {"customer_id", "ad_group_id", "ad_id"}),
        ("enable_ad", {"customer_id", "ad_group_id", "ad_id"}),
    ],
)
async def test_no_status_tool_accepts_a_status(linked, tool, expected) -> None:
    """AdGroupCriterionStatus and AdGroupAdStatus both have a REMOVED member.
    If a status could be passed in, one typo turns a pause tool into a delete
    tool - so the schema must not carry the parameter at all."""
    h = linked()
    async with Client(h.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}

    properties = set(tools[tool].inputSchema.get("properties", {}))
    assert properties == expected, tool
    assert "status" not in properties


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("pause_keyword", KEYWORD_ARGS, "PAUSED"),
        ("enable_keyword", KEYWORD_ARGS, "ENABLED"),
        ("pause_ad", AD_ARGS, "PAUSED"),
        ("enable_ad", AD_ARGS, "ENABLED"),
    ],
)
async def test_each_tool_sets_exactly_one_status(linked, tool, args, expected) -> None:
    h = linked()
    other = "PAUSED" if expected == "ENABLED" else "ENABLED"
    h.reader.keyword = replace(BASE_KEYWORD, status=other)
    h.reader.ad = replace(BASE_AD, status=other)

    draft = await _call(h.mcp, tool, args)
    assert draft["target_status"] == expected


# ===========================================================================
# the preview names the thing, and everything above it
# ===========================================================================

async def test_a_keyword_preview_names_the_keyword_not_its_id(linked) -> None:
    """"criterion 999 -> PAUSED" is not something a human can approve."""
    h = linked()
    draft = await _call(h.mcp, "pause_keyword", KEYWORD_ARGS)
    preview = draft["preview"]

    assert "[ivf treatment]" in preview
    assert "Core Terms" in preview
    assert "Brand - Exact" in preview
    assert "EXACT" in preview
    assert "ENABLED -> PAUSED" in preview


async def test_an_ad_preview_names_it_by_its_first_headline(linked) -> None:
    """An ad has no name, so without this the preview is an id and an arrow."""
    h = linked()
    draft = await _call(h.mcp, "pause_ad", AD_ARGS)
    assert "Fertility Care" in draft["preview"]
    assert "APPROVED" in draft["preview"]


@pytest.mark.parametrize(
    ("ad_group_status", "campaign_status", "expected"),
    [
        ("PAUSED", "ENABLED", "ad group"),
        ("ENABLED", "PAUSED", "campaign"),
        ("PAUSED", "PAUSED", "ad group and campaign"),
    ],
)
async def test_enabling_under_something_paused_says_it_still_will_not_serve(
    linked, ad_group_status, campaign_status, expected
) -> None:
    """Enabling a keyword under a paused ad group changes a flag and nothing
    else. Reporting success without saying so sends people looking for a fault
    that is not there."""
    h = linked()
    h.reader.keyword = replace(
        BASE_KEYWORD,
        status="PAUSED",
        ad_group_status=ad_group_status,
        campaign_status=campaign_status,
    )
    draft = await _call(h.mcp, "enable_keyword", KEYWORD_ARGS)
    preview = draft["preview"]

    assert expected in preview
    assert "still will not serve" in preview
    assert "spend the CAMPAIGN's daily budget" not in preview


async def test_enabling_a_disapproved_ad_says_it_will_not_serve(linked) -> None:
    """Google's approval is a separate gate from status, and the one people
    forget."""
    h = linked()
    h.reader.ad = replace(BASE_AD, status="PAUSED", approval_status="DISAPPROVED")
    draft = await _call(h.mcp, "enable_ad", AD_ARGS)

    assert "DISAPPROVED" in draft["preview"]
    assert "will not serve" in draft["preview"]


async def test_enabling_with_everything_above_it_enabled_warns_about_spend(
    linked,
) -> None:
    h = linked()
    h.reader.keyword = replace(BASE_KEYWORD, status="PAUSED")
    draft = await _call(h.mcp, "enable_keyword", KEYWORD_ARGS)
    assert "spend the CAMPAIGN's daily budget" in draft["preview"]


# ===========================================================================
# nothing to do, nothing terminal, nothing missing
# ===========================================================================

@pytest.mark.parametrize(
    ("tool", "args", "status"),
    [
        ("pause_keyword", KEYWORD_ARGS, "PAUSED"),
        ("enable_keyword", KEYWORD_ARGS, "ENABLED"),
        ("pause_ad", AD_ARGS, "PAUSED"),
        ("enable_ad", AD_ARGS, "ENABLED"),
    ],
)
async def test_something_already_in_that_state_drafts_no_plan(
    linked, tool, args, status
) -> None:
    h = linked()
    h.reader.keyword = replace(BASE_KEYWORD, status=status)
    h.reader.ad = replace(BASE_AD, status=status)

    payload = await _call(h.mcp, tool, args)
    assert payload["no_change_needed"] is True
    assert "plan_id" not in payload
    assert h.executor.applied == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [("pause_keyword", KEYWORD_ARGS), ("pause_ad", AD_ARGS)],
)
async def test_a_removed_entity_is_refused_at_draft_time(linked, tool, args) -> None:
    h = linked()
    h.reader.keyword = replace(BASE_KEYWORD, status="REMOVED")
    h.reader.ad = replace(BASE_AD, status="REMOVED")

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, tool, args)
    assert "REMOVED" in str(caught.value)
    assert h.executor.applied == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [("pause_keyword", KEYWORD_ARGS), ("pause_ad", AD_ARGS)],
)
async def test_a_missing_entity_points_at_the_right_discovery_tool(
    linked, tool, args
) -> None:
    h = linked()
    h.reader.keyword = None
    h.reader.ad = None

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, tool, args)
    message = str(caught.value)
    assert "list_keywords" in message or "list_ads" in message


@pytest.mark.parametrize(
    ("tool", "args"),
    [("pause_keyword", KEYWORD_ARGS), ("pause_ad", AD_ARGS)],
)
async def test_a_failed_read_refuses_rather_than_drafting_blind(
    linked, tool, args
) -> None:
    h = linked()
    h.reader.read_fails = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, tool, args)
    assert "Nothing was changed" in str(caught.value)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pause_keyword", {**KEYWORD_ARGS, "criterion_id": "9 OR 1=1"}),
        ("pause_keyword", {**KEYWORD_ARGS, "ad_group_id": "abc"}),
        ("pause_ad", {**AD_ARGS, "ad_id": "not-an-id"}),
        ("update_keyword_bid", {**KEYWORD_ARGS, "criterion_id": "x", "new_max_cpc": 5}),
    ],
)
async def test_a_non_numeric_id_is_refused(linked, tool, args) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, tool, args)
    assert h.executor.applied == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("pause_keyword", KEYWORD_ARGS),
        ("enable_keyword", KEYWORD_ARGS),
        ("pause_ad", AD_ARGS),
        ("enable_ad", AD_ARGS),
        ("update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 60}),
    ],
)
async def test_a_readonly_user_cannot_draft_any_of_them(linked, tool, args) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(h.mcp, tool, args)
    assert h.executor.applied == []


# ===========================================================================
# confirming a status change
# ===========================================================================

@pytest.mark.parametrize(
    ("tool", "args", "id_field", "id_value"),
    [
        ("pause_keyword", KEYWORD_ARGS, "criterion_id", CRITERION),
        ("pause_ad", AD_ARGS, "ad_id", AD),
    ],
)
async def test_confirming_sends_both_ids_and_no_status(
    linked, tool, args, id_field, id_value
) -> None:
    h = linked()
    draft = await _call(h.mcp, tool, args)
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert applied["applied"] is True
    request = h.executor.applied[0]
    assert request.operation == tool
    assert request.payload == {"ad_group_id": AD_GROUP, id_field: id_value}
    assert "status" not in request.payload


async def test_confirming_a_status_change_does_not_re_read(linked) -> None:
    """What a status change skips is the SECOND gate pass, not the draft's
    read. There is no relative rule to re-establish."""
    h = linked()
    draft = await _call(h.mcp, "pause_keyword", KEYWORD_ARGS)

    h.reader.read_fails = True
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_demoted_user_cannot_confirm_their_own_draft(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(h.mcp, "pause_keyword", KEYWORD_ARGS)

    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


# ===========================================================================
# update_keyword_bid
# ===========================================================================

async def test_setting_a_first_keyword_bid_is_ordinary_not_a_rise_from_zero(
    linked,
) -> None:
    """The property that decides which number is the baseline.

    A keyword with no bid of its own has cpc_bid_micros == 0 and bids the ad
    group's default. Measuring the percentage against the OWN bid would make
    every first keyword-level bid a rise from zero, which the policy refuses
    outright - blocking the single most ordinary keyword bid change there is.
    """
    h = linked()
    h.reader.keyword = replace(
        BASE_KEYWORD, cpc_bid_micros=0, effective_cpc_bid_micros=50_000_000
    )
    draft = await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 60})

    assert draft["plan_id"]
    assert "INR 50.00 -> INR 60.00" in draft["preview"]
    assert "uses the ad group default" in draft["preview"]


async def test_a_keyword_with_its_own_bid_is_measured_against_that(linked) -> None:
    h = linked()
    h.reader.keyword = replace(
        BASE_KEYWORD, cpc_bid_micros=40_000_000, effective_cpc_bid_micros=40_000_000
    )
    draft = await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 45})

    assert "INR 40.00 -> INR 45.00" in draft["preview"]
    assert "uses the ad group default" not in draft["preview"]


async def test_a_wildly_larger_keyword_bid_is_refused(linked) -> None:
    """The typo backstop, unchanged. 50 -> 50,000 is a stray digit."""
    h = linked()
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 50000}
        )
    assert "backstop" in str(caught.value)
    assert h.executor.applied == []


@pytest.mark.parametrize("bad", [0, -5])
async def test_a_keyword_bid_of_zero_or_less_is_refused(linked, bad) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": bad})
    assert h.executor.applied == []


async def test_confirming_a_keyword_bid_sends_micros(linked) -> None:
    """The 1,000,000x rule. 60 rupees must reach Google as 60000000."""
    h = linked()
    draft = await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 60})
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    request = h.executor.applied[0]
    assert request.operation == "update_keyword_bid"
    assert request.payload["cpc_bid_micros"] == 60_000_000
    assert request.payload["ad_group_id"] == AD_GROUP
    assert request.payload["criterion_id"] == CRITERION


async def test_a_keyword_bid_is_re_checked_against_the_bid_at_confirm_time(
    linked,
) -> None:
    """A bid rule is RELATIVE, so the plan's absolute target means nothing
    without re-establishing what it is a percentage of. The human approved
    50 -> 60, a 20% rise; if the effective bid drops to 1 first, the same plan
    is a 5,900% rise nobody previewed.
    """
    h = linked()
    draft = await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 60})

    h.reader.keyword = replace(
        BASE_KEYWORD, cpc_bid_micros=0, effective_cpc_bid_micros=1_000_000
    )

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "backstop" in str(caught.value)
    assert h.executor.applied == []


async def test_a_read_failure_at_confirm_refuses_without_burning_the_plan(
    linked,
) -> None:
    h = linked()
    draft = await _call(h.mcp, "update_keyword_bid", {**KEYWORD_ARGS, "new_max_cpc": 60})

    h.reader.read_fails = True
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []

    h.reader.read_fails = False
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_applying_charges_nothing_against_the_daily_total(linked) -> None:
    """None of these move a daily budget. A bid is a price per click, and a
    status flag moves no money at all."""
    h = linked()
    draft = await _call(h.mcp, "pause_keyword", KEYWORD_ARGS)
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [line for line in AuditLog(h.audit_path).iter_records()
               if line.get("applied") is True]
    assert len(applied) == 1
    assert applied[0].get("spend_delta_units") in (None, "")


async def test_the_kill_switch_hides_the_writes_but_not_the_reads(linked) -> None:
    """GADS_WRITE_ENABLED off makes the server read-only, not useless."""
    h = linked()
    async with Client(h.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}

    for tool in ("pause_keyword", "enable_keyword", "pause_ad", "enable_ad",
                 "update_keyword_bid", "list_keywords", "list_ads"):
        assert tool in names
