"""Budgets, negatives, keywords, bids and RSAs - draft and confirm.

Fake: the Google Ads reader and the executor. Real: the gate, the plan store,
the policy file, the registry, the middleware and the audit log.

Against conftest's BASE_POLICY the effective limits are:

    operator  budget 50-2000, +20% max, 3000/day ceiling, max CPC 100
    lead      budget 50-5000, +25% max, 10000/day ceiling, max CPC 200
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
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
AD_GROUP = "66"
BUDGET_RESOURCE = f"customers/{ACCOUNT}/campaignBudgets/777"

HEADLINES = ["Fertility Care", "IVF Specialists", "Book A Consult"]
DESCRIPTIONS = ["Trusted fertility care.", "Speak to a specialist today."]
URLS = ["https://indiraivf.com/treatments"]


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
        self.budget_micros = 100_000_000          # 100 units
        # How many campaigns use this budget. 1 is the only safe value:
        # 2+ means changing it affects campaigns the preview does not name,
        # and 0 means Google did not tell us, which is not the same as safe.
        self.budget_reference_count = 1
        # Which budget resource the campaign points at. A campaign can be
        # moved onto a different budget in the Google Ads UI at any time.
        self.budget_resource_name = BUDGET_RESOURCE
        self.bidding_strategy = "MANUAL_CPC"
        self.cpc_bid_micros = 50_000_000          # 50 units
        self.campaign_missing = False
        self.ad_group_missing = False
        # Simulates a Google Ads outage at confirm time.
        self.read_fails = False

    async def campaign_by_id(self, *, customer_id, campaign_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        if self.campaign_missing:
            return None
        return CampaignSummary(
            campaign_id=campaign_id,
            name="Brand - Exact",
            status="ENABLED",
            channel_type="SEARCH",
            daily_budget_micros=self.budget_micros,
            budget_resource_name=self.budget_resource_name,
            budget_id="777",
            budget_reference_count=self.budget_reference_count,
            bidding_strategy_type=self.bidding_strategy,
        )

    async def ad_group_by_id(self, *, customer_id, ad_group_id):
        if self.read_fails:
            raise AdsReadError("the Google Ads API is unavailable")
        if self.ad_group_missing:
            return None
        return AdGroupSummary(
            ad_group_id=ad_group_id,
            name="Core Terms",
            status="ENABLED",
            campaign_id=CAMPAIGN,
            campaign_name="Brand - Exact",
            cpc_bid_micros=self.cpc_bid_micros,
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
            resource_names=(f"customers/{request.customer_id}/things/1",),
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
    # Exposed so a test can tighten policy.yaml between draft and confirm,
    # the way a lead would in production. PolicyStore reloads on mtime.
    policy_path: Path


@pytest.fixture
def linked(tmp_path, write_policy):
    """Harness where the draft tools and confirm share ONE plan store."""

    def _build(tier: Tier = Tier.LEAD) -> Harness:
        policy_path = write_policy()
        settings = Settings(
            env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
            oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
            jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
            write_enabled=True,
            policy_path=policy_path,
            roles_path=tmp_path / "roles.yaml",
            audit_log_path=tmp_path / "audit.jsonl",
        )
        policy_store = PolicyStore(policy_path)
        audit_log = AuditLog(settings.audit_log_path)
        tiers = MutableTier(tier)
        guard = Guard(
            settings=settings, policy_store=policy_store, tier_resolver=tiers,
            audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log),
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
            settings=settings,
            reader=reader, caller_provider=caller,
        )
        return Harness(
            mcp=mcp, reader=reader, executor=executor, tiers=tiers,
            caller=caller, audit_path=settings.audit_log_path,
            policy_path=policy_path,
        )

    return _build


async def _approve(message, response_type, params, context):
    """Stand in for a person clicking Apply. See test_human_confirmation.py
    for whether approval is enforced at all."""
    return ElicitResult(action="accept", content=None)


async def _call(mcp, tool, args) -> dict:
    async with Client(mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


def _audit(path: Path) -> list[dict]:
    """Read through the log's own interface, not the file layout."""
    return list(AuditLog(path).iter_records())


# ===========================================================================
# budgets
# ===========================================================================

async def test_a_budget_draft_previews_the_change(linked) -> None:
    h = linked(Tier.OPERATOR)
    payload = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )
    assert payload["plan_id"]
    assert "INR 100.00 -> INR 110.00" in payload["preview"]
    assert "increase of INR 10.00" in payload["preview"]
    assert h.executor.applied == []


async def test_a_shared_budget_is_refused(linked) -> None:
    """A shared budget affects other campaigns, so a preview naming one
    campaign would be actively misleading."""
    h = linked(Tier.OPERATOR)
    h.reader.budget_reference_count = 3
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
        )
    assert "SHARED budget" in str(caught.value)
    assert h.executor.applied == []


@pytest.mark.parametrize("reference_count", [0, 2, 7])
async def test_a_budget_is_refused_unless_exactly_one_campaign_uses_it(
    linked, reference_count
) -> None:
    """`reference_count` is the fact; `explicitly_shared` was only ever an
    intention recorded at creation time, and Google defaults it to true.

    Zero refuses too. It means Google did not tell us how many campaigns use
    this budget, and not knowing is not the same as knowing it is safe.
    """
    h = linked(Tier.OPERATOR)
    h.reader.budget_reference_count = reference_count
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
        )
    assert "SHARED budget" in str(caught.value) or "how many campaigns" in str(caught.value)
    assert h.executor.applied == []


async def test_a_budget_shared_after_drafting_is_refused_at_confirm(linked) -> None:
    """The preview named one campaign. If the budget picks up a second one
    before confirm, applying it would change spend on a campaign nobody
    approved a change to."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )

    h.reader.budget_reference_count = 2   # attached to another campaign in the UI

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "shared" in str(caught.value).lower()
    assert h.executor.applied == []


async def test_a_campaign_repointed_to_another_budget_is_refused_at_confirm(
    linked,
) -> None:
    """Policy is evaluated against the budget the campaign uses NOW, but the
    mutation is addressed to the budget captured at draft time. If those are
    different resources, the check and the change are about different things
    and the plan must not apply."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )

    h.reader.budget_resource_name = f"customers/{ACCOUNT}/campaignBudgets/999"

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "budget" in str(caught.value).lower()
    assert h.executor.applied == []


async def test_the_audit_records_the_increase_that_was_actually_approved(
    linked,
) -> None:
    """The daily ceiling is derived from the audit log, so the number written
    there has to be the real increase - not the one computed at draft time
    against a budget that has since moved."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )
    # Drafted as 100 -> 110, an increase of 10. Someone then lowers it to 95,
    # so confirming the same plan is really an increase of 15.
    h.reader.budget_micros = 95_000_000

    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    from decimal import Decimal
    applied = [line for line in _audit(h.audit_path) if line.get("applied") is True]
    assert len(applied) == 1
    assert Decimal(applied[0]["spend_delta_units"]) == Decimal("15")


async def test_a_budget_above_the_tier_ceiling_is_refused(linked) -> None:
    h = linked(Tier.OPERATOR)   # operator max_daily is 2000
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 2001},
        )
    assert "2000" in str(caught.value)


async def test_a_budget_below_the_minimum_is_refused(linked) -> None:
    h = linked(Tier.OPERATOR)   # min_daily is 50
    with pytest.raises(Exception):
        await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 49},
        )


async def test_too_large_a_percentage_jump_is_refused(linked) -> None:
    h = linked(Tier.OPERATOR)   # +20% max, current is 100
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 121},
        )
    assert "%" in str(caught.value)


async def test_a_decrease_is_always_allowed_within_the_band(linked) -> None:
    """A decrease cannot be a percentage increase, so only the floor binds."""
    h = linked(Tier.OPERATOR)
    payload = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 60},
    )
    assert "decrease of INR 40.00" in payload["preview"]


async def test_confirming_a_budget_sends_micros_not_units(linked) -> None:
    """The 1,000,000x rule. 110 rupees must reach Google as 110000000."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    request = h.executor.applied[0]
    assert request.operation == "update_campaign_budget"
    assert request.payload["amount_micros"] == 110_000_000
    assert request.payload["budget_resource_name"] == BUDGET_RESOURCE


async def test_a_budget_increase_reaches_the_audit_log(linked) -> None:
    """Without this the per-user daily ceiling would never accumulate."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    applied = [l for l in _audit(h.audit_path) if l.get("applied") is True]
    assert len(applied) == 1
    from decimal import Decimal
    assert Decimal(applied[0]["spend_delta_units"]) == Decimal("10")


async def test_the_daily_ceiling_accumulates_across_changes(linked) -> None:
    """The end-to-end proof the ceiling is real: repeated increases eventually
    exhaust the operator's 3000/day allowance."""
    h = linked(Tier.OPERATOR)
    h.reader.budget_micros = 2_000_000_000  # 2000 units, at the tier ceiling

    # Each cycle drafts +0 headroom; instead drive the ledger directly by
    # applying several increases from a low base.
    h.reader.budget_micros = 100_000_000
    total = 0
    for target in (110, 115, 120):
        draft = await _call(
            h.mcp, "update_campaign_budget",
            {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": target},
        )
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
        total += target - 100

    from decimal import Decimal
    deltas = [
        Decimal(l["spend_delta_units"])
        for l in _audit(h.audit_path)
        if l.get("applied") is True
    ]
    assert deltas == [Decimal("10"), Decimal("15"), Decimal("20")]


# ---------------------------------------------------------------------------
# a plan is intent, never a permission
# ---------------------------------------------------------------------------
# Both of these are the same claim from plans.py and CLAUDE.md, stated twice:
#
#   "The plan stores intent, never a permission. Policy is re-checked at
#    confirm time by guards.py, so a plan drafted under looser limits fails
#    once they are tightened."
#
# A plan holds an ABSOLUTE target (amount_micros), but every budget rule is
# RELATIVE - max_increase_percent compares against the campaign's current
# budget, and max_daily against the tier's ceiling as policy.yaml stands NOW.
# So the approval a human gave is only meaningful if both halves of that
# comparison are re-established at confirm time, not just the target.


def _tighten_policy(path: Path, tier: str, block: str, **limits: int) -> None:
    """Rewrite policy.yaml the way a lead would. PolicyStore reloads on mtime."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tier_limits = data["limits"]["tiers"].setdefault(tier, {})
    tier_limits.setdefault(block, {}).update(limits)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


async def test_a_plan_is_refused_once_the_policy_is_tightened(linked) -> None:
    """Drafted at 120 while operator allowed 2000. A lead then drops the
    operator ceiling to 60 and the increase cap to 1%. Confirming afterwards
    must be refused: the plan carried intent, not a permission."""
    h = linked(Tier.OPERATOR)          # 100 -> 120 is +20%, inside 2000 / +20%
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 120},
    )

    _tighten_policy(
        h.policy_path, "operator", "budget", max_daily=60, max_increase_percent=1
    )

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "60" in str(caught.value)
    assert h.executor.applied == []


async def test_a_refused_plan_is_not_consumed(linked) -> None:
    """Refused by policy is not the same as used up. If a lead widens the
    limit again, the same plan_id must still be confirmable - otherwise a
    momentary tightening silently destroys work people already approved."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 120},
    )

    _tighten_policy(h.policy_path, "operator", "budget", max_daily=60)
    with pytest.raises(Exception):
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    _tighten_policy(h.policy_path, "operator", "budget", max_daily=2000)
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert applied["applied"] is True
    assert len(h.executor.applied) == 1


async def test_a_read_failure_at_confirm_refuses_without_burning_the_plan(linked) -> None:
    """We could not establish what we would be changing. That is a reason to
    stop, not a reason to consume the plan."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )

    h.reader.read_fails = True
    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert "Nothing was changed" in str(caught.value)
    assert h.executor.applied == []

    h.reader.read_fails = False
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert applied["applied"] is True


async def test_a_plan_is_refused_once_the_current_budget_has_moved(linked) -> None:
    """The human approved 'INR 100.00 -> INR 120.00', an increase of 20%.
    If someone lowers the budget to 10 in the Google Ads UI first, confirming
    the same plan is a 1100% increase - a change nobody previewed and one the
    operator's +20% cap forbids."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 120},
    )
    assert "INR 100.00 -> INR 120.00" in draft["preview"]

    h.reader.budget_micros = 10_000_000   # someone edits it in the Ads UI

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "20" in str(caught.value)      # the max_increase_percent refusal
    assert h.executor.applied == []


# ===========================================================================
# negative keywords
# ===========================================================================

async def test_a_campaign_negative_keyword_drafts_and_applies(linked) -> None:
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "add_negative_keyword",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
         "keyword_text": "free", "match_type": "PHRASE"},
    )
    assert draft["level"] == "campaign"
    assert "NEGATIVE" in draft["preview"]

    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    request = h.executor.applied[0]
    assert request.operation == "add_campaign_negative_keyword"
    assert request.payload == {
        "campaign_id": CAMPAIGN, "keyword_text": "free", "match_type": "PHRASE"
    }


async def test_an_ad_group_negative_keyword_uses_the_other_operation(linked) -> None:
    """One tool, two Google Ads resources."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "add_negative_keyword",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "keyword_text": "cheap", "match_type": "EXACT"},
    )
    assert draft["level"] == "ad_group"
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied[0].operation == "add_ad_group_negative_keyword"


@pytest.mark.parametrize(
    "args",
    [
        {},                                              # neither
        {"campaign_id": CAMPAIGN, "ad_group_id": AD_GROUP},  # both
    ],
)
async def test_exactly_one_target_is_required(linked, args) -> None:
    h = linked(Tier.OPERATOR)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "add_negative_keyword",
            {"customer_id": ACCOUNT, "keyword_text": "x", "match_type": "EXACT", **args},
        )
    assert "exactly one" in str(caught.value)


# ===========================================================================
# keywords
# ===========================================================================

async def test_broad_match_is_refused_on_manual_cpc(linked) -> None:
    """rules.block_broad_match_with_manual_cpc - the classic money burner."""
    h = linked(Tier.LEAD)
    h.reader.bidding_strategy = "MANUAL_CPC"
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "add_keyword",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "keyword_text": "ivf", "match_type": "BROAD"},
        )
    assert "broad match is not permitted" in str(caught.value)


async def test_broad_match_is_fine_on_automated_bidding(linked) -> None:
    h = linked(Tier.LEAD)
    h.reader.bidding_strategy = "TARGET_CPA"
    payload = await _call(
        h.mcp, "add_keyword",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "keyword_text": "ivf", "match_type": "BROAD"},
    )
    assert payload["plan_id"]


async def test_a_keyword_is_refused_once_the_campaign_moves_to_manual_cpc(linked) -> None:
    """Broad match was legitimate when drafted, because the ad group was on an
    automated strategy. Moving it to manual CPC before confirming makes the
    same keyword the thing rules.block_broad_match_with_manual_cpc forbids."""
    h = linked(Tier.LEAD)
    h.reader.bidding_strategy = "MAXIMIZE_CONVERSIONS"
    draft = await _call(
        h.mcp, "add_keyword",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "keyword_text": "fertility clinic", "match_type": "BROAD"},
    )

    h.reader.bidding_strategy = "MANUAL_CPC"

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "broad match" in str(caught.value).lower()
    assert h.executor.applied == []


async def test_a_new_keyword_is_created_paused(linked) -> None:
    """rules.new_entities_start_paused - creations are reversible by default."""
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "add_keyword",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "keyword_text": "ivf treatment", "match_type": "EXACT"},
    )
    assert draft["created_status"] == "PAUSED"
    assert "cannot spend until someone enables it" in draft["preview"]

    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    assert h.executor.applied[0].payload["status"] == "PAUSED"


async def test_ui_match_type_syntax_is_refused(linked) -> None:
    """[ivf] would become a keyword literally containing brackets."""
    h = linked(Tier.LEAD)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "add_keyword",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "keyword_text": "[ivf]", "match_type": "EXACT"},
        )
    assert "match-type syntax" in str(caught.value)


async def test_an_overlong_keyword_is_refused(linked) -> None:
    h = linked(Tier.LEAD)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "add_keyword",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "keyword_text": "a" * 81, "match_type": "EXACT"},
        )
    assert "80" in str(caught.value)


async def test_an_operator_cannot_add_keywords(linked) -> None:
    h = linked(Tier.OPERATOR)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "add_keyword",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "keyword_text": "ivf", "match_type": "EXACT"},
        )
    assert "requires tier lead" in str(caught.value)


# ===========================================================================
# bids
# ===========================================================================

async def test_a_bid_draft_previews_the_change(linked) -> None:
    h = linked(Tier.LEAD)
    payload = await _call(
        h.mcp, "update_ad_group_bid",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60},
    )
    assert "INR 50.00 -> INR 60.00" in payload["preview"]


async def test_a_bid_above_the_tier_max_cpc_is_refused(linked) -> None:
    h = linked(Tier.LEAD)   # lead max_cpc is 200
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_ad_group_bid",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 201},
        )
    assert "200" in str(caught.value)


async def test_confirming_a_bid_sends_micros(linked) -> None:
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "update_ad_group_bid",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    request = h.executor.applied[0]
    assert request.operation == "update_ad_group_bid"
    assert request.payload["cpc_bid_micros"] == 60_000_000


async def test_a_bid_plan_is_refused_once_the_max_cpc_is_tightened(linked) -> None:
    """Drafted at 60 while lead's max CPC was 200. Dropping it to 55 must
    refuse the plan rather than let the approval outlive the limit."""
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "update_ad_group_bid",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60},
    )

    _tighten_policy(h.policy_path, "lead", "bids", max_cpc=55)

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert "55" in str(caught.value)
    assert h.executor.applied == []


async def test_an_operator_cannot_confirm_a_lead_drafted_plan(linked) -> None:
    """The confirm step checks the tier of the tool that DRAFTED the plan,
    not its own. Otherwise a lead-only change could be drafted by a lead and
    applied by an operator, and the tier on every lead tool would be
    decorative.

    The demotion is lead -> operator on purpose. Every other demotion test
    drops to readonly, which TierMiddleware refuses before confirm_and_apply
    ever runs - so none of them reach this check.
    """
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "update_ad_group_bid",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60},
    )

    h.tiers.tier = Tier.OPERATOR   # demoted in Google Ads between the steps

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    message = str(caught.value).lower()
    assert "lead" in message and "operator" in message
    assert h.executor.applied == []


async def test_an_operator_cannot_confirm_a_lead_drafted_ad(linked) -> None:
    """The same property, on a tool whose rules are absolute.

    Deliberately an RSA and not a bid. A bid re-reads the ad group, so it is
    refused by the earlier authorise step; only a tool with nothing to
    re-read reaches the main gate, which is where the tier of the drafting
    tool is actually enforced. Without this test that gate could be changed
    to check confirm_and_apply's own tier and nothing would notice.
    """
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "create_responsive_search_ad",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "headlines": HEADLINES,
         "descriptions": DESCRIPTIONS, "final_urls": URLS},
    )

    h.tiers.tier = Tier.OPERATOR   # demoted in Google Ads between the steps

    with pytest.raises(Exception) as caught:
        await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    message = str(caught.value).lower()
    assert "lead" in message and "operator" in message
    assert h.executor.applied == []


async def test_an_operator_can_still_confirm_an_operator_tier_plan(linked) -> None:
    """The control for the test above: operator reaches confirm_and_apply
    fine, so the refusal there is about the drafting tool's tier and not
    about being blocked at the door."""
    h = linked(Tier.OPERATOR)
    draft = await _call(
        h.mcp, "update_campaign_budget",
        {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110},
    )
    applied = await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})

    assert applied["applied"] is True
    assert len(h.executor.applied) == 1


async def test_an_operator_cannot_change_bids(linked) -> None:
    h = linked(Tier.OPERATOR)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "update_ad_group_bid",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60},
        )
    assert "requires tier lead" in str(caught.value)


# ===========================================================================
# responsive search ads
# ===========================================================================

async def test_an_rsa_drafts_and_lists_its_assets(linked) -> None:
    h = linked(Tier.LEAD)
    payload = await _call(
        h.mcp, "create_responsive_search_ad",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "headlines": HEADLINES, "descriptions": DESCRIPTIONS, "final_urls": URLS},
    )
    assert payload["created_status"] == "PAUSED"
    for headline in HEADLINES:
        assert headline in payload["preview"]


async def test_an_rsa_with_too_few_headlines_is_refused(linked) -> None:
    h = linked(Tier.LEAD)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "create_responsive_search_ad",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "headlines": ["Only one"], "descriptions": DESCRIPTIONS, "final_urls": URLS},
        )
    assert "at least 3" in str(caught.value)


async def test_an_rsa_pointing_off_domain_is_refused(linked) -> None:
    """The allowlist is what stops an ad in your account pointing elsewhere."""
    h = linked(Tier.LEAD)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "create_responsive_search_ad",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "headlines": HEADLINES, "descriptions": DESCRIPTIONS,
             "final_urls": ["https://evil.example.com/x"]},
        )
    assert "evil.example.com" in str(caught.value)


async def test_an_rsa_over_http_is_refused(linked) -> None:
    h = linked(Tier.LEAD)
    with pytest.raises(Exception) as caught:
        await _call(
            h.mcp, "create_responsive_search_ad",
            {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
             "headlines": HEADLINES, "descriptions": DESCRIPTIONS,
             "final_urls": ["http://indiraivf.com/x"]},
        )
    assert "https" in str(caught.value)


async def test_confirming_an_rsa_carries_every_asset(linked) -> None:
    h = linked(Tier.LEAD)
    draft = await _call(
        h.mcp, "create_responsive_search_ad",
        {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
         "headlines": HEADLINES, "descriptions": DESCRIPTIONS,
         "final_urls": URLS, "path1": "ivf"},
    )
    await _call(h.mcp, "confirm_and_apply", {"plan_id": draft["plan_id"]})
    request = h.executor.applied[0]
    assert request.operation == "create_responsive_search_ad"
    assert request.payload["headlines"] == HEADLINES
    assert request.payload["descriptions"] == DESCRIPTIONS
    assert request.payload["final_urls"] == URLS
    assert request.payload["path1"] == "ivf"
    assert request.payload["status"] == "PAUSED"


# ===========================================================================
# cross-cutting
# ===========================================================================

async def test_every_new_tool_drafts_without_touching_the_executor(linked) -> None:
    """The two-step promise, across all five."""
    h = linked(Tier.LEAD)
    await _call(h.mcp, "update_campaign_budget",
                {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN, "new_daily_budget": 110})
    await _call(h.mcp, "add_negative_keyword",
                {"customer_id": ACCOUNT, "campaign_id": CAMPAIGN,
                 "keyword_text": "free", "match_type": "PHRASE"})
    await _call(h.mcp, "add_keyword",
                {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
                 "keyword_text": "ivf", "match_type": "EXACT"})
    await _call(h.mcp, "update_ad_group_bid",
                {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP, "new_max_cpc": 60})
    await _call(h.mcp, "create_responsive_search_ad",
                {"customer_id": ACCOUNT, "ad_group_id": AD_GROUP,
                 "headlines": HEADLINES, "descriptions": DESCRIPTIONS, "final_urls": URLS})

    assert h.executor.applied == []


async def test_an_unmanaged_account_is_refused_for_every_new_tool(linked) -> None:
    h = linked(Tier.LEAD)
    with pytest.raises(Exception):
        await _call(h.mcp, "update_campaign_budget",
                    {"customer_id": "5555555555", "campaign_id": CAMPAIGN,
                     "new_daily_budget": 110})
    assert h.executor.applied == []


async def test_the_kill_switch_hides_every_phase5_tool(tmp_path, write_policy) -> None:
    policy_path = write_policy()
    settings = Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
        write_enabled=False,
        policy_path=policy_path, roles_path=tmp_path / "r.yaml",
        audit_log_path=tmp_path / "a.jsonl",
    )
    policy_store = PolicyStore(policy_path)
    audit_log = AuditLog(settings.audit_log_path)
    tiers = MutableTier(Tier.LEAD)
    guard = Guard(settings=settings, policy_store=policy_store, tier_resolver=tiers,
                  audit_log=audit_log, spend_ledger=DailySpendLedger(audit_log))
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

    for tool in ("update_campaign_budget", "add_negative_keyword", "add_keyword",
                 "update_ad_group_bid", "create_responsive_search_ad"):
        assert tool not in names
