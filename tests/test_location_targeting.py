"""find_locations and add_location_target.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy snapshot, the registry, the middleware and the audit log.

Three properties carry the weight here, and all three are about the preview
telling the truth rather than about limiting spend.

  ambiguity is never resolved silently   "Delhi" is a city, a state AND a
                                         union territory. The write tool takes
                                         ids, so it cannot pick one.

  presence, not presence-or-interest     Google's default serves ads to anyone
                                         in the world searching ABOUT a
                                         targeted place, which would make
                                         "targeting India" untrue.

  every location is named on the preview A preview saying "3 locations" asks
                                         someone to approve a number rather
                                         than a decision.
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
from gads_write.ads.reads import (
    AdsReadError,
    CampaignLocationRow,
    CampaignSummary,
    GeoTargetRow,
)
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
CAMPAIGN = "55"

# The real thing, and the reason this tool takes ids. Both are called "Delhi"
# in Google's data; only the canonical name and target type tell them apart.
DELHI_STATE = GeoTargetRow(
    geo_target_id="1007751",
    resource_name="geoTargetConstants/1007751",
    name="Delhi",
    canonical_name="Delhi,India",
    country_code="IN",
    target_type="Region",
    status="ENABLED",
)
NEW_DELHI_CITY = GeoTargetRow(
    geo_target_id="9040379",
    resource_name="geoTargetConstants/9040379",
    name="New Delhi",
    canonical_name="New Delhi,Delhi,India",
    country_code="IN",
    target_type="City",
    status="ENABLED",
)
INDIA = GeoTargetRow(
    geo_target_id="2356",
    resource_name="geoTargetConstants/2356",
    name="India",
    canonical_name="India",
    country_code="IN",
    target_type="Country",
    status="ENABLED",
)
CATALOGUE = {row.geo_target_id: row for row in (DELHI_STATE, NEW_DELHI_CITY, INDIA)}


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
        self.positive_geo_target_type = "PRESENCE_OR_INTEREST"
        self.campaign_status = "ENABLED"
        self.campaign_missing = False
        self.read_fails = False
        self.locations_fail = False
        # What the campaign already targets or excludes.
        self.existing: list[CampaignLocationRow] = []

    async def campaign_by_id(self, *, customer_id, campaign_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        if self.campaign_missing:
            return None
        return CampaignSummary(
            campaign_id=campaign_id,
            name="Brand - Exact",
            status=self.campaign_status,
            channel_type="SEARCH",
            daily_budget_micros=100_000_000,
            budget_resource_name=f"customers/{ACCOUNT}/campaignBudgets/777",
            budget_id="777",
            budget_reference_count=1,
            bidding_strategy_type="MANUAL_CPC",
            positive_geo_target_type=self.positive_geo_target_type,
        )

    async def campaign_locations(self, *, customer_id, campaign_id):
        if self.locations_fail:
            raise AdsReadError("the Google Ads API is unavailable")
        return tuple(self.existing)

    async def geo_targets_by_id(self, *, customer_id, geo_target_ids):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        return tuple(
            CATALOGUE[value] for value in geo_target_ids if value in CATALOGUE
        )

    async def find_geo_targets(
        self, *, customer_id, query, country_code=None, limit=50
    ):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        text = str(query).strip().lower()
        return tuple(
            row for row in CATALOGUE.values() if text in row.name.lower()
        )

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


class FakeExecutor:
    def __init__(self) -> None:
        self.applied: list = []

    async def apply(self, request):
        self.applied.append(request)
        return MutationResult(
            success=True,
            resource_names=(f"customers/{request.customer_id}/campaignCriteria/1",),
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


def _targeted(geo_target_id: str, *, negative: bool = False) -> CampaignLocationRow:
    row = CATALOGUE[geo_target_id]
    return CampaignLocationRow(
        criterion_id=f"c{geo_target_id}",
        geo_target_constant=row.resource_name,
        geo_target_id=geo_target_id,
        negative=negative,
        status="ENABLED",
        display_name=row.canonical_name,
    )


# ===========================================================================
# find_locations
# ===========================================================================

async def test_an_ambiguous_name_returns_every_candidate(linked) -> None:
    """The whole reason this tool exists. "Delhi" is not an answer, it is a
    question, and the server does not answer it on anyone's behalf."""
    h = linked(Tier.READONLY)
    payload = await _call(
        h.mcp, "find_locations", {"customer_id": ACCOUNT, "query": "Delhi"}
    )
    names = {row["canonical_name"] for row in payload["locations"]}
    assert names == {"Delhi,India", "New Delhi,Delhi,India"}
    # The two fields that tell near-identical names apart.
    types = {row["target_type"] for row in payload["locations"]}
    assert types == {"Region", "City"}


async def test_a_readonly_user_can_search_for_locations(linked) -> None:
    """Parity: a READ_ONLY user browses the location picker in the UI."""
    h = linked(Tier.READONLY)
    payload = await _call(
        h.mcp, "find_locations", {"customer_id": ACCOUNT, "query": "India"}
    )
    assert payload["ok"] is True


@pytest.mark.parametrize("hostile", ["Delhi' OR '1'='1", "Del%hi", "D", ""])
async def test_an_unusable_search_term_is_refused(linked, hostile) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(
            h.mcp, "find_locations", {"customer_id": ACCOUNT, "query": hostile}
        )


async def test_a_bad_country_code_is_refused(linked) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp,
            "find_locations",
            {"customer_id": ACCOUNT, "query": "Delhi", "country_code": "India"},
        )
    assert "two-letter" in str(caught.value)


async def test_no_match_says_so_rather_than_returning_silence(linked) -> None:
    h = linked(Tier.READONLY)
    payload = await _call(
        h.mcp, "find_locations", {"customer_id": ACCOUNT, "query": "Atlantis"}
    )
    assert payload["match_count"] == 0
    assert "note" in payload


# ===========================================================================
# add_location_target - drafting
# ===========================================================================

async def test_the_preview_names_every_location_in_full(linked) -> None:
    """"3 locations" would be asking someone to approve a number. The
    canonical name and target type are what make it a decision."""
    h = linked()
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
         "location_ids": ["1007751", "9040379"]},
    )
    preview = draft["preview"]

    assert draft["plan_id"]
    assert "Delhi,India (Region)" in preview
    assert "New Delhi,Delhi,India (City)" in preview
    assert h.executor.applied == []


async def test_the_preview_states_the_presence_change(linked) -> None:
    """Campaign-level, made on someone's behalf, so it has to be visible with
    the value it is replacing."""
    h = linked()
    h.reader.positive_geo_target_type = "PRESENCE_OR_INTEREST"
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    preview = draft["preview"]

    assert "people IN or regularly in these locations" in preview
    assert "PRESENCE_OR_INTEREST" in preview
    assert "searching ABOUT them" in preview


async def test_an_already_presence_campaign_says_unchanged(linked) -> None:
    h = linked()
    h.reader.positive_geo_target_type = "PRESENCE"
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    assert "unchanged" in draft["preview"]


async def test_an_untargeted_campaign_is_told_it_was_serving_everywhere(
    linked,
) -> None:
    """The fact that makes this change worth approving: a Search campaign with
    no location criteria serves worldwide."""
    h = linked()
    h.reader.existing = []
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    assert "serving" in draft["preview"]
    assert "everywhere" in draft["preview"]


async def test_locations_already_targeted_are_not_added_again(linked) -> None:
    """The mutate is all-or-nothing, so one duplicate would fail the whole
    batch - and a preview offering to add what is already there lies."""
    h = linked()
    h.reader.existing = [_targeted("2356")]
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
         "location_ids": ["2356", "1007751"]},
    )
    assert draft["locations_added"] == ["Delhi,India (Region)"]
    assert draft["already_targeted"] == ["India (Country)"]
    assert "already targeted, not added again" in draft["preview"]


async def test_nothing_to_do_when_every_location_is_already_targeted(linked) -> None:
    """pause_campaign's rule. A confirmed, audited change that changed nothing
    makes the audit log harder to read."""
    h = linked()
    h.reader.existing = [_targeted("2356")]
    payload = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    assert payload["no_change_needed"] is True
    assert "plan_id" not in payload
    assert h.executor.applied == []


async def test_targeting_a_location_the_campaign_excludes_is_refused(linked) -> None:
    """A contradiction, and this server has no tool to remove an exclusion, so
    it cannot be resolved here."""
    h = linked()
    h.reader.existing = [_targeted("2356", negative=True)]
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
        )
    assert "EXCLUDES" in str(caught.value)
    assert h.executor.applied == []


async def test_an_unknown_location_id_is_refused(linked) -> None:
    """Refused, not skipped. Silently dropping one would apply a change
    different from the one previewed."""
    h = linked()
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
             "location_ids": ["2356", "99999999"]},
        )
    assert "99999999" in str(caught.value)
    assert h.executor.applied == []


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["Delhi"],                    # a name, not an id
        ["2356", "2356"],             # duplicated
        [str(n) for n in range(21)],  # past what a person will read
    ],
)
async def test_unusable_location_id_lists_are_refused(linked, ids) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ids},
        )
    assert h.executor.applied == []


async def test_a_removed_campaign_is_refused(linked) -> None:
    h = linked()
    h.reader.campaign_status = "REMOVED"
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
        )
    assert "REMOVED" in str(caught.value)


async def test_an_unmanaged_account_is_refused(linked) -> None:
    h = linked()
    with pytest.raises(Exception):
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": "5555555555", "campaign_id": CAMPAIGN,
             "location_ids": ["2356"]},
        )
    assert h.executor.applied == []


async def test_a_readonly_user_cannot_draft_one(linked) -> None:
    h = linked(Tier.READONLY)
    with pytest.raises(Exception):
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
        )


async def test_a_failed_location_read_refuses_rather_than_assuming_none(
    linked,
) -> None:
    """"This campaign targets nothing" and "we could not find out" must not
    look the same - one of them means every location is a duplicate waiting
    to fail the batch."""
    h = linked()
    h.reader.locations_fail = True
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp,
            "add_location_target",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
        )
    assert "Nothing was changed" in str(caught.value)


# ===========================================================================
# add_location_target - confirming
# ===========================================================================

async def test_confirming_sends_every_id_and_the_presence_setting(linked) -> None:
    h = linked()
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
         "location_ids": ["1007751", "9040379"]},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    request = h.executor.applied[0]
    assert request.operation == "add_location_target"
    assert request.payload["geo_target_constant_ids"] == ["1007751", "9040379"]
    assert request.payload["positive_geo_target_type"] == "PRESENCE"


async def test_an_already_presence_campaign_carries_no_campaign_write(linked) -> None:
    """None means "leave the campaign record alone"."""
    h = linked()
    h.reader.positive_geo_target_type = "PRESENCE"
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert h.executor.applied[0].payload["positive_geo_target_type"] is None


async def test_a_presence_setting_changed_after_drafting_refuses_the_plan(
    linked,
) -> None:
    """The reason this tool pays for a second gate pass and a re-read.

    The campaign was already PRESENCE when this was drafted, so the preview
    said "unchanged" and the plan carries no campaign write. If somebody
    switches it back to presence-or-interest in the Google Ads UI first,
    applying the plan would add the locations and leave interest targeting ON -
    under a preview that promised otherwise.
    """
    h = linked()
    h.reader.positive_geo_target_type = "PRESENCE"
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    assert "unchanged" in draft["preview"]

    h.reader.positive_geo_target_type = "PRESENCE_OR_INTEREST"

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "has changed since this was drafted" in str(caught.value)
    assert h.executor.applied == []


async def test_a_read_failure_at_confirm_refuses_without_burning_the_plan(
    linked,
) -> None:
    h = linked()
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )

    h.reader.read_fails = True
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []

    h.reader.read_fails = False
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_demoted_user_cannot_confirm_their_own_draft(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    h.tiers.tier = Tier.READONLY
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied == []


async def test_applying_writes_an_audit_line_charging_no_spend(linked) -> None:
    """Location targeting moves no daily budget: it changes WHERE the existing
    budget is spent, not how much."""
    h = linked()
    draft = await _call(
        h.mcp,
        "add_location_target",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "location_ids": ["2356"]},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [line for line in AuditLog(h.audit_path).iter_records()
               if line.get("applied") is True]
    assert len(applied) == 1
    assert applied[0]["tool"] == "add_location_target"
    assert applied[0].get("spend_delta_units") in (None, "")


async def test_the_kill_switch_hides_the_write_but_not_the_search(linked) -> None:
    """GADS_WRITE_ENABLED off makes the server read-only, not useless -
    finding a location is still a read."""
    h = linked()
    async with Client(h.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "add_location_target" in names
    assert "find_locations" in names
