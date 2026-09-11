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


def annotations_for(name: str) -> dict[str, object]:
    """MCP annotations for a tool, derived from its registry entry.

    Clients use these to group tools and to decide what needs approval:
    Claude's connector settings render "Read-only tools" and "Write tools" as
    separate blocks, each with one allow / ask / deny switch. Without them
    every tool lands in a single "Other tools" bucket and a person wanting
    "ask me before anything changes" must set that tool by tool.

    That matters here specifically. Claude's connector does not support MCP
    elicitation, so `GADS_REQUIRE_HUMAN_CONFIRMATION` is false in production
    and the server can no longer force an approval prompt itself. These hints
    are what let the CLIENT be configured to ask instead. A client may ignore
    a hint, so this is not a server guarantee - it is the difference between
    an approval step being one setting and being twelve.

    Derived, never hand-written, so `writes` and `readOnlyHint` cannot drift
    apart: the gate and the client read one source.

    The destructive/read-only split follows what each tool actually does:

      reads                  touch nothing
      draft writes           create a PLAN. They do not touch Google Ads, so
                             they are not read-only but they are not
                             destructive either
      confirm_and_apply      the ONLY tool that mutates an account

    Marking the drafts destructive would train people to dismiss the prompt
    that actually matters.
    """
    spec = spec_for(name)
    # An unregistered tool is assumed to be the worst thing it could be, the
    # same way spec_for() already assumes lead+writes. `applies_plan` keeps
    # its literal meaning on ToolSpec ("carries a plan_id"); the caution
    # lives here rather than being smuggled into that field.
    destructive = spec.applies_plan or not is_registered(name)
    return {
        "readOnlyHint": not spec.writes,
        # Only the apply step changes anything in Google Ads.
        "destructiveHint": destructive,
        # Plans are single-use and consumed before execution, so repeating an
        # apply is refused rather than being a no-op. Reads may be repeated
        # freely; a draft creates a new plan each time.
        "idempotentHint": not spec.writes,
        # Everything here reaches Google Ads, which this server does not own.
        "openWorldHint": True,
    }


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

    # Discovery. Every write tool needs an id, and the performance report
    # cannot supply one for a campaign or ad group that has never served -
    # it filters on segments.date, so Google returns no rows for it. Without
    # these two, finding an id meant opening the Google Ads UI.
    register(
        ToolSpec(name="list_campaigns", required_tier=Tier.READONLY, writes=False)
    )
    register(
        ToolSpec(name="list_ad_groups", required_tier=Tier.READONLY, writes=False)
    )

    # The general escape hatch. Everything the purpose-built reads cannot
    # answer - ads, assets, conversions, geo, change history - without a new
    # tool per question. Still `readonly`, still through the full gate, and
    # still on the caller's own token, so Google decides what they may read.
    register(
        ToolSpec(name="run_gaql_query", required_tier=Tier.READONLY, writes=False)
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
    # Also operator. These were `lead` on the reasoning that they create new
    # spending surface or raise what a click costs - but a STANDARD user can
    # do every one of them in the Google Ads UI, and requiring Admin here is a
    # restriction the UI does not have. That reasoning predates the parity
    # decision and does not survive it.
    #
    # What a Standard user cannot do in the UI is manage users and manage
    # billing. There is no tool here for either, which is why no write tool
    # needs `lead` at all. tests/test_annotations.py enforces that.
    register(
        ToolSpec(
            name="add_keyword",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="add_keyword",
        )
    )
    register(
        ToolSpec(
            name="update_ad_group_bid",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="update_ad_group_bid",
        )
    )
    # Operator, like the rest. A new campaign IS a spending surface that did
    # not exist before - but a Standard user creates them in the Google Ads UI
    # every day, and this one arrives PAUSED with no ad groups, keywords or
    # ads, so it cannot spend until a person builds it out there.
    register(
        ToolSpec(
            name="create_campaign",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="create_campaign",
        )
    )
    register(
        ToolSpec(
            name="create_responsive_search_ad",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="create_responsive_search_ad",
        )
    )
    # Operator, like everything else that writes. An ad group is the container
    # add_keyword and create_responsive_search_ad target, and a Standard user
    # creates them in the Google Ads UI every day. It arrives PAUSED with no
    # keywords and no ads, so it cannot spend until someone fills it.
    register(
        ToolSpec(
            name="create_ad_group",
            required_tier=Tier.OPERATOR,
            writes=True,
            operation="create_ad_group",
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
