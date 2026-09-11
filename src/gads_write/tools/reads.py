"""The Phase 3 read tools.

Deliberately thin, in the way tools/__init__.py describes. Each one does
exactly four things:

    1. establish who is calling      auth/identity.py
    2. ask the gate                  safety/guards.py
    3. read                          ads/reads.py
    4. shape the answer for a human

No tool validates anything itself beyond handing a callback to the gate, and
no tool touches a Google Ads service object. A tool that grew its own logic
would be a tool that could grow its own bypass.

These are reads, so nothing here can spend money - but they still go through
the full gate. That is on purpose. The managed-account check has to hold for
reads too, or this becomes a way to read any Google Ads account the caller
happens to have on their personal login, through our developer token and our
audit log. And every read is audited, which is what lets you answer "who
looked at this account, and when".

Money is always carried in micros and always named that way. Formatting to
currency units happens once, at the edge, in `_money()`.

Python note for a TypeScript reader:
  `register_read_tools` builds closures over its dependencies rather than
  reaching for module globals. It is the same reason server.py is a
  composition root: tests call this function with fakes and get a real
  FastMCP server with no credentials involved.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastmcp.exceptions import ToolError

from ..ads.reads import MAX_LOCATION_MATCHES, AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..auth.tiers import Tier
from ..safety.accounts import AccountLookupError, ManagedAccountStore
from ..safety.audit import local_date_for
from ..safety.policy import Policy, PolicyStore
from ..safety.units import format_micros
from ..safety.validators import (
    MAX_GAQL_ROWS,
    MAX_REPORT_ROWS,
    ValidationResult,
    gaql_resource,
    validate_gaql_query,
    validate_location_query,
    with_row_limit,
    validate_customer_id,
    validate_date_range,
    validate_numeric_id,
    validate_row_limit,
)

from .registry import annotations_for

logger = logging.getLogger(__name__)

# How many accounts list_accounts will describe in one call. There is no
# silent truncation: if the manager holds more than this, the response says
# so explicitly.
MAX_ACCOUNTS_LISTED = 50

DEFAULT_REPORT_DAYS = 30
DEFAULT_ROW_LIMIT = 50


def _money(micros: int, currency_code: str) -> str:
    """Format a micros amount for a human.

    The only place micros->units conversion happens in this module. Raw
    micros are also returned alongside every formatted value, so a follow-up
    tool call never has to parse a display string back into a number.
    """
    return format_micros(micros, currency_code)


def _today_in_timezone(timezone_name: str) -> date:
    """Today, as the Google Ads account reckons it.

    Using UTC here would mean that for the first five and a half hours of
    every Indian day, "today" meant yesterday. The audit log already dates
    itself this way; reports must agree with it.

    Takes the zone NAME rather than a Policy because the account's timezone
    is only known after the gate has run - see `_derived_window`.
    """
    stamp = local_date_for(datetime.now(timezone.utc), timezone_name)
    return date.fromisoformat(stamp)


def _customer_only(customer_id: str) -> ValidationResult:
    """The whole validation for a tool whose only argument is an account."""
    result = ValidationResult()
    result.extend(validate_customer_id(customer_id))
    return result


def register_read_tools(
    mcp: Any,
    *,
    guard: Any,
    reader: AdsReader,
    policy_store: PolicyStore,
    managed_accounts: ManagedAccountStore,
    caller_provider: Callable[[], Any] = current_caller,
) -> None:
    """Define the read tools on `mcp`, closed over their dependencies."""

    async def _gate(
        *,
        tool: str,
        customer_id: str | None,
        arguments: dict[str, Any],
        validate: Callable[[Policy], ValidationResult] | None = None,
    ) -> Any:
        """Run the gate and refuse loudly if it says no.

        Raises ToolError rather than returning an error payload, so a refusal
        cannot be mistaken for an empty result. `tools/list` filtering and
        this both surface as errors to the model, which keeps one consistent
        failure shape.
        """
        caller = caller_provider()
        decision = await guard.check(
            tool=tool,
            caller=caller,
            customer_id=customer_id,
            arguments=arguments,
            validate=validate,
        )
        if not decision.allowed:
            raise ToolError(f"{decision.reason_text}. Nothing was read.")
        return decision

    # ------------------------------------------------------------------
    # list_accounts
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for('list_accounts'))
    async def list_accounts() -> dict:
        """List the Google Ads accounts you may work with, and your access on each.

        Shows only accounts under this server's manager account, never
        everything your Google login can reach. Start here to find the
        customer_id that the other tools need.
        """
        decision = await _gate(tool="list_accounts", customer_id=None, arguments={})

        # The canonical list comes from the manager account, not a config
        # file. `account_summary` is still called per account below because
        # that runs on the CALLER's credential: the MCC listing says what
        # this server manages, the per-account read says what this person can
        # actually reach.
        try:
            managed = await managed_accounts.all()
        except AccountLookupError as exc:
            raise ToolError(
                f"could not list the accounts this server manages: {exc}. "
                "Nothing was changed."
            ) from exc

        all_ids = [account.customer_id for account in managed]
        shown = all_ids[:MAX_ACCOUNTS_LISTED]
        omitted = len(all_ids) - len(shown)

        accounts: list[dict] = []
        for customer_id in shown:
            entry: dict[str, Any] = {"customer_id": customer_id}
            try:
                summary = await reader.account_summary(customer_id)
            except AdsReadError as exc:
                # One unreachable account must not blank the whole list. This
                # is the normal appearance of "you have no access to this
                # one", and saying so per-account is more useful than failing.
                entry["available"] = False
                entry["note"] = f"could not read this account: {exc}"
                accounts.append(entry)
                continue

            if summary is None:
                entry["available"] = False
                entry["note"] = "no such account, or no access to it"
                accounts.append(entry)
                continue

            entry.update(
                available=True,
                name=summary.descriptive_name,
                currency_code=summary.currency_code,
                time_zone=summary.time_zone,
                is_manager=summary.is_manager,
                is_test_account=summary.is_test_account,
                status=summary.status,
            )
            accounts.append(entry)

        result: dict[str, Any] = {
            "ok": True,
            "your_highest_access": decision.tier.value,
            "accounts": accounts,
        }
        if omitted > 0:
            # No silent caps. If we did not show everything, say so.
            result["note"] = (
                f"{omitted} further managed account(s) were not listed; "
                f"this tool describes at most {MAX_ACCOUNTS_LISTED} per call."
            )
        return result

    # ------------------------------------------------------------------
    # list_campaigns / list_ad_groups
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("list_campaigns"))
    async def list_campaigns(customer_id: str) -> dict:
        """Every campaign in an account, including ones that have never run.

        Use this to find a campaign_id. Unlike get_campaign_performance this
        is not a report and carries no metrics, so it also shows paused and
        newly created campaigns that have never served.
        """
        decision = await _gate(
            tool="list_campaigns",
            customer_id=str(customer_id).strip(),
            arguments={"customer_id": customer_id},
            validate=lambda _: _customer_only(customer_id),
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        rows = await reader.list_campaigns(str(customer_id).strip())
        return {
            "ok": True,
            "customer_id": str(customer_id).strip(),
            "currency_code": code,
            "campaign_count": len(rows),
            "campaigns": [
                {
                    "campaign_id": row.campaign_id,
                    "name": row.name,
                    "status": row.status,
                    "channel_type": row.channel_type,
                    "daily_budget": _money(row.daily_budget_micros, code),
                    "daily_budget_micros": row.daily_budget_micros,
                    "bidding_strategy": row.bidding_strategy_type,
                    # A shared budget cannot be changed through this server:
                    # the preview would name one campaign and change several.
                    "budget_is_shared": row.budget_reference_count > 1,
                }
                for row in rows
            ],
        }

    @mcp.tool(annotations=annotations_for("list_ad_groups"))
    async def list_ad_groups(
        customer_id: str, campaign_id: str | None = None
    ) -> dict:
        """Ad groups in an account, optionally within one campaign.

        Use this to find an ad_group_id, which add_keyword and
        update_ad_group_bid both require. Shows ad groups that have never
        served, which the performance reports cannot.
        """
        def validate(_: Policy) -> ValidationResult:
            result = _customer_only(customer_id)
            if campaign_id is not None:
                result.extend(
                    validate_numeric_id(campaign_id, field_name="campaign_id")
                )
            return result

        decision = await _gate(
            tool="list_ad_groups",
            customer_id=str(customer_id).strip(),
            arguments={"customer_id": customer_id, "campaign_id": campaign_id},
            validate=validate,
        )
        policy = decision.policy or policy_store.current()
        code = policy.currency_code

        rows = await reader.list_ad_groups(
            customer_id=str(customer_id).strip(),
            campaign_id=None if campaign_id is None else str(campaign_id).strip(),
        )
        return {
            "ok": True,
            "customer_id": str(customer_id).strip(),
            "ad_group_count": len(rows),
            "ad_groups": [
                {
                    "ad_group_id": row.ad_group_id,
                    "name": row.name,
                    "status": row.status,
                    "campaign_id": row.campaign_id,
                    "campaign_name": row.campaign_name,
                    "max_cpc": _money(row.cpc_bid_micros, code),
                    "max_cpc_micros": row.cpc_bid_micros,
                    "bidding_strategy": row.bidding_strategy_type,
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------------
    # find_locations
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("find_locations"))
    async def find_locations(
        customer_id: str, query: str, country_code: str | None = None
    ) -> dict:
        """Find the places Google will let a campaign target, by name.

        Use this before add_location_target, which takes ids rather than
        names. It takes ids because a name is usually a QUESTION: "Delhi"
        matches a city, a state and a union territory in Google's data, as
        three different targets with three different ids. This tool shows all
        of them so a person can say which one they meant.

        `country_code` is an ISO-3166-1 alpha-2 code such as IN, and narrows
        the search. Every result carries a canonical name
        ("New Delhi,Delhi,India") and a target type ("City"), which are what
        tell near-identical names apart.
        """
        customer_id = str(customer_id).strip()
        code = None if country_code is None else str(country_code).strip().upper()

        def validate(_: Policy) -> ValidationResult:
            result = _customer_only(customer_id)
            result.extend(validate_location_query(query))
            if code is not None and not (len(code) == 2 and code.isalpha()):
                result.add(
                    "country_code",
                    f"{country_code!r} is not a two-letter ISO country code, "
                    "e.g. IN",
                )
            return result

        await _gate(
            tool="find_locations",
            customer_id=customer_id,
            arguments={
                "customer_id": customer_id,
                "query": query,
                "country_code": code,
            },
            validate=validate,
        )

        try:
            rows = await reader.find_geo_targets(
                customer_id=customer_id,
                query=str(query).strip(),
                country_code=code,
            )
        except AdsReadError as exc:
            raise ToolError(str(exc)) from exc

        result: dict[str, Any] = {
            "ok": True,
            "query": str(query).strip(),
            "country_code": code,
            "match_count": len(rows),
            "locations": [
                {
                    "location_id": row.geo_target_id,
                    "name": row.name,
                    # The disambiguating field. Named in full on every
                    # add_location_target preview.
                    "canonical_name": row.canonical_name,
                    "target_type": row.target_type,
                    "country_code": row.country_code,
                }
                for row in rows
            ],
        }
        if len(rows) >= MAX_LOCATION_MATCHES:
            # No silent caps, the same rule list_accounts follows.
            result["note"] = (
                f"This tool returns at most {MAX_LOCATION_MATCHES} matches and "
                "that many came back, so there may be more. Narrow the search "
                "with country_code or a longer name."
            )
        if not rows:
            result["note"] = (
                "No enabled place matched that name. Try a shorter or more "
                "official spelling - Google stores English names, so "
                "'Bengaluru' and 'Bangalore' are not interchangeable."
            )
        return result

    # ------------------------------------------------------------------
    # run_gaql_query
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for("run_gaql_query"))
    async def run_gaql_query(customer_id: str, query: str) -> dict:
        """Run any Google Ads Query Language (GAQL) query against an account.

        The general read. Use it for anything the other tools do not cover -
        ads, assets, conversion actions, geo and device performance, change
        history, and so on.

        GAQL is a single SELECT against one resource; it has no JOINs and no
        subqueries. Related fields come along automatically, so
        `SELECT campaign.name, ad_group.name FROM ad_group` works.

        A LIMIT is added if you do not supply one. Resources that expose
        people or payment details rather than advertising performance are not
        available here.

        Example:
            SELECT campaign.id, campaign.name, campaign.status
            FROM campaign
            WHERE campaign.status != 'REMOVED'
        """
        def validate(_: Policy) -> ValidationResult:
            result = _customer_only(customer_id)
            result.extend(validate_gaql_query(query))
            return result

        await _gate(
            tool="run_gaql_query",
            customer_id=str(customer_id).strip(),
            # The query is audited verbatim. "Who looked at what" is only
            # answerable for a free-form read if the query itself is recorded.
            arguments={"customer_id": customer_id, "query": query},
            validate=validate,
        )

        rows = await reader.run_query(
            customer_id=str(customer_id).strip(),
            query=with_row_limit(str(query)),
        )
        return {
            "ok": True,
            "customer_id": str(customer_id).strip(),
            "resource": gaql_resource(str(query)),
            "row_count": len(rows),
            # No silent truncation: if we hit the cap, say so.
            "truncated": len(rows) >= MAX_GAQL_ROWS,
            "rows": list(rows),
        }

    # ------------------------------------------------------------------
    # get_campaign_performance
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for('get_campaign_performance'))
    async def get_campaign_performance(
        customer_id: str,
        days: int = DEFAULT_REPORT_DAYS,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> dict:
        """Campaign performance for one account, highest spend first.

        Dates are YYYY-MM-DD in the account's own timezone. Give either
        `days` (a window ending today) or an explicit `start_date` and
        `end_date`. Today's figures are always partial.

        Metrics are aggregated across the whole range - one row per campaign,
        not one row per day.
        """
        # An explicit range is the caller's own and needs no timezone. A
        # `days` window does, and the account's timezone is only known once
        # the gate has stamped it - so that half is resolved AFTER the gate.
        explicit = _explicit_window(start_date, end_date)

        arguments: dict[str, Any] = {"customer_id": customer_id, "limit": limit}
        if explicit is not None:
            arguments["start_date"], arguments["end_date"] = explicit
        else:
            arguments["days"] = days

        def validate(_: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            if explicit is not None:
                result.extend(validate_date_range(*explicit))
            result.extend(validate_row_limit(limit, maximum=MAX_REPORT_ROWS))
            return result

        decision = await _gate(
            tool="get_campaign_performance",
            customer_id=str(customer_id).strip(),
            arguments=arguments,
            validate=validate,
        )

        window_start, window_end = explicit or _derived_window(
            (decision.policy or policy_store.current()).timezone, days
        )

        rows = await reader.campaign_performance(
            customer_id=str(customer_id).strip(),
            start_date=window_start,
            end_date=window_end,
            limit=limit,
        )

        # From the gate's decision, not `policy_store.current()`: the gate
        # stamped the ACCOUNT's own currency onto its snapshot, and this
        # report may well be for an account in a different one. The fallback
        # matches tools/writes.py - `policy` is optional on GuardDecision.
        policy = decision.policy or policy_store.current()
        currency = policy.currency_code
        return {
            "ok": True,
            "customer_id": str(customer_id).strip(),
            "start_date": window_start,
            "end_date": window_end,
            "currency_code": currency,
            "campaign_count": len(rows),
            "campaigns": [
                {
                    "campaign_id": row.campaign_id,
                    "name": row.name,
                    "status": row.status,
                    "channel_type": row.channel_type,
                    "daily_budget": _money(row.daily_budget_micros, currency),
                    "daily_budget_micros": row.daily_budget_micros,
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "cost": _money(row.cost_micros, currency),
                    "cost_micros": row.cost_micros,
                    "conversions": row.conversions,
                    # Computed from cost and clicks, not read from the API.
                    # See the note in ads/reads.py.
                    "average_cpc": _money(row.average_cpc_micros, currency),
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------------
    # get_search_terms
    # ------------------------------------------------------------------

    @mcp.tool(annotations=annotations_for('get_search_terms'))
    async def get_search_terms(
        customer_id: str,
        days: int = DEFAULT_REPORT_DAYS,
        start_date: str | None = None,
        end_date: str | None = None,
        campaign_id: str | None = None,
        limit: int = DEFAULT_ROW_LIMIT,
    ) -> dict:
        """What people actually searched before seeing your ads, costliest first.

        This is the report you read before adding negative keywords. Optional
        `campaign_id` narrows it to one campaign.
        """
        explicit = _explicit_window(start_date, end_date)

        arguments: dict[str, Any] = {
            "customer_id": customer_id,
            "campaign_id": campaign_id,
            "limit": limit,
        }
        if explicit is not None:
            arguments["start_date"], arguments["end_date"] = explicit
        else:
            arguments["days"] = days

        def validate(_: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            if explicit is not None:
                result.extend(validate_date_range(*explicit))
            result.extend(validate_row_limit(limit, maximum=MAX_REPORT_ROWS))
            if campaign_id is not None:
                result.extend(
                    validate_numeric_id(campaign_id, field_name="campaign_id")
                )
            return result

        decision = await _gate(
            tool="get_search_terms",
            customer_id=str(customer_id).strip(),
            arguments=arguments,
            validate=validate,
        )

        window_start, window_end = explicit or _derived_window(
            (decision.policy or policy_store.current()).timezone, days
        )

        rows = await reader.search_terms(
            customer_id=str(customer_id).strip(),
            start_date=window_start,
            end_date=window_end,
            limit=limit,
            campaign_id=None if campaign_id is None else str(campaign_id).strip(),
        )

        currency = (decision.policy or policy_store.current()).currency_code
        return {
            "ok": True,
            "customer_id": str(customer_id).strip(),
            "start_date": window_start,
            "end_date": window_end,
            "currency_code": currency,
            "search_term_count": len(rows),
            "search_terms": [
                {
                    "search_term": row.search_term,
                    "status": row.status,
                    "campaign_id": row.campaign_id,
                    "campaign_name": row.campaign_name,
                    "ad_group_id": row.ad_group_id,
                    "ad_group_name": row.ad_group_name,
                    "impressions": row.impressions,
                    "clicks": row.clicks,
                    "cost": _money(row.cost_micros, currency),
                    "cost_micros": row.cost_micros,
                    "conversions": row.conversions,
                }
                for row in rows
            ],
        }


def _explicit_window(
    start_date: str | None, end_date: str | None
) -> tuple[str, str] | None:
    """The caller's own dates, if they gave both. None means "derive them".

    Anything malformed is passed through untouched so that the gate's
    validation step is what reports it - this must never be the thing that
    rejects input, or the error would arrive without an audit line.
    """
    if start_date and end_date:
        return str(start_date).strip(), str(end_date).strip()
    return None


def _derived_window(timezone_name: str, days: int) -> tuple[str, str]:
    """The last `days` days, ending today in the ACCOUNT'S timezone.

    Resolved AFTER the gate, never before, because the timezone belongs to
    the account and the gate is what establishes it. Deriving it earlier
    would silently use the UTC fallback and shift every window by a day for
    the first 5.5 hours of an Asia/Kolkata day.
    """

    try:
        span = int(days)
    except (TypeError, ValueError):
        span = DEFAULT_REPORT_DAYS
    span = max(1, span)

    today = _today_in_timezone(timezone_name)
    start = today - timedelta(days=span - 1)
    return start.isoformat(), today.isoformat()


__all__ = ["register_read_tools", "MAX_ACCOUNTS_LISTED"]
