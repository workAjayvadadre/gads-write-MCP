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

from ..ads.reads import AdsReader, AdsReadError
from ..auth.identity import current_caller
from ..auth.tiers import Tier
from ..safety.accounts import AccountLookupError, ManagedAccountStore
from ..safety.audit import local_date_for
from ..safety.policy import Policy, PolicyStore
from ..safety.units import format_micros
from ..safety.validators import (
    MAX_REPORT_ROWS,
    ValidationResult,
    validate_customer_id,
    validate_date_range,
    validate_numeric_id,
    validate_row_limit,
)

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


def _today_in_account_timezone(policy: Policy) -> date:
    """Today, as the Google Ads account reckons it.

    Using UTC here would mean that for the first five and a half hours of
    every Indian day, "today" meant yesterday. The audit log already dates
    itself this way; reports must agree with it.
    """
    stamp = local_date_for(datetime.now(timezone.utc), policy.timezone)
    return date.fromisoformat(stamp)


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

    @mcp.tool
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
    # get_campaign_performance
    # ------------------------------------------------------------------

    @mcp.tool
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
        policy = policy_store.current()
        window_start, window_end = _resolve_window(policy, days, start_date, end_date)

        arguments = {
            "customer_id": customer_id,
            "start_date": window_start,
            "end_date": window_end,
            "limit": limit,
        }

        def validate(_: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_date_range(window_start, window_end))
            result.extend(validate_row_limit(limit, maximum=MAX_REPORT_ROWS))
            return result

        decision = await _gate(
            tool="get_campaign_performance",
            customer_id=str(customer_id).strip(),
            arguments=arguments,
            validate=validate,
        )

        rows = await reader.campaign_performance(
            customer_id=str(customer_id).strip(),
            start_date=window_start,
            end_date=window_end,
            limit=limit,
        )

        # From the gate's decision, not `policy_store.current()`: the gate
        # stamped the ACCOUNT's own currency onto its snapshot, and this
        # report may well be for an account in a different one.
        currency = decision.policy.currency_code
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

    @mcp.tool
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
        policy = policy_store.current()
        window_start, window_end = _resolve_window(policy, days, start_date, end_date)

        arguments = {
            "customer_id": customer_id,
            "start_date": window_start,
            "end_date": window_end,
            "campaign_id": campaign_id,
            "limit": limit,
        }

        def validate(_: Policy) -> ValidationResult:
            result = ValidationResult()
            result.extend(validate_customer_id(customer_id))
            result.extend(validate_date_range(window_start, window_end))
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

        rows = await reader.search_terms(
            customer_id=str(customer_id).strip(),
            start_date=window_start,
            end_date=window_end,
            limit=limit,
            campaign_id=None if campaign_id is None else str(campaign_id).strip(),
        )

        currency = decision.policy.currency_code
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


def _resolve_window(
    policy: Policy,
    days: int,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str, str]:
    """Turn (days | explicit dates) into one pair of ISO dates.

    Explicit dates win when both are given. Anything malformed is passed
    through untouched so that the gate's validation step is what reports it -
    this function must never be the thing that rejects input, or the error
    would arrive without an audit line.
    """
    if start_date and end_date:
        return str(start_date).strip(), str(end_date).strip()

    try:
        span = int(days)
    except (TypeError, ValueError):
        span = DEFAULT_REPORT_DAYS
    span = max(1, span)

    today = _today_in_account_timezone(policy)
    start = today - timedelta(days=span - 1)

    return (
        str(start_date).strip() if start_date else start.isoformat(),
        str(end_date).strip() if end_date else today.isoformat(),
    )


__all__ = ["register_read_tools", "MAX_ACCOUNTS_LISTED"]
