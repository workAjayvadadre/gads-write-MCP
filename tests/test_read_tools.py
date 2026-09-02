"""The read tools, end to end through a real FastMCP server and a real Guard.

Only two things are fake: the Google Ads reader, and the tier resolver. The
gate, the policy file, the registry, the middleware and the audit log are all
the production objects, so these tests exercise the same ordering of checks
that a live request would.

What is being proved here is mostly that reads are not a side door: the
account allowlist, the tier floor and the audit log all apply to them, even
though no read can spend a rupee.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from gads_write.ads.reads import AccountSummary, AdsReadError, CampaignRow, SearchTermRow
from gads_write.auth.tiers import Tier
from gads_write.mcp_middleware import TierMiddleware
from gads_write.safety.audit import AuditLog
from gads_write.safety.guards import Guard
from gads_write.safety.policy import PolicyStore
from gads_write.safety.spend import DailySpendLedger
from gads_write.settings import Settings
from gads_write.tools.reads import register_read_tools

ACCOUNT = "1234567890"       # on the allowlist in conftest's BASE_POLICY
UNMANAGED = "5555555555"     # deliberately not


@dataclass(frozen=True)
class FakeCaller:
    email: str = "analyst@example.com"
    name: str | None = "Analyst"


class FixedTierResolver:
    def __init__(self, tier: Tier) -> None:
        self.tier = tier

    async def resolve(self, caller, customer_id: str) -> Tier:
        return self.tier

    async def visible_tier(self, caller) -> Tier:
        return self.tier

    @property
    def source(self) -> str:
        return "fixed"


class FakeReader:
    def __init__(self) -> None:
        self.unreachable: set[str] = set()
        self.campaign_calls: list[dict] = []
        self.search_term_calls: list[dict] = []

    async def accessible_customer_ids(self):
        return (ACCOUNT,)

    async def account_summary(self, customer_id: str):
        if customer_id in self.unreachable:
            raise AdsReadError("USER_PERMISSION_DENIED")
        return AccountSummary(
            customer_id=customer_id,
            descriptive_name="Indira IVF - Search",
            currency_code="INR",
            time_zone="Asia/Kolkata",
            is_manager=False,
            is_test_account=True,
            status="ENABLED",
        )

    async def access_role(self, *, customer_id: str, email: str):
        return "READ_ONLY"

    async def campaign_performance(self, **kwargs):
        self.campaign_calls.append(kwargs)
        return (
            CampaignRow(
                campaign_id="55",
                name="Brand - Exact",
                status="ENABLED",
                channel_type="SEARCH",
                daily_budget_micros=12_000_000_000,
                impressions=1000,
                clicks=20,
                cost_micros=10_000_000,
                conversions=3.0,
            ),
        )

    async def search_terms(self, **kwargs):
        self.search_term_calls.append(kwargs)
        return (
            SearchTermRow(
                search_term="ivf cost",
                status="ADDED",
                campaign_id="55",
                campaign_name="Brand - Exact",
                ad_group_id="66",
                ad_group_name="Core",
                impressions=100,
                clicks=5,
                cost_micros=2_500_000,
                conversions=1.0,
            ),
        )


def _settings(tmp_path: Path, policy_path: Path) -> Settings:
    return Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="9999999999",
        write_enabled=False,           # reads must work with writes switched off
        policy_path=policy_path,
        roles_path=tmp_path / "roles.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture
def harness(tmp_path, write_policy):
    """A real server with fake Google and a fixed tier."""

    def _build(tier: Tier = Tier.READONLY, *, reader: FakeReader | None = None):
        policy_path = write_policy()
        settings = _settings(tmp_path, policy_path)
        policy_store = PolicyStore(policy_path)
        audit_log = AuditLog(settings.audit_log_path)
        resolver = FixedTierResolver(tier)
        guard = Guard(
            settings=settings,
            policy_store=policy_store,
            tier_resolver=resolver,
            audit_log=audit_log,
            spend_ledger=DailySpendLedger(audit_log),
        )

        ads_reader = reader or FakeReader()
        mcp = FastMCP(name="test")
        mcp.add_middleware(
            TierMiddleware(
                tier_resolver=resolver,
                settings=settings,
                caller_provider=FakeCaller,
            )
        )
        register_read_tools(
            mcp,
            guard=guard,
            reader=ads_reader,
            policy_store=policy_store,
            caller_provider=FakeCaller,
        )
        return mcp, ads_reader, settings.audit_log_path

    return _build


def _audit_lines(path: Path) -> list[dict]:
    """Read through the log's own interface.

    The audit log partitions files by date, so reaching for the configured
    path directly would be testing a storage detail rather than behaviour.
    """
    return list(AuditLog(path).iter_records())


# ---------------------------------------------------------------------------
# visibility
# ---------------------------------------------------------------------------

async def test_reads_are_visible_with_writes_switched_off(harness) -> None:
    """The kill switch makes the server read-only, not useless."""
    mcp, _, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert {"list_accounts", "get_campaign_performance", "get_search_terms"} <= names


async def test_tier_none_sees_no_read_tools(harness) -> None:
    mcp, _, _ = harness(Tier.NONE)
    async with Client(mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "get_campaign_performance" not in names


# ---------------------------------------------------------------------------
# the allowlist applies to reads
# ---------------------------------------------------------------------------

async def test_an_unmanaged_account_is_refused(harness) -> None:
    """The reason reads go through the gate at all.

    Without this, someone with a personal Google Ads account could read it
    through our developer token, our server and our audit log.
    """
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception) as caught:
            await client.call_tool(
                "get_campaign_performance", {"customer_id": UNMANAGED}
            )

    assert UNMANAGED in str(caught.value)
    # Refused before Google was ever asked.
    assert reader.campaign_calls == []


async def test_a_permitted_read_returns_rows(harness) -> None:
    mcp, _, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_campaign_performance", {"customer_id": ACCOUNT}
        )
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is True
    assert payload["campaigns"][0]["campaign_id"] == "55"


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

async def test_micros_are_converted_once_and_also_returned_raw(harness) -> None:
    """12,000,000,000 micros is 12,000 INR. A missing conversion here is the
    1,000,000x error that type-checks fine."""
    mcp, _, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        result = await client.call_tool(
            "get_campaign_performance", {"customer_id": ACCOUNT}
        )
    campaign = json.loads(result.content[0].text)["campaigns"][0]

    assert campaign["daily_budget_micros"] == 12_000_000_000
    assert "12,000" in campaign["daily_budget"]
    assert "INR" in campaign["daily_budget"]
    # cost 10,000,000 micros over 20 clicks -> 500,000 micros -> 0.50 INR
    assert "0.50" in campaign["average_cpc"]


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

async def test_a_malformed_customer_id_is_refused(harness) -> None:
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "get_campaign_performance", {"customer_id": "123-456-7890"}
            )
    assert reader.campaign_calls == []


async def test_an_out_of_range_limit_is_refused(harness) -> None:
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "get_campaign_performance",
                {"customer_id": ACCOUNT, "limit": 100_000},
            )
    assert reader.campaign_calls == []


async def test_a_backwards_date_range_is_refused(harness) -> None:
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception) as caught:
            await client.call_tool(
                "get_campaign_performance",
                {
                    "customer_id": ACCOUNT,
                    "start_date": "2026-08-31",
                    "end_date": "2026-08-01",
                },
            )
    assert "after end_date" in str(caught.value)
    assert reader.campaign_calls == []


async def test_the_default_window_is_thirty_days_inclusive(harness) -> None:
    from datetime import date

    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        await client.call_tool("get_campaign_performance", {"customer_id": ACCOUNT})

    call = reader.campaign_calls[0]
    start = date.fromisoformat(call["start_date"])
    end = date.fromisoformat(call["end_date"])
    assert (end - start).days + 1 == 30


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

async def test_every_read_is_audited(harness) -> None:
    """"Who looked at this account, and when" has to be answerable."""
    mcp, _, audit_path = harness(Tier.READONLY)
    async with Client(mcp) as client:
        await client.call_tool("get_campaign_performance", {"customer_id": ACCOUNT})

    lines = _audit_lines(audit_path)
    assert len(lines) == 1
    assert lines[0]["tool"] == "get_campaign_performance"
    assert lines[0]["verdict"] == "allowed"
    assert lines[0]["user_email"] == "analyst@example.com"
    assert lines[0]["applied"] is False


async def test_a_refused_read_is_audited_too(harness) -> None:
    mcp, _, audit_path = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "get_campaign_performance", {"customer_id": UNMANAGED}
            )

    lines = _audit_lines(audit_path)
    assert len(lines) == 1
    assert lines[0]["verdict"] == "denied"


# ---------------------------------------------------------------------------
# list_accounts
# ---------------------------------------------------------------------------

async def test_list_accounts_shows_only_the_allowlist(harness) -> None:
    mcp, _, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        result = await client.call_tool("list_accounts", {})
    payload = json.loads(result.content[0].text)

    assert [a["customer_id"] for a in payload["accounts"]] == [ACCOUNT]
    assert payload["accounts"][0]["name"] == "Indira IVF - Search"
    assert payload["your_highest_access"] == "readonly"


async def test_one_unreachable_account_does_not_blank_the_list(harness) -> None:
    reader = FakeReader()
    reader.unreachable = {ACCOUNT}
    mcp, _, _ = harness(Tier.READONLY, reader=reader)

    async with Client(mcp) as client:
        result = await client.call_tool("list_accounts", {})
    payload = json.loads(result.content[0].text)

    assert payload["ok"] is True
    assert payload["accounts"][0]["available"] is False
    assert "USER_PERMISSION_DENIED" in payload["accounts"][0]["note"]


# ---------------------------------------------------------------------------
# search terms
# ---------------------------------------------------------------------------

async def test_search_terms_pass_the_campaign_filter_through(harness) -> None:
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        await client.call_tool(
            "get_search_terms", {"customer_id": ACCOUNT, "campaign_id": "55"}
        )
    assert reader.search_term_calls[0]["campaign_id"] == "55"


async def test_a_non_numeric_campaign_filter_is_refused(harness) -> None:
    mcp, reader, _ = harness(Tier.READONLY)
    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "get_search_terms",
                {"customer_id": ACCOUNT, "campaign_id": "55 OR 1=1"},
            )
    assert reader.search_term_calls == []
