"""Tier-aware tool listing and call gating.

MCP has no URL paths. Every request arrives at `POST /mcp` with the tool
name as a string in the JSON body, so you cannot write the equivalent of
`app.use('/admin', requireAdmin)`. This middleware is what replaces that.

Two hooks, doing two different jobs:

  on_list_tools   Filters the menu. A readonly analyst never SEES
                  update_campaign_budget, so their Claude never proposes it
                  and they are never refused for something they were shown.
                  This is user experience and prompt hygiene.

  on_call_tool    Refuses the call. This is the security boundary.

Both are needed, and confusing them is a real mistake. `tools/list` and
`tools/call` are independent requests over the same endpoint with no session
tying them together: a client can call a tool it never listed, one that was
filtered out of its listing, or one it cached an hour ago before the user
was demoted. Filtering the list changes what the model knows about. Only the
check on the call refuses one.

This middleware is the COARSE gate. `tools/list` carries no customer_id -
the model is asking "what can I do?", not "on what?" - so it can only ask
whether the caller holds the tier on ANY permitted account. The
authoritative, per-account decision happens later, in safety/guards.py,
which knows which account is being touched. Two layers on purpose: this one
refuses early and cheaply; the gate refuses correctly.

Python notes for a TypeScript reader:
  - `call_next` is the same idea as Express's `next()`, except it returns
    the downstream result rather than mutating a response object.
  - `caller_provider` is injected so tests can supply an identity without
    standing up an OAuth flow.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .auth.identity import AuthError, current_caller
from .auth.tiers import Tier, TierLookupError, TierResolver, tier_at_least
from .settings import Settings
from .tools.registry import spec_for

logger = logging.getLogger(__name__)


class TierMiddleware(Middleware):
    """Filters the tool list and refuses calls above the caller's tier."""

    def __init__(
        self,
        *,
        tier_resolver: TierResolver,
        settings: Settings,
        caller_provider: Callable[[], Any] = current_caller,
    ) -> None:
        self._tiers = tier_resolver
        self._settings = settings
        self._caller_provider = caller_provider

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _visible_tier(self) -> Tier:
        """The caller's best tier anywhere, or NONE if we cannot tell.

        Fails closed on every path. An unauthenticated request, a caller with
        no email, or an indeterminate lookup all produce NONE, which reveals
        only health_check and refuses everything else.
        """
        try:
            caller = self._caller_provider()
        except AuthError:
            return Tier.NONE
        if caller is None:
            return Tier.NONE

        try:
            return await self._tiers.visible_tier(caller)
        except TierLookupError as exc:
            # Deliberately not re-raised. A Google outage must not become an
            # escalation path, and it must not crash tool listing either.
            logger.warning("tier lookup failed, showing no privileged tools: %s", exc)
            return Tier.NONE

    def _permitted(self, tool_name: str, tier: Tier) -> tuple[bool, str | None]:
        """Whether this tier may see and call this tool, and why not."""
        spec = spec_for(tool_name)

        if spec.writes and not self._settings.write_enabled:
            return False, (
                "writes are disabled on this server (GADS_WRITE_ENABLED=false)"
            )

        if not tier_at_least(tier, spec.required_tier):
            return False, (
                f"{tool_name} requires tier {spec.required_tier.value}; "
                f"your access level is {tier.value}"
            )

        return True, None

    # ------------------------------------------------------------------
    # hooks
    # ------------------------------------------------------------------

    async def on_list_tools(
        self, context: MiddlewareContext, call_next
    ) -> Sequence[Any]:
        """Show only the tools this caller could actually use."""
        tools = await call_next(context)
        tier = await self._visible_tier()
        return [tool for tool in tools if self._permitted(tool.name, tier)[0]]

    async def on_call_tool(self, context: MiddlewareContext, call_next) -> Any:
        """Refuse tools above the caller's tier, before the tool body runs."""
        tool_name = context.message.name
        tier = await self._visible_tier()

        allowed, reason = self._permitted(tool_name, tier)
        if not allowed:
            # ToolError text reaches the model, and through it the user, so
            # it says what to do rather than just "forbidden".
            raise ToolError(
                f"{reason}. Nothing was changed. "
                "Ask a lead if you need this access."
            )

        return await call_next(context)
