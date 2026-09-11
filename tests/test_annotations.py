"""Tool annotations, and the fact that they are DERIVED rather than typed out.

MCP lets a tool describe its own nature - `readOnlyHint`, `destructiveHint`
and friends. Clients use those to group tools and to decide what needs
approval: Claude's connector settings show "Read-only tools" and "Write
tools" as separate blocks, each with its own allow / ask / deny switch.

Without them every tool lands in one undifferentiated "Other tools" bucket,
and a person who wants "ask me before anything changes" has to set that
tool by tool, from memory, correctly, for ever.

That matters more here than it would elsewhere. Claude's connector does not
support MCP elicitation, so `GADS_REQUIRE_HUMAN_CONFIRMATION` is false in
production and the server can no longer force an approval prompt itself.
These hints are what let the CLIENT be configured to ask instead. They are
not a replacement for a server-side guarantee - a client can ignore a hint -
but they are the difference between an approval step being one setting and
being twelve.

The property these tests pin is that the annotations come from the SAME
registry entry the gate uses. A tool cannot be `writes=True` for the guard
and `readOnlyHint=True` for the client, because both read one source.
"""

from __future__ import annotations

import pytest

from gads_write.tools.registry import all_specs, annotations_for


def test_every_registered_tool_has_annotations() -> None:
    for name in all_specs():
        assert annotations_for(name), f"{name} has no annotations"


@pytest.mark.parametrize("name", sorted(all_specs()))
def test_read_only_matches_the_registry(name: str) -> None:
    """`readOnlyHint` must agree with `writes`, because they answer the same
    question and are read by different consumers."""
    spec = all_specs()[name]
    ann = annotations_for(name)
    assert ann["readOnlyHint"] is (not spec.writes)


def test_reads_are_marked_read_only_and_non_destructive() -> None:
    for name in ("health_check", "list_accounts", "get_campaign_performance",
                 "get_search_terms"):
        ann = annotations_for(name)
        assert ann["readOnlyHint"] is True, name
        assert ann["destructiveHint"] is False, name


def test_drafting_a_change_is_not_destructive() -> None:
    """A draft tool creates a PLAN. It does not touch Google Ads.

    Marking it destructive would train people to dismiss the prompt that
    actually matters - the one on confirm_and_apply, which is the only tool
    in this server that changes an account.
    """
    for name in ("pause_campaign", "update_campaign_budget", "add_keyword",
                 "create_responsive_search_ad", "update_ad_group_bid",
                 "add_negative_keyword", "enable_campaign"):
        ann = annotations_for(name)
        assert ann["readOnlyHint"] is False, name
        assert ann["destructiveHint"] is False, name


def test_only_confirm_is_marked_destructive() -> None:
    """One tool in this server mutates a Google Ads account. Exactly one
    should carry the hint that says so."""
    destructive = [
        name for name in all_specs() if annotations_for(name)["destructiveHint"]
    ]
    assert destructive == ["confirm_and_apply"]


def test_applying_is_not_idempotent() -> None:
    """Plans are single-use and consumed before execution, so a repeat call
    is not a no-op - it is refused. Saying otherwise would invite a client to
    retry an apply whose outcome it could not know."""
    assert annotations_for("confirm_and_apply")["idempotentHint"] is False


def test_every_tool_is_open_world() -> None:
    """All of them reach Google Ads, an external system this server does not
    own. None operate on a closed local domain."""
    for name in all_specs():
        assert annotations_for(name)["openWorldHint"] is True, name


def test_an_unregistered_tool_gets_the_most_cautious_annotations() -> None:
    """Same fail-closed default as the registry itself: an unknown tool is
    treated as a destructive write, never as a harmless read."""
    ann = annotations_for("something_nobody_registered")
    assert ann["readOnlyHint"] is False
    assert ann["destructiveHint"] is True


# ---------------------------------------------------------------------------
# parity with the Google Ads UI access levels
# ---------------------------------------------------------------------------


def test_no_write_tool_requires_more_than_a_standard_google_ads_user() -> None:
    """The design principle, made enforceable.

    A person should be able to do here what they could already do in the
    Google Ads UI. A STANDARD user there can create and edit campaigns, ad
    groups, keywords, ads, budgets and bids - everything this server writes.
    The only things Standard cannot do in the UI are manage users and manage
    billing, and there is no tool here for either.

    Requiring ADMIN for a change Standard can make in the UI is a restriction
    the UI does not have, which is exactly what makes people work around the
    tool. Four tools used to sit at `lead` on the reasoning that they create
    spending surface or raise what a click costs; that predates the parity
    decision and does not survive it.
    """
    from gads_write.auth.tiers import Tier

    too_strict = [
        name
        for name, spec in all_specs().items()
        if spec.writes and spec.required_tier is Tier.LEAD
    ]
    assert not too_strict, (
        "these require Admin but a Standard Google Ads user can do them in "
        f"the UI: {sorted(too_strict)}"
    )


def test_reads_still_need_only_read_only_access() -> None:
    """The other half of parity: a READ_ONLY user in the UI can see campaigns,
    reports and search terms, so they can here."""
    from gads_write.auth.tiers import Tier

    for name, spec in all_specs().items():
        if not spec.writes and name != "health_check":
            assert spec.required_tier is Tier.READONLY, name
