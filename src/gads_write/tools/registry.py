"""Which tier each tool requires, and whether it can change anything.

There are no URL paths in MCP. Every request arrives at `POST /mcp` with the
tool name as a string in the JSON body, so authorization cannot key on a
route prefix the way `app.use('/admin', requireAdmin)` does in Express. This
registry is the lookup table that replaces that.

The security property that matters here is the DEFAULT. A tool with no entry
resolves to `unknown_tool_spec()`, which requires `lead` and is flagged as
writing. So forgetting to register a new tool makes it maximally restricted,
not accidentally public. Registering is how you loosen a tool, never how you
tighten it.

Python notes for a TypeScript reader:
  - This is a plain module-level dict, populated at import time. Same shape
    as a route table you build once at boot.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..auth.tiers import Tier


@dataclass(frozen=True)
class ToolSpec:
    """What the gate needs to know about a tool before running it."""

    name: str
    required_tier: Tier
    # True if this tool can change anything in Google Ads. Write tools are
    # hidden and refused entirely when GADS_WRITE_ENABLED is false.
    writes: bool
    # Stable operation name, matched against policy.blocked_operations.
    # None for tools that are not mutations.
    operation: str | None = None
    # True for the confirm tool, which is a write in the sense that it
    # applies one, but carries a plan_id rather than change arguments.
    applies_plan: bool = False


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add a tool to the registry. Duplicate names are a programming error."""
    if spec.name in _REGISTRY:
        raise ValueError(f"tool {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec
    return spec


def unknown_tool_spec(name: str) -> ToolSpec:
    """The fail-closed default for a tool nobody registered.

    Requires the highest tier and is treated as a write, so an unregistered
    tool is hidden from everyone below lead and blocked by the kill switch.
    The gate refuses it outright; this exists so that the refusal path has a
    well-formed spec to log rather than a None to crash on.
    """
    return ToolSpec(name=name, required_tier=Tier.LEAD, writes=True, operation=name)


def spec_for(name: str) -> ToolSpec:
    return _REGISTRY.get(name) or unknown_tool_spec(name)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def all_specs() -> dict[str, ToolSpec]:
    return dict(_REGISTRY)


def reset_for_tests() -> None:
    """Clear the registry. Tests only."""
    _REGISTRY.clear()
    _register_builtin_tools()


def _register_builtin_tools() -> None:
    # health_check is the one tool available at tier `none`. Someone not yet
    # listed must still be able to ask "who does this server think I am, and
    # what do I need to do about it?" - otherwise their only feedback is an
    # empty tool list, which looks like the server being broken.
    register(
        ToolSpec(name="health_check", required_tier=Tier.NONE, writes=False)
    )

    # --- Phase 3 reads -----------------------------------------------------
    # `writes=False` means the kill switch does not hide these: turning
    # GADS_WRITE_ENABLED off is meant to make the server read-only, not
    # useless. `operation=None` because there is no mutation to match
    # against policy.blocked_operations.
    #
    # readonly is the floor for all three. Someone at tier `none` is
    # authenticated but has no Google Ads access we recognise, and account
    # names and spend figures are not public information.
    register(
        ToolSpec(name="list_accounts", required_tier=Tier.READONLY, writes=False)
    )
    register(
        ToolSpec(
            name="get_campaign_performance",
            required_tier=Tier.READONLY,
            writes=False,
        )
    )
    register(
        ToolSpec(name="get_search_terms", required_tier=Tier.READONLY, writes=False)
    )

    # --- Phase 4 writes ----------------------------------------------------
    # `writes=True` puts these behind the kill switch: GADS_WRITE_ENABLED
    # false hides them from tools/list and refuses them on tools/call.
    #
    # Both sit at operator. Pause and enable are NOT symmetric - pausing
    # stops spend, enabling resumes it, and in Phase 4 enable is the only
    # tool that can cause money to be spent. Raising enable_campaign to
    # Tier.LEAD here is a one-line change and needs nothing else.
    register(
        ToolSpec(
            name="pause_campaign",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="pause_campaign",
        )
    )
    register(
        ToolSpec(
            name="enable_campaign",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="enable_campaign",
        )
    )
    # --- Phase 5 writes ----------------------------------------------------
    # operator: budgets and negative keywords. Negatives only ever reduce
    # spend, and budgets are bounded by the policy limits plus the per-user
    # daily ceiling.
    register(
        ToolSpec(
            name="update_campaign_budget",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="update_campaign_budget",
        )
    )
    register(
        ToolSpec(
            name="add_negative_keyword",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="add_negative_keyword",
        )
    )
    # lead: the three that create new spending surface or raise what a click
    # costs. A new keyword or ad reaches an audience nobody has reviewed, and
    # a bid change has no daily ceiling behind it the way a budget does.
    register(
        ToolSpec(
            name="add_keyword",
            required_tier=Tier.LEAD,
            writes=True,
            operation="add_keyword",
        )
    )
    register(
        ToolSpec(
            name="update_ad_group_bid",
            required_tier=Tier.LEAD,
            writes=True,
            operation="update_ad_group_bid",
        )
    )
    register(
        ToolSpec(
            name="create_responsive_search_ad",
            required_tier=Tier.LEAD,
            writes=True,
            operation="create_responsive_search_ad",
        )
    )

    # `operation=None` on purpose: confirm_and_apply is not itself a
    # mutation kind, so it must not be matched against
    # policy.blocked_operations. The gate re-check inside it runs against the
    # ORIGINAL drafting tool, which carries the real operation name and the
    # real tier requirement.
    register(
        ToolSpec(
            name="confirm_and_apply",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation=None,
            applies_plan=True,
        )
    )


_register_builtin_tools()
