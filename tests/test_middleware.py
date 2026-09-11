"""Tool visibility and call gating, through a real FastMCP server.

These run against an in-memory FastMCP client, so `tools/list` and
`tools/call` really are two independent requests, exactly as they are in
production. That matters: the point of these tests is that filtering the
list does NOT stop a call, and only the call gate does.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastmcp import Client, FastMCP

from gads_write.auth.tiers import Tier, TierLookupError
from gads_write.mcp_middleware import TierMiddleware
from gads_write.settings import Settings
from gads_write.tools.registry import ToolSpec, register, reset_for_tests


@dataclass(frozen=True)
class FakeCaller:
    email: str


class MutableTierResolver:
    """A tier that the test can change mid-session, like a real demotion."""

    def __init__(self, tier: Tier, *, raises: bool = False) -> None:
        self.tier = tier
        self.raises = raises

    async def resolve(self, caller, customer_id: str) -> Tier:
        if self.raises:
            raise TierLookupError("outage")
        return self.tier

    async def visible_tier(self, caller) -> Tier:
        if self.raises:
            raise TierLookupError("outage")
        return self.tier

    @property
    def source(self) -> str:
        return "mutable"


def _settings(tmp_path, *, write_enabled: bool) -> Settings:
    return Settings(
        env="test", host="127.0.0.1", port=8081, base_url="https://example.com",
        oauth_client_id="x.apps.googleusercontent.com", oauth_client_secret="s",
        jwt_signing_key="k", developer_token="d", login_customer_id="1234567890",
        write_enabled=write_enabled, roles_path=tmp_path / "r.yaml",
        audit_log_path=tmp_path / "audit.jsonl",
    )


@pytest.fixture(autouse=True)
def _registry():
    reset_for_tests()
    # update_campaign_budget (operator) and create_responsive_search_ad
    # (lead) are real registry builtins from Phase 5, so they are NOT
    # registered here - these tests now run against the production specs
    # rather than hand-made copies of them.
    register(ToolSpec(name="get_campaigns", required_tier=Tier.READONLY, writes=False))
    # A synthetic lead-only tool. No REAL write tool requires `lead` any more -
    # a Standard Google Ads user can do all of them in the UI - but the tier
    # mechanism still has to work, so it is exercised against a tool invented
    # for the purpose rather than against a production spec that may move.
    register(ToolSpec(name="lead_only_thing", required_tier=Tier.LEAD, writes=True,
                      operation="lead_only_thing"))
    yield
    reset_for_tests()


def _server(tmp_path, resolver, *, write_enabled: bool = True) -> FastMCP:
    """A FastMCP server with the middleware and a matching set of tools.

    No auth provider: identity is injected through `caller_provider`, so the
    tests exercise the tier logic rather than the OAuth flow.
    """
    mcp = FastMCP(name="test")
    mcp.add_middleware(
        TierMiddleware(
            tier_resolver=resolver,
            settings=_settings(tmp_path, write_enabled=write_enabled),
            caller_provider=lambda: FakeCaller("user@example.com"),
        )
    )

    @mcp.tool
    async def health_check() -> str:
        return "ok"

    @mcp.tool
    async def get_campaigns() -> str:
        return "campaigns"

    @mcp.tool
    async def update_campaign_budget() -> str:
        return "changed the budget"

    @mcp.tool
    async def create_responsive_search_ad() -> str:
        return "made an ad"

    @mcp.tool
    async def lead_only_thing() -> str:
        return "something only a lead may do"

    return mcp


async def _names(mcp: FastMCP) -> set[str]:
    async with Client(mcp) as client:
        return {tool.name for tool in await client.list_tools()}


# ---------------------------------------------------------------------------
# tools/list filtering
# ---------------------------------------------------------------------------

async def test_readonly_does_not_see_write_tools(tmp_path) -> None:
    """The gate condition. Not merely refused on call - not shown at all."""
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.READONLY)))
    assert names == {"health_check", "get_campaigns"}
    assert "update_campaign_budget" not in names


async def test_an_operator_sees_every_real_write_tool(tmp_path) -> None:
    """Parity with the Google Ads UI: a STANDARD user can create and edit
    campaigns, keywords, ads, budgets and bids there, so every write tool here
    is reachable at `operator`."""
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.OPERATOR)))
    assert "update_campaign_budget" in names
    assert "create_responsive_search_ad" in names


async def test_the_tier_mechanism_still_filters(tmp_path) -> None:
    """No production write tool needs `lead`, so this is checked against a
    synthetic one - otherwise moving a real tool's tier would silently delete
    the only coverage of the filter itself."""
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.OPERATOR)))
    assert "lead_only_thing" not in names


async def test_lead_sees_everything(tmp_path) -> None:
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.LEAD)))
    assert "lead_only_thing" in names
    assert "get_campaigns" in names
    assert "health_check" in names


async def test_tier_none_sees_only_health_check(tmp_path) -> None:
    # Someone not yet granted access must still be able to ask why they have
    # no tools, rather than seeing an empty list that looks like a broken
    # server.
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.NONE)))
    assert names == {"health_check"}


async def test_kill_switch_hides_every_write_tool(tmp_path) -> None:
    names = await _names(
        _server(tmp_path, MutableTierResolver(Tier.LEAD), write_enabled=False)
    )
    assert names == {"health_check", "get_campaigns"}


async def test_a_tier_lookup_outage_hides_privileged_tools(tmp_path) -> None:
    names = await _names(_server(tmp_path, MutableTierResolver(Tier.LEAD, raises=True)))
    assert names == {"health_check"}


# ---------------------------------------------------------------------------
# tools/call gating
# ---------------------------------------------------------------------------

async def test_calling_a_hidden_tool_is_still_refused(tmp_path) -> None:
    """The one that proves hiding is not the security mechanism.

    `tools/list` and `tools/call` are unrelated requests. A client can name
    any tool it likes, including one that was filtered out of its listing.
    """
    mcp = _server(tmp_path, MutableTierResolver(Tier.READONLY))

    async with Client(mcp) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert "update_campaign_budget" not in listed

        with pytest.raises(Exception) as caught:
            await client.call_tool("update_campaign_budget", {})

    assert "requires tier operator" in str(caught.value)


async def test_the_kill_switch_refuses_the_call_too(tmp_path) -> None:
    mcp = _server(tmp_path, MutableTierResolver(Tier.LEAD), write_enabled=False)
    async with Client(mcp) as client:
        with pytest.raises(Exception) as caught:
            await client.call_tool("update_campaign_budget", {})
    assert "GADS_WRITE_ENABLED" in str(caught.value)


async def test_a_permitted_call_runs(tmp_path) -> None:
    mcp = _server(tmp_path, MutableTierResolver(Tier.OPERATOR))
    async with Client(mcp) as client:
        result = await client.call_tool("update_campaign_budget", {})
    assert "changed the budget" in str(result.content[0].text)


async def test_a_lookup_outage_refuses_the_call(tmp_path) -> None:
    mcp = _server(tmp_path, MutableTierResolver(Tier.LEAD, raises=True))
    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool("update_campaign_budget", {})


# ---------------------------------------------------------------------------
# mid-session tier changes
# ---------------------------------------------------------------------------

async def test_a_demotion_takes_effect_on_the_next_call(tmp_path) -> None:
    """The gate condition: no reconnect required.

    The same client, on the same connection, having already listed the tool.
    A demotion lands immediately because the tier is resolved per call rather
    than captured at connect.
    """
    resolver = MutableTierResolver(Tier.OPERATOR)
    mcp = _server(tmp_path, resolver)

    async with Client(mcp) as client:
        result = await client.call_tool("update_campaign_budget", {})
        assert "changed the budget" in str(result.content[0].text)

        resolver.tier = Tier.READONLY  # removed from the account in Google Ads

        with pytest.raises(Exception) as caught:
            await client.call_tool("update_campaign_budget", {})
        assert "requires tier operator" in str(caught.value)


async def test_a_demotion_also_updates_the_tool_list(tmp_path) -> None:
    resolver = MutableTierResolver(Tier.OPERATOR)
    mcp = _server(tmp_path, resolver)

    async with Client(mcp) as client:
        assert "update_campaign_budget" in {t.name for t in await client.list_tools()}
        resolver.tier = Tier.READONLY
        assert "update_campaign_budget" not in {t.name for t in await client.list_tools()}


async def test_a_promotion_takes_effect_without_reconnecting(tmp_path) -> None:
    resolver = MutableTierResolver(Tier.READONLY)
    mcp = _server(tmp_path, resolver)

    async with Client(mcp) as client:
        with pytest.raises(Exception):
            await client.call_tool("update_campaign_budget", {})

        resolver.tier = Tier.OPERATOR

        result = await client.call_tool("update_campaign_budget", {})
        assert "changed the budget" in str(result.content[0].text)
